"""Aggregate order status notifications (plan Phase 5, §1 and §4.2).

Laravel pushes one event per stop change; the Messager recomputes the batch and
notifies the client **at most once per batch-level transition**:

===============  ==========================================================
``asignado``     every stop has a courier assigned (first time)
``en_ruta``      the first stop starts the route
``entregado``    every stop delivered
``cancelado``    every stop canceled
===============  ==========================================================

Each transition is controlled by ``BotConfig.order_status_enabled``. The
cost-saving default only notifies ``entregado`` and ``cancelado``: every
outbound WhatsApp message is billable, and the creation confirmation already
covers the "recibido" moment. Disabled transitions are still claimed in
``Order.notified_statuses`` (without sending), so enabling a switch later never
replays a stale state. A mixed batch (for example one canceled stop and one
delivered stop) sends nothing: agents see it in the order card.

Delivery: the client first receives a normal free-text message (valid inside the
WhatsApp 24 h customer-service window). When Meta rejects it because the window
is closed, ``api.integrations.notify`` replaces the failed text with the approved
Meta template (``aviso_asignado`` …) configured in
``BotConfig.order_status_templates``. Messages never include the client name.
"""

import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger('api')

DEFAULT_ORDER_STATUS_MESSAGES = {
    'asignado': '🛵 Pedido {order_numbers} asignado. Un domiciliario va en camino a recogerlo.',
    'en_ruta': '🛵 Pedido {order_numbers} en camino.',
    'entregado': '✅ Pedido {order_numbers} entregado. ¡Gracias por preferirnos!',
    'cancelado': '❌ Pedido {order_numbers} cancelado. Si necesitas ayuda, escríbenos.',
}

# Per-status switches. WhatsApp bills every outbound message, so the default
# keeps only the two moments that matter to the client: delivery and the rare
# cancellation. "recibido" is the order-creation confirmation (a separate
# BotConfig key) and is not part of this map.
DEFAULT_ORDER_STATUS_ENABLED = {
    'asignado': False,
    'en_ruta': False,
    'entregado': True,
    'cancelado': True,
}

# Names supplied by the user (already approved by Meta). Each entry describes
# the template body parameter: ``name`` is the variable name used in the
# approved template (``{{order_code}}``) and ``value`` the Messager value fed
# into it. ``params`` still accepts plain value-key strings for positional
# templates (``{{1}}``).
DEFAULT_ORDER_STATUS_TEMPLATES = {
    'asignado': {
        'template': 'aviso_asignado', 'language': 'es',
        'params': [{'name': 'order_code', 'value': 'order_number'}],
    },
    'en_ruta': {
        'template': 'aviso_en_ruta', 'language': 'es',
        'params': [{'name': 'order_code', 'value': 'order_number'}],
    },
    'entregado': {
        'template': 'aviso_entregado', 'language': 'es',
        'params': [{'name': 'order_code', 'value': 'order_number'}],
    },
    'cancelado': {
        'template': 'aviso_cancelado', 'language': 'es',
        'params': [{'name': 'order_code', 'value': 'order_number'}],
    },
}

STATUS_LABELS = {
    'asignado': 'Asignado',
    'en_ruta': 'En camino',
    'entregado': 'Entregado',
    'cancelado': 'Cancelado',
}

# Stop statuses that mean a courier is already assigned.
ASSIGNED_STOP_STATUSES = frozenset({'asignado', 'confirmado', 'en_ruta', 'entregado'})

# Never send a lower positive transition after a higher one went out.
POSITIVE_RANK = {'asignado': 1, 'en_ruta': 2, 'entregado': 3}

# Values available to message texts and templates. The client name is
# intentionally excluded (acceptance: messages must not include it).
VALUE_KEYS = (
    'status', 'status_label', 'order_number', 'order_numbers',
    'total', 'count', 'origin',
)


def _normalize(status) -> str:
    return str(status or '').strip().lower()


def get_order_status_messages() -> dict:
    """Configured status texts merged over the defaults."""
    from api.bot.config import get_config

    messages = dict(DEFAULT_ORDER_STATUS_MESSAGES)
    stored = get_config('order_status_messages', None)
    if isinstance(stored, dict):
        for key, text in stored.items():
            name = _normalize(key)
            if name in messages and isinstance(text, str) and text.strip():
                messages[name] = text
    return messages


def _as_bool(value, default: bool) -> bool:
    """Best-effort boolean for values saved through the admin API."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ('1', 'true', 'yes', 'on', 'sí', 'si'):
            return True
        if normalized in ('0', 'false', 'no', 'off', ''):
            return False
    return default


def get_order_status_enabled() -> dict:
    """Configured per-transition switches merged over the cost-saving defaults."""
    from api.bot.config import get_config

    enabled = dict(DEFAULT_ORDER_STATUS_ENABLED)
    stored = get_config('order_status_enabled', None)
    if isinstance(stored, dict):
        for key, value in stored.items():
            name = _normalize(key)
            if name in enabled:
                enabled[name] = _as_bool(value, enabled[name])
    return enabled


def _clean_params(raw) -> list:
    """Keep configured template params: value-key strings or ``{name, value}``."""
    cleaned = []
    for item in raw or []:
        if isinstance(item, str) and item.strip():
            cleaned.append(item.strip())
        elif isinstance(item, dict):
            name = str(item.get('name') or '').strip()
            value_key = str(item.get('value') or '').strip() or name
            if name or value_key:
                cleaned.append({'name': name, 'value': value_key})
    return cleaned


def get_order_status_templates() -> dict:
    """Configured template descriptors merged over the defaults."""
    from api.bot.config import get_config

    templates = {key: dict(value) for key, value in DEFAULT_ORDER_STATUS_TEMPLATES.items()}
    stored = get_config('order_status_templates', None)
    if isinstance(stored, dict):
        for key, value in stored.items():
            name = _normalize(key)
            if name not in templates or not isinstance(value, dict):
                continue
            merged = templates[name]
            for field in ('template', 'language'):
                if isinstance(value.get(field), str) and value[field].strip():
                    merged[field] = value[field].strip()
            params = value.get('params')
            if isinstance(params, list):
                cleaned = _clean_params(params)
                if cleaned:
                    merged['params'] = cleaned
    return templates


def notifications_enabled() -> bool:
    """Master switch (``BotConfig.order_status_notifications_enabled``)."""
    from api.bot.config import get_config

    return bool(get_config('order_status_notifications_enabled', True))


def resolve_transition(order) -> str | None:
    """The highest batch-level transition currently satisfied by ``order``.

    Returns only the most advanced state so a courier jumping straight to
    ``en_ruta`` never triggers an extra ``asignado`` message.
    """
    statuses = [_normalize(stop.status) for stop in order.stops.all()]
    if not statuses:
        return None
    if all(status == 'entregado' for status in statuses):
        return 'entregado'
    if all(status == 'cancelado' for status in statuses):
        return 'cancelado'
    if 'en_ruta' in statuses:
        return 'en_ruta'
    if all(status in ASSIGNED_STOP_STATUSES for status in statuses):
        return 'asignado'
    return None


def status_values(order, status: str) -> dict:
    """Placeholder values for the status texts and templates."""
    from api.integrations.orders import (
        first_order_number_text,
        format_cop,
        order_numbers_text,
    )

    values = {
        'status': status,
        'status_label': STATUS_LABELS.get(status, status),
        'order_number': first_order_number_text(order),
        'order_numbers': order_numbers_text(order) or f'#{order.id}',
        'total': format_cop(order.total),
        'count': order.stops.count(),
        'origin': order.origin_address or '',
    }
    return {key: values.get(key, '') for key in VALUE_KEYS}


def render_status_text(order, status: str) -> str:
    """The configured free-text notification for ``status``."""
    template = get_order_status_messages().get(status) or DEFAULT_ORDER_STATUS_MESSAGES[status]
    values = status_values(order, status)
    out = template
    for key, value in values.items():
        out = out.replace('{' + key + '}', str(value))
    return out


def build_fallback_template(order, status: str) -> dict | None:
    """Descriptor for the Meta template used when the text window is closed."""
    config = get_order_status_templates().get(status)
    if not config:
        return None
    name = (config.get('template') or '').strip()
    if not name:
        return None
    params = _clean_params(config.get('params'))
    if not params:
        params = [{'name': 'order_code', 'value': 'order_number'}]
    values = status_values(order, status)
    return {
        'status': status,
        'name': name,
        'language': (config.get('language') or 'es').strip() or 'es',
        'params': params,
        'values': values,
    }


def claim_transition(order, status: str) -> bool:
    """Atomically mark ``status`` as notified and return whether it was claimed.

    Must be called inside the transaction that saved the order: the row lock
    taken by the order ``UPDATE`` serializes concurrent events for the same
    batch, so a transition can never be notified twice. Lower positive
    transitions are skipped once a higher one was sent (for example a batch that
    went straight from ``asignado`` to ``en_ruta``).
    """
    from api.models import Order

    locked = Order.objects.select_for_update().get(pk=order.pk)
    notified = list(locked.notified_statuses or [])
    if status in notified:
        order.notified_statuses = notified
        return False
    rank = POSITIVE_RANK.get(status, 0)
    if rank and any(POSITIVE_RANK.get(item, 0) > rank for item in notified):
        order.notified_statuses = notified
        return False
    notified.append(status)
    locked.notified_statuses = notified
    locked.save(update_fields=['notified_statuses', 'updated_at'])
    order.notified_statuses = notified
    return True


def has_delivery_target(conversation) -> bool:
    """Whether the client can be addressed: phone or WhatsApp username.

    Username-only contacts (the WhatsApp username feature) have an empty
    ``contact_phone``; the shared send path targets their business-scoped user
    ID stored in ``whatsapp_id`` (``_resolve_whatsapp_target``). Every
    conversation has a ``whatsapp_id``, so this is False only when the
    conversation (or its whole identity) is missing.
    """
    if conversation is None:
        return False
    return bool(conversation.contact_phone or conversation.whatsapp_id)


def send_status_notification(order_id: int, status: str) -> None:
    """Send one aggregate notification for ``order_id`` (after commit)."""
    from api.models import Order

    from .notify import send_text

    try:
        order = (
            Order.objects.select_related('conversation')
            .prefetch_related('stops')
            .get(id=order_id)
        )
    except Order.DoesNotExist:
        return

    conversation = order.conversation
    if not has_delivery_target(conversation):
        return

    text = render_status_text(order, status)
    fallback = build_fallback_template(order, status)
    try:
        send_text(
            conversation,
            text,
            fallback_template=fallback,
            preview=text,
        )
        logger.info('Order %s status notification sent (%s)', order_id, status)
    except Exception:
        logger.exception('Failed to send order %s status notification (%s)', order_id, status)


def notify_transition(order, status: str) -> bool:
    """Claim ``status`` and schedule the message once the transaction commits.

    Returns True when this call claimed the transition (response ``notified``).
    """
    if not claim_transition(order, status):
        return False
    order_id = order.id
    transaction.on_commit(lambda: send_status_notification(order_id, status))
    return True


def maybe_notify(order) -> str | None:
    """Evaluate the batch, claim the current transition and send it.

    Returns the notified status (or ``None``).
    """
    if not notifications_enabled():
        return None
    # Un pedido programado no notifica al cliente hasta que llegue su hora:
    # ops puede editarlo o pre-asignar un domi sin disparar el "asignado".
    if (order.status or '').strip().lower() == 'programado':
        scheduled = getattr(order, 'scheduled_for', None)
        if scheduled is None or scheduled > timezone.now():
            logger.info(
                'Order %s is still scheduled; notification suppressed', order.id,
            )
            return None
    transition = resolve_transition(order)
    if not transition:
        return None
    if not get_order_status_enabled().get(transition, True):
        # Consume the transition silently. If the switch is enabled later, the
        # client must not receive a stale "asignado"/"en ruta" message.
        claim_transition(order, transition)
        logger.info(
            'Order %s %s notification disabled; transition consumed',
            order.id, transition,
        )
        return None
    if not has_delivery_target(order.conversation):
        # Nothing to address (no phone and no username): do not claim the
        # transition, so it can still be sent if an identity appears later.
        logger.warning(
            'Order %s has no delivery target; %s notification skipped',
            order.id, transition,
        )
        return None
    if not notify_transition(order, transition):
        return None
    return transition
