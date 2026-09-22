"""Outbound message helpers for the integration layer.

Extracted from ``ConversationViewSet.send_template`` (views.py) so order
notifications can build template payloads and send messages immediately,
bypassing ``send_delay_seconds``.

Phase 5 adds the service-window fallback: a notification is first sent as a
normal free-text message (valid inside the WhatsApp 24 h customer service
window). When Meta rejects it with ``131047`` (Re-engagement message) the failed
text message is hidden and replaced by the approved Meta template, so the
client always receives exactly one meaningful message.
"""

import json
import logging
import re

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger('api')

_PREVIEW_BY_TYPE = {
    'image': '[Image]',
    'video': '[Video]',
    'audio': '[Audio]',
    'sticker': '[Sticker]',
    'document': '[Document]',
}

# Meta rejects free-form messages outside the 24 h service window with these.
SERVICE_WINDOW_ERROR_CODES = frozenset({131047, 470})
_SERVICE_WINDOW_HINTS = ('131047', 're-engagement')

_NAMED_VAR_RE = re.compile(r'\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}')
_POSITIONAL_VAR_RE = re.compile(r'\{\{(\d+)\}\}')


def build_template_payload(template, parameters=None):
    """Build the WhatsApp template payload for ``template``.

    ``parameters`` is a dict with either named body params
    ``{param_name: value}`` plus optional ``header_media_id`` / ``buttons``.
    """
    parameters = parameters or {}
    components = []

    for comp in template.components:
        comp_type = comp.get('type')

        if comp_type == 'body':
            params = []
            if parameters:
                for name, value in parameters.items():
                    if name in ('header_media_id', 'buttons'):
                        continue
                    params.append({
                        'type': 'text',
                        'parameter_name': name,
                        'text': value,
                    })
            components.append({'type': 'body', 'parameters': params})

        elif comp_type == 'header' and comp.get('format') in ('image', 'video', 'document'):
            header_media_id = parameters.get('header_media_id')
            if header_media_id:
                components.append({
                    'type': 'header',
                    'parameters': [{
                        'type': comp['format'],
                        comp['format']: {'id': header_media_id},
                    }],
                })

        elif comp_type == 'buttons':
            button_params = parameters.get('buttons', [])
            btn_components = []
            for i, btn in enumerate(comp.get('buttons', [])):
                if btn.get('type') == 'url' and i < len(button_params):
                    btn_components.append({
                        'type': 'url',
                        'text': btn['text'],
                        'url': button_params[i],
                    })
            if btn_components:
                components.append({
                    'type': 'button',
                    'sub_type': 'url',
                    'index': '0',
                    'parameters': btn_components,
                })

    return {
        'name': template.name,
        'language': {'code': template.language},
        'components': components,
    }


def _named_param(name, values):
    return {'type': 'text', 'parameter_name': name, 'text': str(values.get(name, ''))}


def _body_parameters(component, values, param_order, name_map=None):
    """Body parameters for one template body component.

    Templates created in the Messager carry their parameter names inline
    (``{{cliente}}`` / internal ``parameters`` metadata); templates created
    directly in Meta may use named (``{{order_code}}``) or positional
    (``{{1}}``) placeholders. Named placeholders resolve through ``name_map``
    first, then by name and finally by position in ``param_order``, so a
    differently named approved template still gets its values.
    """
    name_map = name_map or {}
    text = component.get('text') or ''
    declared = [
        p.get('name') for p in (component.get('parameters') or [])
        if isinstance(p, dict) and p.get('name')
    ]
    if declared:
        return [
            {'type': 'text', 'parameter_name': name,
             'text': str(_param_value(name, index, values, param_order, name_map))}
            for index, name in enumerate(declared)
        ]
    if _POSITIONAL_VAR_RE.search(text):
        return [{'type': 'text', 'text': str(values.get(name, ''))} for name in param_order]
    named = _NAMED_VAR_RE.findall(text)
    if named:
        return [
            {'type': 'text', 'parameter_name': name,
             'text': str(_param_value(name, index, values, param_order, name_map))}
            for index, name in enumerate(named)
        ]
    return []


def _param_value(name, index, values, param_order, name_map=None):
    mapped = (name_map or {}).get(name)
    if mapped and mapped in values:
        return values[mapped]
    if name in values:
        return values.get(name, '')
    if index < len(param_order):
        return values.get(param_order[index], '')
    return ''


def build_template_payload_from_values(template, values, param_order=None, name_map=None):
    """Build a payload for ``template`` mapping ``values`` to its placeholders.

    ``param_order`` lists the value keys in order (positional bodies);
    ``name_map`` maps the template's own placeholder names to value keys
    (``{'order_code': 'order_number'}``). Unresolved names render as empty
    strings, so a template whose parameters were renamed never breaks the send.
    """
    values = values or {}
    param_order = [str(name) for name in (param_order or []) if name]
    name_map = {
        str(name): str(key) for name, key in (name_map or {}).items() if name and key
    }
    components = []
    has_body = False
    for comp in (template.components or []):
        if not isinstance(comp, dict):
            continue
        if comp.get('type') == 'body':
            has_body = True
            components.append({
                'type': 'body',
                'parameters': _body_parameters(comp, values, param_order, name_map),
            })
    if not has_body:
        components.append({
            'type': 'body',
            'parameters': [_named_param(name, values) for name in param_order],
        })
    return {
        'name': template.name,
        'language': {'code': template.language or 'es'},
        'components': components,
    }


def _normalize_fallback_params(raw):
    """Normalize configured params to ``[{'name': str|None, 'value': key}]``.

    A string entry is a value key for a positional template (``{{1}}``); an
    object entry describes a named template parameter
    (``{'name': 'order_code', 'value': 'order_number'}``). Old configurations
    with plain value-key strings keep working.
    """
    entries = []
    for item in raw or []:
        if isinstance(item, dict):
            name = str(item.get('name') or '').strip() or None
            value_key = str(item.get('value') or '').strip() or name or ''
            if value_key:
                entries.append({'name': name, 'value': value_key})
        elif isinstance(item, str) and item.strip():
            entries.append({'name': None, 'value': item.strip()})
    return entries or [{'name': None, 'value': 'order_number'}]


def build_fallback_template_payload(fallback):
    """Payload for a fallback descriptor ``{name, language, params, values}``.

    Uses the local template row (named or positional placeholders) when it
    exists; otherwise sends the configured parameters as a generic body so the
    approved template still receives its values.
    """
    name = (fallback.get('name') or '').strip()
    language = (fallback.get('language') or 'es').strip() or 'es'
    values = fallback.get('values') if isinstance(fallback.get('values'), dict) else {}
    entries = _normalize_fallback_params(fallback.get('params'))
    param_order = [entry['value'] for entry in entries]
    name_map = {
        entry['name']: entry['value'] for entry in entries if entry['name']
    }

    if not name:
        return None

    from api.models import WhatsAppTemplate

    template = (
        WhatsAppTemplate.objects.filter(name=name, language=language).first()
        or WhatsAppTemplate.objects.filter(name=name).first()
    )
    if template is not None:
        return build_template_payload_from_values(
            template, values, param_order, name_map,
        )

    if name_map and len(name_map) == len(entries):
        parameters = [
            {'type': 'text', 'parameter_name': entry['name'],
             'text': str(values.get(entry['value'], ''))}
            for entry in entries
        ]
    else:
        parameters = [
            {'type': 'text', 'text': str(values.get(entry['value'], ''))}
            for entry in entries
        ]

    return {
        'name': name,
        'language': {'code': language},
        'components': [{'type': 'body', 'parameters': parameters}],
    }


def _preview(message_type, content):
    if message_type == 'text':
        return content
    if message_type == 'template':
        try:
            payload = json.loads(content) if isinstance(content, str) else content
            return f"[{payload.get('name', 'template')}]"
        except (ValueError, TypeError):
            return '[Template]'
    return _PREVIEW_BY_TYPE.get(message_type, f'[{message_type.capitalize()}]')


def create_and_send_outbound(conversation, message_type, content, *,
                             sender=None, sender_name='', metadata=None,
                             fallback_template=None, preview=None):
    """Create an outbound ``Message`` and send it to WhatsApp immediately.

    Unlike the agent flow this never honors ``send_delay_seconds``: ops
    notifications must go out right away. Returns the created ``Message``.

    ``fallback_template`` is an optional descriptor
    (``{name, language, params, values}``) sent instead when Meta rejects the
    message because the 24 h service window is closed. ``preview`` overrides
    the conversation list preview (``last_message``).
    """
    from api.models import Message

    meta = dict(metadata or {})
    if fallback_template:
        meta['fallback_template'] = fallback_template

    message = Message.objects.create(
        conversation=conversation,
        direction='outbound',
        message_type=message_type,
        content=content,
        sender_name=sender_name or 'Sistema',
        sender=sender,
        metadata=meta,
    )

    conversation.last_message = preview or _preview(message_type, content)
    conversation.last_message_at = timezone.now()
    conversation.save(update_fields=['last_message', 'last_message_at', 'updated_at'])
    conversation._last_msg_direction = 'outbound'
    if hasattr(conversation, '_prefetched_objects_cache'):
        conversation._prefetched_objects_cache.pop('takes', None)

    # Imports here to avoid a circular import with api.views.
    from api.serializers import MessageSerializer
    from api.views import _send_pool, publish_conversation_update

    def _send(mid=message.id):
        from api.models import Message as Msg

        try:
            msg = Msg.objects.select_related('context_message').get(id=mid)
        except Msg.DoesNotExist:
            return
        fallback = msg.metadata.get('fallback_template') if isinstance(msg.metadata, dict) else None
        context_wamid = msg.context_message.whatsapp_message_id if msg.context_message else None
        _send_pool.submit(
            _deliver_outbound,
            msg.id,
            msg.conversation_id,
            fallback,
            context_wamid,
        )

    transaction.on_commit(_send)
    # Publish the message right away so open threads show it even before (or
    # without) the WhatsApp send result; the send path merges by id later.
    publish_conversation_update(conversation, MessageSerializer(message).data)
    return message


def _is_service_window_error(metadata) -> bool:
    """Whether the send failure means the 24 h customer service window is closed."""
    metadata = metadata or {}
    code = metadata.get('send_error_code')
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = None
    if code in SERVICE_WINDOW_ERROR_CODES:
        return True
    text = str(metadata.get('send_error') or '').lower()
    return any(hint in text for hint in _SERVICE_WINDOW_HINTS)


def _deliver_outbound(message_id, conversation_id, fallback=None, context_wamid=None):
    """Send one message, replacing it with the approved template on window errors.

    Runs inside the WhatsApp send pool. ``send_whatsapp_outbound`` records the
    Meta error in ``Message.metadata`` synchronously, so the fallback decision
    is made right after it returns.
    """
    from api.models import Message

    # Imported here (not at module level) to avoid a circular import.
    from api.views import send_whatsapp_outbound

    msg = (
        Message.objects.select_related('conversation')
        .filter(id=message_id)
        .first()
    )
    if msg is None:
        return

    send_whatsapp_outbound(
        msg.message_type or 'text',
        msg.media_url or msg.content,
        msg.conversation.contact_phone,
        msg.id,
        msg.conversation_id,
        context_wamid=context_wamid,
    )

    if fallback:
        send_service_window_fallback(msg.id, fallback)


def send_service_window_fallback(message_id, fallback):
    """Swap a text notification rejected for the closed window with its template.

    The failed text message is marked ``cancelled`` (hidden from the thread,
    excluded by ``MessageViewSet``) and the template message is sent in its
    place. Idempotent: a message already replaced is never sent twice.
    """
    from api.models import Message

    msg = (
        Message.objects.select_related('conversation')
        .filter(id=message_id)
        .first()
    )
    if msg is None or not msg.conversation:
        return None

    meta = dict(msg.metadata or {})
    if meta.get('fallback_sent') or not _is_service_window_error(meta):
        return None

    payload = build_fallback_template_payload(fallback)
    if not payload:
        logger.warning('Status fallback without a template name for message %s', message_id)
        return None

    preview = msg.content if msg.message_type == 'text' else None

    meta['fallback_sent'] = True
    meta['status'] = 'cancelled'
    msg.metadata = meta
    msg.save(update_fields=['metadata'])

    template_message = create_and_send_outbound(
        msg.conversation,
        'template',
        json.dumps(payload, ensure_ascii=False),
        sender_name='Sistema',
        metadata={
            'status_fallback_for': msg.id,
            'status_fallback_name': (fallback.get('name') or '')[:120],
            'status_fallback_state': (fallback.get('status') or '')[:20],
        },
        preview=preview,
    )

    meta['fallback_message_id'] = template_message.id
    msg.metadata = meta
    msg.save(update_fields=['metadata'])
    _publish_message_update(msg)
    logger.info(
        'Status text message %s replaced by template %s (message %s)',
        message_id, fallback.get('name'), template_message.id,
    )
    return template_message


def _publish_message_update(message):
    try:
        from api.serializers import MessageSerializer
        from api.views import publish_conversation_update

        publish_conversation_update(message.conversation, MessageSerializer(message).data)
    except Exception:
        logger.exception('Failed to publish message update for %s', message.id)


def send_text(conversation, text, *, sender=None, sender_name='Sistema',
              fallback_template=None, preview=None):
    """Convenience wrapper for text notifications."""
    return create_and_send_outbound(
        conversation, 'text', text, sender=sender, sender_name=sender_name,
        fallback_template=fallback_template, preview=preview,
    )


def send_template(conversation, template, parameters=None, *,
                  sender=None, sender_name='Sistema'):
    """Build and send a WhatsApp template message immediately."""
    payload = build_template_payload(template, parameters)
    return create_and_send_outbound(
        conversation, 'template', json.dumps(payload),
        sender=sender, sender_name=sender_name,
    )
