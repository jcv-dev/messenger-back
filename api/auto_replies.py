"""Detect automated business replies and keep them out of the unread queue.

Marketing template blasts usually trigger business auto-responders
("¡Hola! Gracias por escribirnos, en breve te atenderemos…"). Those messages
are real and stay in the thread, but they carry no agent value and otherwise
turn hundreds of conversations unread after every campaign.

The guard classifies an inbound message with configurable phrases, records the
match in ``Message.metadata['auto_reply_guard']``, sets ``is_automated`` and —
by default — ``is_read``. Detection is restricted to conversations that
received a MARKETING template inside a configurable window, so a human reply in
an organic conversation is never swallowed by a generic greeting.

Configuration lives in ``BotConfig`` key ``auto_reply_guard``:

    {
      "enabled": true,
      "mark_read": true,
      "require_marketing": true,
      "marketing_window_minutes": 2,
      "patterns": ["gracias por escribirnos", ...]
    }

Missing keys fall back to the defaults below, so the guard works right after
deploying even before an admin opens the settings page.
"""

import json
import re
import unicodedata
from datetime import timedelta

from django.utils import timezone

from .bot.config import get_config
from .models import Message, WhatsAppTemplate

DEFAULT_PATTERNS = [
    'mensaje automático',
    'respuesta automática',
    'esta es una respuesta automática',
    'gracias por escribirnos',
    'gracias por escribir',
    'gracias por contactarnos',
    'gracias por comunicarte',
    'gracias por comunicarse',
    'gracias por tu mensaje',
    'gracias por su mensaje',
    'hemos recibido tu mensaje',
    'recibimos tu mensaje',
    'tu mensaje fue recibido',
    'su mensaje ha sido recibido',
    'mensaje recibido',
    'en breve te atenderemos',
    'te atenderemos en breve',
    'en breve un asesor te atenderá',
    'un asesor te atenderá',
    'pronto nos pondremos en contacto',
    'te responderemos lo antes posible',
    'te responderemos a la brevedad',
    'no podemos atenderte',
    'en este momento no podemos',
    'horario de atención',
    'fuera de horario',
    'fuera de nuestro horario',
    'fuera de la oficina',
    'no estamos disponibles',
    'no se encuentra disponible',
    'asistente virtual',
    'chatbot',
    'respuesta generada automáticamente',
    'no monitoreamos este número',
    'responde con una opción',
    'selecciona una opción',
    'marca una opción',
    'automated response',
    'automatic reply',
    'auto reply',
    'this is an automated',
    'out of office',
    'we have received your message',
    'we will get back to you',
    'we are currently unavailable',
    'we are away',
    'please leave a message',
    'thank you for contacting',
    'thank you for your message',
    'business hours',
]

DEFAULT_CONFIG = {
    'enabled': True,
    'mark_read': True,
    'require_marketing': True,
    'marketing_window_minutes': 2,
}
DEFAULT_WINDOW_MINUTES = 2
MAX_WINDOW_MINUTES = 24 * 60 * 30

# Auto-responders answer with text (or a caption on an attachment). Reactions
# and edits are never auto-replies.
DETECTABLE_TYPES = frozenset({'text', 'image', 'video', 'document'})


def normalize_text(value):
    """Lowercase, strip accents/punctuation and collapse whitespace.

    The normalized form is only used for matching; the original message and
    pattern are preserved for display.
    """
    text = unicodedata.normalize('NFKD', str(value or ''))
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    return ' '.join(text.split())


def _as_bool(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ('', '0', 'false', 'no', 'off')
    return bool(value)


def _as_window_minutes(value):
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_MINUTES
    return max(1, min(MAX_WINDOW_MINUTES, minutes))


def _pattern_pairs(raw_patterns):
    """Return ``[(original, normalized), ...]`` for valid non-empty patterns."""
    if not isinstance(raw_patterns, (list, tuple)):
        raw_patterns = DEFAULT_PATTERNS
    pairs = []
    seen = set()
    for raw in raw_patterns:
        if not isinstance(raw, str):
            continue
        normalized = normalize_text(raw)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        pairs.append((raw.strip(), normalized))
    if not pairs:
        pairs = [(pattern, normalize_text(pattern)) for pattern in DEFAULT_PATTERNS]
    return pairs


def get_guard_config():
    raw = get_config('auto_reply_guard', None)
    if not isinstance(raw, dict):
        raw = {}
    return {
        'enabled': _as_bool(raw.get('enabled'), DEFAULT_CONFIG['enabled']),
        'mark_read': _as_bool(raw.get('mark_read'), DEFAULT_CONFIG['mark_read']),
        'require_marketing': _as_bool(
            raw.get('require_marketing'), DEFAULT_CONFIG['require_marketing'],
        ),
        'marketing_window_minutes': _as_window_minutes(
            raw.get('marketing_window_minutes', DEFAULT_WINDOW_MINUTES),
        ),
        'patterns': _pattern_pairs(raw.get('patterns')),
    }


def match_pattern(content, patterns):
    """Return ``(original_pattern, normalized_pattern)`` or ``None``."""
    normalized = normalize_text(content)
    if not normalized:
        return None
    padded = f' {normalized} '
    for original, pattern in patterns:
        if f' {pattern} ' in padded:
            return original, pattern
    return None


def _template_name_from_message(message):
    """Best-effort template name for messages created before this feature."""
    meta = message.metadata or {}
    name = meta.get('template_name')
    if isinstance(name, str) and name:
        return name
    try:
        payload = json.loads(message.content)
    except (TypeError, ValueError):
        return None
    if isinstance(payload, dict):
        name = payload.get('name')
        if isinstance(name, str) and name:
            return name
    return None


def _recent_marketing_template(conversation, window_minutes, at):
    """Return the name of a MARKETING template sent inside the window."""
    since = at - timedelta(minutes=window_minutes)
    recent = (
        Message.objects
        .filter(
            conversation=conversation,
            direction='outbound',
            message_type='template',
            created_at__gte=since,
            created_at__lte=at,
        )
        .order_by('-created_at')[:25]
    )

    candidate_names = []
    for message in recent:
        meta = message.metadata or {}
        category = str(meta.get('template_category') or '').upper()
        name = _template_name_from_message(message)
        if category == 'MARKETING':
            return name or 'marketing'
        if category:
            # Explicit non-marketing category: do not let a same-named local
            # template misclassify a utility/authentication send.
            continue
        if name and name not in candidate_names:
            candidate_names.append(name)

    if not candidate_names:
        return None

    name = (
        WhatsAppTemplate.objects
        .filter(name__in=candidate_names, category='MARKETING')
        .values_list('name', flat=True)
        .first()
    )
    return name


def classify_message(conversation, message_type, content, at=None):
    """Classify an inbound message as an automated reply.

    Returns the evidence dict stored in ``metadata['auto_reply_guard']``, or
    ``None`` when the message is not an automated reply. ``at`` lets the
    backfill command evaluate the marketing window relative to the message's
    own timestamp instead of "now".
    """
    config = get_guard_config()
    if not config['enabled'] or message_type not in DETECTABLE_TYPES:
        return None

    matched = match_pattern(content, config['patterns'])
    if not matched:
        return None
    matched_original, matched_normalized = matched

    at = at or timezone.now()
    template_name = None
    if config['require_marketing']:
        template_name = _recent_marketing_template(
            conversation, config['marketing_window_minutes'], at,
        )
        if not template_name:
            return None

    return {
        'matched_pattern': matched_original,
        'matched_normalized': matched_normalized,
        'template_name': template_name,
        'window_minutes': config['marketing_window_minutes'] if config['require_marketing'] else None,
        'require_marketing': config['require_marketing'],
        'mark_read': config['mark_read'],
        'detected_at': timezone.now().isoformat(),
    }


def apply_guard(message, match, mark_read=None):
    """Persist a classification on an existing message and return it."""
    meta = dict(message.metadata or {})
    meta['auto_reply_guard'] = match
    message.metadata = meta
    message.is_automated = True
    if mark_read is None:
        mark_read = bool(match.get('mark_read', True))
    if mark_read:
        message.is_read = True
    message.save(update_fields=['metadata', 'is_automated', 'is_read'])
    return message
