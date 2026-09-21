"""Client coupling between conversations and ops clients (plan §5, Phase 2).

Two responsibilities:

- Resolve an ops client from a WhatsApp phone with Redis caching
  (``ops:client:{phone}``, positive 24 h / negative 1 h).
- Persist the conversation ↔ client link (``ops_client_*`` fields) and run
  the best-effort auto-link from the webhook in a daemon thread, so an ops
  failure never blocks the conversation flow.
"""

import logging
import threading

from django.core.cache import cache
from django.utils import timezone

from api.models import Conversation

from . import ops
from .phones import to_ops

logger = logging.getLogger('api')

CLIENT_CACHE_PREFIX = 'ops:client:'
CLIENT_CACHE_POSITIVE_TTL = 86400  # 24 h
CLIENT_CACHE_NEGATIVE_TTL = 3600   # 1 h
# ``cache.get`` returns ``None`` on a miss, so a stored negative needs a marker.
CLIENT_CACHE_NEGATIVE = '__none__'

MATCH_SOURCE_AUTO_PHONE = 'auto_phone'
MATCH_SOURCE_MANUAL = 'manual'
MATCH_SOURCE_ORDER = 'order'


def cache_key(phone: str) -> str:
    return f'{CLIENT_CACHE_PREFIX}{phone}'


def get_cached_client(phone: str):
    """Return ``(hit, payload)``; ``payload`` is ``None`` for a negative hit."""
    cached = cache.get(cache_key(phone))
    if cached is None:
        return False, None
    if cached == CLIENT_CACHE_NEGATIVE:
        return True, None
    return True, cached


def cache_client(phone: str, payload: dict | None):
    if payload is None:
        cache.set(cache_key(phone), CLIENT_CACHE_NEGATIVE, CLIENT_CACHE_NEGATIVE_TTL)
    else:
        cache.set(cache_key(phone), payload, CLIENT_CACHE_POSITIVE_TTL)


def normalize_client(data) -> dict | None:
    """Turn a ``GET /api/v1/clients/{phone}`` payload into a linkable snapshot.

    Riders (``domiciliario``) and unknown phones are not clients.
    """
    if not isinstance(data, dict) or not data.get('found'):
        return None
    if (data.get('type') or '') != 'cliente':
        return None
    try:
        client_id = int(data.get('id'))
    except (TypeError, ValueError):
        return None
    return {
        'id': client_id,
        'name': str(data.get('name') or '')[:255],
        'phone': str(data.get('phone') or '')[:20],
        'address': str(data.get('address') or '')[:500],
    }


def fetch_client_by_phone(phone, timeout: float | None = None) -> dict | None:
    """Resolve an ops client from a WhatsApp phone, with caching.

    Network errors are never cached (the next inbound retries); unknown phones
    and riders are cached as negative for 1 h.
    """
    ops_phone = to_ops(phone)
    if not ops_phone:
        return None

    hit, payload = get_cached_client(ops_phone)
    if hit:
        return payload

    try:
        data = ops.get_client_by_phone(ops_phone, timeout=timeout)
    except ops.OpsAPIError as exc:
        logger.warning('Ops client lookup failed for %s: %s', ops_phone, exc)
        return None

    snapshot = normalize_client(data)
    cache_client(ops_phone, snapshot)
    return snapshot


def link_conversation(conversation: Conversation, client_id: int, snapshot: dict | None = None,
                      source: str = MATCH_SOURCE_MANUAL) -> Conversation:
    """Persist the conversation ↔ ops client link."""
    conversation.ops_client_user_id = int(client_id)
    conversation.ops_client_linked_at = timezone.now()
    conversation.ops_client_match_source = source
    if snapshot:
        conversation.ops_client_snapshot = {
            'id': int(client_id),
            'name': str(snapshot.get('name') or '')[:255],
            'phone': str(snapshot.get('phone') or '')[:20],
            'address': str(snapshot.get('address') or '')[:500],
        }
    conversation.save(update_fields=[
        'ops_client_user_id', 'ops_client_linked_at',
        'ops_client_match_source', 'ops_client_snapshot', 'updated_at',
    ])
    return conversation


def unlink_conversation(conversation: Conversation) -> Conversation:
    conversation.ops_client_user_id = None
    conversation.ops_client_linked_at = None
    conversation.ops_client_match_source = ''
    conversation.ops_client_snapshot = {}
    conversation.save(update_fields=[
        'ops_client_user_id', 'ops_client_linked_at',
        'ops_client_match_source', 'ops_client_snapshot', 'updated_at',
    ])
    return conversation


def auto_link_now(conversation_id: int, phone: str) -> bool:
    """Synchronous auto-link body. Returns True when a link was created.

    Safe to call concurrently: the conversation is re-checked after the ops
    round-trip and a manual link always wins.
    """
    if not phone:
        return False

    conversation = Conversation.objects.filter(id=conversation_id).first()
    if conversation is None or conversation.ops_client_user_id:
        return False

    client = fetch_client_by_phone(phone)
    if not client:
        return False

    # Re-read: an agent may have linked (or unlinked) while ops answered.
    conversation.refresh_from_db(fields=['ops_client_user_id'])
    if conversation.ops_client_user_id:
        return False

    link_conversation(
        conversation, client['id'], snapshot=client,
        source=MATCH_SOURCE_AUTO_PHONE,
    )
    logger.info(
        'Auto-linked conversation %s to ops client %s', conversation.id, client['id'],
    )

    # An ops batch created before the link never matched an event; pull the
    # client's active orders into the mirror now (best effort).
    try:
        from .adoption import sync_active_orders
        sync_active_orders(conversation)
    except Exception:
        logger.exception('Failed to backfill active orders after auto-link')

    try:
        from api.views import publish_conversation_update
        publish_conversation_update(conversation)
    except Exception:
        logger.exception('Failed to publish conversation.updated after auto-link')

    return True


def schedule_auto_link(conversation_id: int, phone: str) -> bool:
    """Fire-and-forget auto-link in a daemon thread.

    Returns True when a thread was started. Skips silently when ops is not
    configured or the phone cannot be normalized.
    """
    if not phone or not to_ops(phone) or not ops.is_configured():
        return False

    thread = threading.Thread(
        target=_run_auto_link,
        args=(conversation_id, phone),
        name=f'ops-auto-link-{conversation_id}',
        daemon=True,
    )
    thread.start()
    return True


def _run_auto_link(conversation_id: int, phone: str):
    try:
        auto_link_now(conversation_id, phone)
    except Exception:
        logger.exception('Auto-link ops client failed for conversation %s', conversation_id)
