"""SLA reminder notifications to couriers (ops → Messager, WhatsApp).

Ops' ``domii:sla-alerts`` command detects orders stuck past a stage threshold
and reminds the courier through Telegram and the mobile app. This module adds
the WhatsApp channel: a utility Meta template (``aviso_sla`` by default) is
sent directly, because a courier is normally outside the 24 h customer-service
window.

Endpoint payload (``POST /api/integrations/notifications/sla/``)::

    {
      "alert_key": "sla:12345:asignado:5:2",
      "courier": {"id": 1642, "name": "Samuel Niampira", "phone": "3007654321"},
      "order": {"order_number": 900123456, "status": "asignado",
                "minutes": 12, "threshold": 5}
    }

``alert_key`` is idempotent for 24 h, so ops queue retries never duplicate the
message; ops appends the threshold cycle (``minutes // threshold``) so an
intentional later reminder is a different key. When the courier has no
conversation yet, one is created (and tagged ``Domii``) so the alert also shows
up in the panel thread.
"""

import json
import logging

from django.core.cache import cache

from .notify import build_fallback_template_payload, create_and_send_outbound
from .phones import to_wa
from .status_notifications import _clean_params

logger = logging.getLogger('api')

ALERT_DEDUPE_TTL = 86400  # 24 h

# Statuses ops alerts on: assigned (not confirmed), confirmed (not on route)
# and on route (not delivered).
SLA_ALERT_STATUSES = frozenset({'asignado', 'confirmado', 'en_ruta'})

# Human label rendered in the template body.
STATUS_LABELS = {
    'asignado': 'asignado',
    'confirmado': 'confirmado',
    'en_ruta': 'en ruta',
}

# Approved Meta template: "Tu pedido {{order_code}} lleva {{time}} minutos en
# estado {{order_status}}. Por favor actualízalo mediante Telegram o comenta tu
# situación a la central." ``params`` maps the template's own placeholder names
# to the value keys built by ``sla_values``.
DEFAULT_SLA_TEMPLATE = {
    'template': 'aviso_sla',
    'language': 'es',
    'params': [
        {'name': 'order_code', 'value': 'order_code'},
        {'name': 'time', 'value': 'time'},
        {'name': 'order_status', 'value': 'order_status'},
    ],
}


def notifications_enabled() -> bool:
    """Master switch (``BotConfig.sla_notifications_enabled``)."""
    from api.bot.config import get_config

    return bool(get_config('sla_notifications_enabled', True))


def get_sla_template() -> dict:
    """Configured template descriptor merged over the defaults."""
    from api.bot.config import get_config

    config = {
        'template': DEFAULT_SLA_TEMPLATE['template'],
        'language': DEFAULT_SLA_TEMPLATE['language'],
        'params': [dict(param) for param in DEFAULT_SLA_TEMPLATE['params']],
    }
    stored = get_config('sla_alert_template', None)
    if isinstance(stored, dict):
        for field in ('template', 'language'):
            if isinstance(stored.get(field), str) and stored[field].strip():
                config[field] = stored[field].strip()
        params = stored.get('params')
        if isinstance(params, list):
            cleaned = _clean_params(params)
            if cleaned:
                config['params'] = cleaned
    return config


def sla_values(payload: dict) -> dict:
    """Values available to the SLA template body."""
    order = payload.get('order') if isinstance(payload.get('order'), dict) else {}
    number = order.get('order_number')
    try:
        order_code = f'#{int(number)}'
    except (TypeError, ValueError):
        order_code = ''
    try:
        minutes = str(max(0, int(order.get('minutes'))))
    except (TypeError, ValueError):
        minutes = '0'
    raw_status = str(order.get('status') or '').strip().lower()
    return {
        'order_code': order_code,
        'time': minutes,
        'order_status': STATUS_LABELS.get(raw_status, raw_status),
    }


def build_sla_template_descriptor(payload: dict) -> dict | None:
    """Fallback descriptor ``{name, language, params, values}`` for the alert."""
    config = get_sla_template()
    name = (config.get('template') or '').strip()
    if not name:
        return None
    params = config.get('params') or DEFAULT_SLA_TEMPLATE['params']
    return {
        'status': 'sla',
        'name': name,
        'language': (config.get('language') or 'es').strip() or 'es',
        'params': params,
        'values': sla_values(payload),
    }


def _claim_alert_key(alert_key: str) -> bool:
    """False when ``alert_key`` was already sent (degrades open without cache)."""
    if not alert_key:
        return True
    try:
        return bool(cache.add(f'integration:sla:{alert_key}', 1, ALERT_DEDUPE_TTL))
    except Exception:
        logger.warning('SLA alert dedupe cache unavailable for %s', alert_key)
        return True


def _release_alert_key(alert_key: str) -> None:
    if not alert_key:
        return
    try:
        cache.delete(f'integration:sla:{alert_key}')
    except Exception:
        logger.warning('SLA alert dedupe cache release failed for %s', alert_key)


def _resolve_conversation(phone: str, name: str):
    """Conversation for ``phone``, creating (and tagging) it when missing."""
    from api.models import Conversation, ConversationTag
    from api.views import get_default_group

    conversation = (
        Conversation.objects.filter(contact_phone=phone).first()
        or Conversation.objects.filter(whatsapp_id=phone).first()
    )
    if conversation is None:
        conversation = Conversation.objects.create(
            whatsapp_id=phone,
            contact_name=(name or phone)[:255],
            contact_phone=phone,
            group=get_default_group(),
            status='active',
        )
        from .views import DOMII_TAG_COLOR, DOMII_TAG_NAME

        ConversationTag.create_tag(
            conversation, DOMII_TAG_NAME,
            expiry_type='never', tag_color=DOMII_TAG_COLOR,
        )
    else:
        updates = []
        if not conversation.contact_phone:
            conversation.contact_phone = phone
            updates.append('contact_phone')
        if not conversation.contact_name and name:
            conversation.contact_name = name[:255]
            updates.append('contact_name')
        if updates:
            updates.append('updated_at')
            conversation.save(update_fields=updates)
    return conversation


def send_sla_alert(payload: dict) -> dict:
    """Send one courier SLA reminder through WhatsApp.

    Returns ``{sent, duplicate, skipped, message_id}``. The WhatsApp delivery
    itself runs in the shared send pool (``create_and_send_outbound``), so a
    Meta failure is recorded on the ``Message`` instead of raised here.
    """
    courier = payload.get('courier') if isinstance(payload.get('courier'), dict) else {}
    order = payload.get('order') if isinstance(payload.get('order'), dict) else {}
    alert_key = str(payload.get('alert_key') or '').strip()

    if not notifications_enabled():
        return {'sent': False, 'duplicate': False, 'skipped': 'disabled', 'message_id': None}

    phone = to_wa(courier.get('phone'))
    if not phone:
        return {'sent': False, 'duplicate': False, 'skipped': 'invalid_phone', 'message_id': None}

    if not _claim_alert_key(alert_key):
        return {'sent': False, 'duplicate': True, 'skipped': None, 'message_id': None}

    descriptor = build_sla_template_descriptor(payload)
    template_payload = build_fallback_template_payload(descriptor) if descriptor else None
    if not template_payload:
        _release_alert_key(alert_key)
        logger.warning('SLA alert without a usable template: %s', alert_key)
        return {'sent': False, 'duplicate': False, 'skipped': 'template_missing', 'message_id': None}

    values = descriptor['values']
    raw_status = str(order.get('status') or '').strip().lower()
    preview = (
        f"⏰ Pedido {values['order_code']} lleva {values['time']} min "
        f"en estado {values['order_status']}"
    ).strip()

    try:
        conversation = _resolve_conversation(phone, str(courier.get('name') or ''))
        message = create_and_send_outbound(
            conversation,
            'template',
            json.dumps(template_payload, ensure_ascii=False),
            sender_name='Sistema',
            metadata={
                'sla_alert': {
                    'alert_key': alert_key,
                    'order_number': order.get('order_number'),
                    'status': raw_status,
                    'status_label': values['order_status'],
                    'minutes': values['time'],
                    'threshold': order.get('threshold'),
                    'courier_id': courier.get('id'),
                },
            },
            preview=preview,
        )
    except Exception:
        _release_alert_key(alert_key)
        raise

    logger.info(
        'SLA alert sent to %s for order %s (%s, %s min) — message %s',
        phone, order.get('order_number'), raw_status, values['time'], message.id,
    )
    return {'sent': True, 'duplicate': False, 'skipped': None, 'message_id': message.id}
