"""Integration endpoints (ops → Messager), plan §4.2.

Base path ``api/integrations/``. Every request requires an integration API key
(``X-Api-Key``) with the endpoint scope plus an HMAC signature
(``X-Signature`` / ``X-Timestamp``).
"""

import logging
import time

from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from api.models import BotExemptContact, Conversation, ConversationTag, Order, OrderStop
from api.realtime import publish
from api.serializers import OrderSerializer
from api.views import publish_conversation_update

from .auth import (
    HasIntegrationScope,
    IntegrationKeyAuthentication,
    IntegrationRateThrottle,
    SCOPE_EXEMPTIONS_WRITE,
    SCOPE_ORDERS_WRITE,
)
from .hmac import verify_webhook_request
from .phones import to_wa
from .status_notifications import maybe_notify

logger = logging.getLogger('api')

DOMII_TAG_NAME = 'Domii'
DOMII_TAG_COLOR = 'gray'
EVENT_DEDUPE_TTL = 86400  # 24 h

KNOWN_ORDER_STATUSES = frozenset({
    'nuevo', 'disponible', 'asignado', 'confirmado', 'en_ruta',
    'entregado', 'cancelado',
})

# Local mirror statuses considered "not started yet".
_PENDING_LOCAL = frozenset({'draft', 'pending', 'failed', ''})


def _as_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _dedupe_event(key: str) -> bool:
    """Return True when the event was already processed.

    Degrades gracefully (returns False) if the cache backend is unavailable.
    """
    try:
        return not cache.add(key, 1, EVENT_DEDUPE_TTL)
    except Exception:
        logger.warning('Integration dedupe cache unavailable for %s', key)
        return False


def _publish_order_updated(order: Order):
    try:
        payload = {
            'type': 'order.updated',
            'conversation_id': order.conversation_id,
            'order': OrderSerializer(order).data,
        }
        publish(payload, group_id=order.conversation.group_id)
    except Exception:
        logger.exception('Failed to publish order.updated SSE')


def recompute_order_status(order: Order) -> str:
    """Mirror Laravel's ``recomputeOrderTotalsAndStatus`` derivation rules."""
    statuses = [
        (s.status or '').strip().lower()
        for s in order.stops.all()
    ]
    if not statuses:
        return order.status

    normalized = ['nuevo' if s in _PENDING_LOCAL else s for s in statuses]
    all_done = all(s == 'entregado' for s in normalized)
    all_canceled = all(s == 'cancelado' for s in normalized)
    has_en_ruta = 'en_ruta' in normalized
    has_confirmed = 'confirmado' in normalized
    has_assigned = 'asignado' in normalized
    has_new = 'nuevo' in normalized
    has_available = 'disponible' in normalized
    courier_assigned = any(
        s in ('asignado', 'confirmado', 'en_ruta', 'entregado') for s in normalized
    )

    if all_done:
        new_status = 'entregado'
    elif all_canceled:
        new_status = 'cancelado'
    elif has_en_ruta:
        new_status = 'en_ruta'
    elif courier_assigned and has_confirmed and not has_assigned:
        new_status = 'confirmado'
    elif courier_assigned and has_assigned:
        new_status = 'asignado'
    elif courier_assigned:
        new_status = 'asignado'
    elif has_available or has_new or has_assigned:
        new_status = 'disponible'
    else:
        new_status = order.status

    order.status = new_status
    total = sum((s.price or 0) for s in order.stops.all())
    order.total = total
    return new_status


class IntegrationAPIView(APIView):
    """Base class: key auth + HMAC signature + scope + per-key throttle."""

    authentication_classes = [IntegrationKeyAuthentication]
    permission_classes = [HasIntegrationScope]
    throttle_classes = [IntegrationRateThrottle]
    integration_scope = None

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        verify_webhook_request(request)


class PingView(IntegrationAPIView):
    """POST /api/integrations/ping/ — connectivity test for ops settings.

    Requires a valid key + HMAC but no specific scope. Never mutates data.
    """

    def post(self, request):
        key = request.auth
        return Response({
            'ok': True,
            'key_name': getattr(key, 'name', ''),
            'scopes': list(getattr(key, 'scopes', []) or []),
        })


class ExemptionsSyncView(IntegrationAPIView):
    """POST /api/integrations/exemptions/sync/ — full rider snapshot replace.

    Plan §4.2 steps:
    1. Normalize phones.
    2. Active couriers upsert ``BotExemptContact(source='ops_sync')``.
    3. Delete ``ops_sync`` contacts no longer present/inactive (manual rows untouched).
    4. Add the never-expiring ``Domii`` tag to matching conversations.
    5. Remove it from couriers that became inactive/removed.
    6. Publish ``conversation.updated`` SSE for affected conversations.
    """

    integration_scope = SCOPE_EXEMPTIONS_WRITE

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        kind = (data.get('kind') or 'riders').strip().lower()
        if kind != 'riders':
            return Response(
                {'ok': False, 'error': f'kind no soportado: {kind}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        couriers = data.get('couriers')
        if not isinstance(couriers, list):
            return Response(
                {'ok': False, 'error': 'couriers debe ser una lista.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        requested = []
        for item in couriers:
            if not isinstance(item, dict):
                continue
            phone = to_wa(item.get('phone'))
            if not phone:
                continue
            try:
                courier_id = int(item.get('id'))
            except (TypeError, ValueError):
                courier_id = None
            requested.append({
                'phone': phone,
                'name': str(item.get('name') or '')[:255],
                'id': courier_id,
                'active': bool(item.get('active', True)),
            })

        active_by_phone = {c['phone']: c for c in requested if c['active']}
        inactive_phones = {c['phone'] for c in requested if not c['active']}

        now = timezone.now()
        affected_ids = set()
        applied = removed = tags_added = tags_removed = 0

        with transaction.atomic():
            previous_phones = set(
                BotExemptContact.objects.filter(source='ops_sync')
                .values_list('contact_phone', flat=True)
            )

            for phone, courier in active_by_phone.items():
                BotExemptContact.objects.update_or_create(
                    contact_phone=phone,
                    defaults={
                        'contact_name': courier['name'],
                        'source': 'ops_sync',
                        'ops_courier_id': courier['id'],
                    },
                )
                applied += 1

            stale = BotExemptContact.objects.filter(source='ops_sync').exclude(
                contact_phone__in=active_by_phone
            )
            stale_phones = set(stale.values_list('contact_phone', flat=True))
            removed = len(stale_phones)
            if stale_phones:
                stale.delete()

            # ── Add the Domii tag to active couriers' conversations ──────
            active_convs = list(
                Conversation.objects.filter(contact_phone__in=active_by_phone)
            )
            for conv in active_convs:
                already = ConversationTag.objects.filter(
                    conversation=conv, tag_name=DOMII_TAG_NAME
                ).filter(
                    Q(expires_at__gt=now) | Q(expires_at__isnull=True)
                ).exists()
                if not already:
                    ConversationTag.create_tag(
                        conv, DOMII_TAG_NAME,
                        expiry_type='never', tag_color=DOMII_TAG_COLOR,
                    )
                    tags_added += 1
                    affected_ids.add(conv.id)

            # ── Remove the Domii tag from removed/inactive couriers ──────
            untag_phones = stale_phones | inactive_phones
            if untag_phones:
                untag_convs = list(Conversation.objects.filter(contact_phone__in=untag_phones))
                affected_ids.update(c.id for c in untag_convs)
                deleted, _ = ConversationTag.objects.filter(
                    conversation__in=untag_convs,
                    tag_name=DOMII_TAG_NAME,
                ).delete()
                tags_removed = int(deleted or 0)

        if affected_ids:
            ids = list(affected_ids)

            def _publish_affected():
                for conv in Conversation.objects.filter(id__in=ids):
                    publish_conversation_update(conv)

            transaction.on_commit(_publish_affected)

        return Response({
            'ok': True,
            'applied': applied,
            'removed': removed,
            'tags_added': tags_added,
            'tags_removed': tags_removed,
        })


class OrderEventsView(IntegrationAPIView):
    """POST /api/integrations/orders/events/ — order status events.

    Phase 0: dedupe, locate the local stop/order, update the mirror, recompute
    the batch status and publish ``order.updated``. Aggregate client
    notifications are Phase 5 (``notified`` is always ``false`` for now).
    """

    integration_scope = SCOPE_ORDERS_WRITE

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        event = (data.get('event') or 'order.status_changed').strip()
        if event not in ('order.created', 'order.status_changed'):
            return Response(
                {'ok': False, 'error': f'event no soportado: {event}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            order_number = int(data.get('order_number'))
        except (TypeError, ValueError):
            return Response(
                {'ok': False, 'error': 'order_number requerido.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        status_value = str(data.get('status') or '').strip().lower()
        if status_value not in KNOWN_ORDER_STATUSES:
            return Response(
                {'ok': False, 'error': f'status inválido: {status_value}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        occurred_at = str(data.get('occurred_at') or '')
        stop_no = self._event_stop_no(data) or 0
        dedupe_key = (
            f'integration:event:{order_number}:{stop_no}:{status_value}:{occurred_at}'
        )
        if _dedupe_event(dedupe_key):
            return Response({
                'ok': True,
                'duplicate': True,
                'linked': True,
                'batch_status': None,
                'notified': False,
            })

        order, stop = self._locate(data, order_number)
        if stop is None:
            return Response({
                'ok': True,
                'linked': False,
                'batch_status': None,
                'notified': False,
            })

        now = timezone.now()
        created = event == 'order.created'
        if created:
            touched = self._apply_created(order, stop, data, status_value, now)
        else:
            self._apply_event(order, stop, data, status_value, now)
            touched = [stop]

        notified_status = None
        with transaction.atomic():
            for target in touched:
                target.save()
            order.ops_batch_id = order.ops_batch_id or (data.get('batch_id') or None)
            order.ops_client_user_id = order.ops_client_user_id or self._client_id(data)
            if data.get('origin'):
                order.origin_address = str(data['origin'])
            client = data.get('client') if isinstance(data.get('client'), dict) else {}
            if client.get('name'):
                order.client_name = str(client['name'])[:255]
            recompute_order_status(order)
            order.last_synced_at = now
            order.save()
            # Phase 5: at most one aggregate client message per batch transition.
            notified_status = maybe_notify(order)

        order.refresh_from_db()
        transaction.on_commit(lambda oid=order.id: _publish_order_updated(
            Order.objects.select_related('conversation').get(id=oid)
        ))

        return Response({
            'ok': True,
            'linked': True,
            'batch_status': order.status,
            'notified': notified_status is not None,
            'notified_status': notified_status,
        })

    # ------------------------------------------------------------------
    #  Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _client_id(data) -> int | None:
        client = data.get('client') if isinstance(data.get('client'), dict) else {}
        try:
            return int(client.get('id'))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _event_stop_no(data) -> int | None:
        """Stop index from the event payload (comandas share one order number)."""
        try:
            stop_no = int(data.get('stop'))
        except (TypeError, ValueError):
            return None
        return stop_no if stop_no > 0 else None

    @staticmethod
    def _match_stop(order: Order, order_number: int, stop_no: int | None = None):
        numbered = order.stops.filter(ops_order_number=order_number)
        if stop_no is not None:
            stop = numbered.filter(stop_no=stop_no).first()
            if stop:
                return stop
        stop = numbered.first()
        if stop:
            return stop

        unnumbered = order.stops.filter(ops_order_number__isnull=True)
        if stop_no is not None:
            stop = unnumbered.filter(stop_no=stop_no).first()
            if stop:
                return stop
        return unnumbered.order_by('stop_no').first()

    def _locate(self, data, order_number: int):
        stop_no = self._event_stop_no(data)

        qs = (
            OrderStop.objects
            .select_related('order', 'order__conversation')
            .filter(ops_order_number=order_number)
        )
        stop = qs.filter(stop_no=stop_no).first() if stop_no is not None else None
        stop = stop or qs.first()
        if stop:
            return stop.order, stop

        batch_id = (data.get('batch_id') or '').strip()
        if batch_id:
            order = Order.objects.select_related('conversation').filter(ops_batch_id=batch_id).first()
            if order:
                stop = self._match_stop(order, order_number, stop_no)
                if stop is not None and stop.ops_order_number is None:
                    stop.ops_order_number = order_number
                return order, stop

        client = data.get('client') if isinstance(data.get('client'), dict) else {}
        phone = to_wa(client.get('phone'))
        if phone:
            conversation = Conversation.objects.filter(contact_phone=phone).first()
            if conversation:
                order = conversation.orders.order_by('-created_at').first()
                if order:
                    stop = self._match_stop(order, order_number, stop_no)
                    if stop is not None and stop.ops_order_number is None:
                        stop.ops_order_number = order_number
                    return order, stop

        return None, None

    @staticmethod
    def _apply_event(order, stop, data, status_value, now):
        payload_stops = data.get('stops') if isinstance(data.get('stops'), list) else []
        entry = next(
            (s for s in payload_stops
             if isinstance(s, dict) and _as_int(s.get('stop')) == stop.stop_no),
            None,
        )
        if entry:
            if entry.get('address'):
                stop.dest_address = str(entry['address'])
            if entry.get('description'):
                stop.description = str(entry['description'])[:500]
            if entry.get('service_type'):
                stop.service_type = str(entry['service_type'])[:40]

        stop.status = status_value
        stop.payload = {
            **(stop.payload or {}),
            'status_label': str(data.get('status_label') or ''),
            'courier_code': str(data.get('courier_code') or ''),
            'last_event': str(data.get('event') or ''),
        }
        stop.last_synced_at = now

        if status_value == 'cancelado':
            stop.canceled_at = stop.canceled_at or now
            reason = str(data.get('reason') or '').strip()
            if reason:
                stop.cancel_reason = reason[:500]

    @staticmethod
    def _apply_created(order, located_stop, data, status_value, now):
        """``order.created``: a comanda shares number and status across stops.

        When the payload carries more than one stop entry (comanda) every local
        stop is stamped with the shared order number and the header status; a
        single-stop payload only touches the located stop.
        """
        payload_stops = data.get('stops') if isinstance(data.get('stops'), list) else []
        entries = {}
        for entry in payload_stops:
            if not isinstance(entry, dict):
                continue
            index = _as_int(entry.get('stop'))
            if index:
                entries[index] = entry

        order_number = _as_int(data.get('order_number'))
        targets = (
            list(order.stops.order_by('stop_no', 'id'))
            if len(entries) > 1
            else [located_stop]
        )

        for stop in targets:
            entry = entries.get(stop.stop_no)
            if entry:
                if entry.get('address'):
                    stop.dest_address = str(entry['address'])
                if entry.get('description'):
                    stop.description = str(entry['description'])[:500]
                if entry.get('service_type'):
                    stop.service_type = str(entry['service_type'])[:40]
            if stop.ops_order_number is None and order_number:
                stop.ops_order_number = order_number
            stop.status = status_value
            stop.payload = {
                **(stop.payload or {}),
                'status_label': str(data.get('status_label') or ''),
                'courier_code': str(data.get('courier_code') or ''),
                'last_event': str(data.get('event') or ''),
            }
            stop.last_synced_at = now

        return targets
