"""Backfill the conversation mirror with the linked client's active ops orders.

Laravel pushes one event per change, but a batch created while the conversation
was not linked to its ops client never matches and is dropped (``linked:
false``). Linking the client afterwards used to leave those orders invisible in
``Pedidos activos`` until their next status change — and a courier release in
ops does not push an event at all (release paths in ``OrdersService`` do not
dispatch ``NotifyMessagerOrderStatus``), so a released order could stay hidden
forever.

``sync_active_orders`` closes that gap: right after a link (manual or by phone)
it lists the client's recent orders in ops and adopts the active ones that are
missing locally. The adoption reuses :func:`api.integrations.views.adopt_order`
so the mirror shape is identical to the webhook's. Notifications for states
that already happened before the link are suppressed (``notified_statuses``),
so backfilling never surprises the client with a late WhatsApp message.
"""

import datetime
import logging

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from api.models import Order, OrderStop

from .couriers import courier_snapshot

logger = logging.getLogger('api')

#: Ops stores ``orders.created_at`` in Bogotá local time (session TZ ``-05:00``).
OPS_LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=-5))

#: Bound the backfill: how many recent orders to inspect and adopt.
BACKFILL_ORDER_LIMIT = 10
BACKFILL_MAX_ADOPTIONS = 5
BACKFILL_TIMEOUT = 5.0
#: Coalesce concurrent syncs for the same conversation (double click on
#: Vincular, command running next to a link…).
BACKFILL_LOCK_TTL = 60


def _lock_key(conversation_id: int) -> str:
    return f'order_backfill:{conversation_id}'


def _acquire_lock(conversation_id: int) -> bool:
    """Best effort; degrades to ``True`` when the cache is unavailable."""
    try:
        return cache.add(_lock_key(conversation_id), 1, BACKFILL_LOCK_TTL)
    except Exception:
        logger.warning('Order backfill lock unavailable for conversation %s', conversation_id)
        return True


def _created_at_value(source) -> str | None:
    """Ops ``created_at`` (Bogotá local, no offset) as an aware ISO string."""
    raw = source.get('created_at') if isinstance(source, dict) else None
    if not raw:
        return None
    value = parse_datetime(str(raw))
    if value is None:
        return None
    if timezone.is_naive(value):
        value = value.replace(tzinfo=OPS_LOCAL_TZ)
    return value.isoformat()


def _backfill_payload(conversation, client_id: int, row: dict, order_number: int) -> dict:
    """Build the event-shaped payload :func:`adopt_order` expects.

    The client-orders row carries ``batch_id``/``total``/``created_at``;
    ``GET /orders/{n}`` adds per-stop price/coords/observation and the courier
    identity (``{id, name, code}``, or the legacy code string). When the detail
    call fails the row is enough (``Refrescar`` fills the gaps later).
    """
    from . import ops
    from .views import _as_int

    detail = {}
    try:
        data = ops.get_order(order_number, timeout=BACKFILL_TIMEOUT)
        if isinstance(data, dict) and data.get('ok'):
            detail = data
    except ops.OpsAPIError as exc:
        logger.warning(
            'Order backfill: detail lookup failed for order %s: %s', order_number, exc,
        )

    entries = detail.get('stops') if isinstance(detail.get('stops'), list) else None
    if not entries:
        entries = row.get('stops') if isinstance(row.get('stops'), list) else []
    entries = [entry for entry in entries if isinstance(entry, dict)]
    if not entries:
        entries = [{'stop': 1, 'status': row.get('status')}]

    courier = courier_snapshot(detail)
    snapshot = (
        conversation.ops_client_snapshot
        if isinstance(conversation.ops_client_snapshot, dict) else {}
    )

    payload = {
        'event': 'order.created',
        'batch_id': row.get('batch_id'),
        'order_number': order_number,
        'stop': _as_int(entries[0].get('stop')) or 1,
        'status': str(detail.get('status') or row.get('status') or '').strip().lower(),
        'status_label': str(detail.get('status_label') or row.get('status_label') or ''),
        'client': {
            'id': client_id,
            'name': str(snapshot.get('name') or conversation.contact_name or '')[:255],
            'phone': str(snapshot.get('phone') or ''),
        },
        'origin': str(detail.get('origin') or row.get('origin') or ''),
        'total': _as_int(row.get('total')),
        'courier_code': courier.get('code', ''),
        'courier': courier,
        'stops': entries,
    }
    occurred_at = _created_at_value(detail) or _created_at_value(row)
    if occurred_at:
        payload['occurred_at'] = occurred_at
    return payload


def _finalize_adopted(order: Order) -> None:
    """Mark the batch as backfilled and mute transitions that already happened."""
    from .status_notifications import resolve_transition
    from .views import recompute_order_status

    if hasattr(order, '_prefetched_objects_cache'):
        order._prefetched_objects_cache.pop('stops', None)
    recompute_order_status(order)

    updates = ['status', 'total']
    payload = order.payload if isinstance(order.payload, dict) else {}
    if not payload.get('backfilled'):
        payload['backfilled'] = True
        order.payload = payload
        updates.append('payload')

    transition = resolve_transition(order)
    notified = list(order.notified_statuses or [])
    if transition and transition not in notified:
        notified.append(transition)
        order.notified_statuses = notified
        updates.append('notified_statuses')

    order.last_synced_at = timezone.now()
    updates.extend(['last_synced_at', 'updated_at'])
    order.save(update_fields=updates)


def _publish_backfilled(order_ids: list[int]) -> None:
    from .views import _publish_order_updated

    for order in Order.objects.select_related('conversation').filter(id__in=order_ids):
        _publish_order_updated(order)


def sync_active_orders(conversation, client_id: int | None = None) -> int:
    """Adopt the linked client's active ops orders into the conversation.

    Best effort by design: returns the number of batches adopted and never
    raises — a link must not fail because ops is slow or down. Idempotent:
    a batch already mirrored (any conversation) is skipped.
    """
    from . import ops
    from .orders import FINISHED_STATUSES
    from .views import _as_int, adopt_order

    client_id = _as_int(client_id or getattr(conversation, 'ops_client_user_id', 0))
    if not client_id or not ops.is_configured():
        return 0
    if not _acquire_lock(conversation.id):
        return 0

    try:
        response = ops.get_client_orders(
            client_id, limit=BACKFILL_ORDER_LIMIT, timeout=BACKFILL_TIMEOUT,
        )
    except ops.OpsAPIError as exc:
        logger.warning(
            'Order backfill: ops client-orders lookup failed for client %s: %s',
            client_id, exc,
        )
        return 0

    rows = response.get('orders') if isinstance(response, dict) else None
    if not isinstance(rows, list):
        return 0

    adopted: list[Order] = []
    for row in rows:
        if len(adopted) >= BACKFILL_MAX_ADOPTIONS:
            break
        if not isinstance(row, dict):
            continue

        order_number = _as_int(row.get('order_number'))
        status = str(row.get('status') or '').strip().lower()
        if not order_number or status in FINISHED_STATUSES:
            continue
        if OrderStop.objects.filter(ops_order_number=order_number).exists():
            continue

        data = _backfill_payload(conversation, client_id, row, order_number)
        try:
            with transaction.atomic():
                order, _ = adopt_order(conversation, data, order_number)
                _finalize_adopted(order)
        except Exception:
            logger.exception(
                'Order backfill: could not adopt order %s for conversation %s',
                order_number, conversation.id,
            )
            continue

        adopted.append(order)

    if adopted:
        logger.info(
            'Order backfill: %s batch(es) adopted for conversation %s',
            len(adopted), conversation.id,
        )
        order_ids = [order.id for order in adopted]
        transaction.on_commit(lambda: _publish_backfilled(order_ids))

    return len(adopted)
