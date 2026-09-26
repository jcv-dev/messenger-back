"""Integration endpoints (ops → Messager), plan §4.2.

Base path ``api/integrations/``. Every request requires an integration API key
(``X-Api-Key``) with the endpoint scope plus an HMAC signature
(``X-Signature`` / ``X-Timestamp``).
"""

import datetime
import logging
import time

from django.core.cache import cache
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
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
from .sla_notifications import SLA_ALERT_STATUSES, send_sla_alert
from .status_notifications import maybe_notify, resolve_transition

logger = logging.getLogger('api')

DOMII_TAG_NAME = 'Domii'
DOMII_TAG_COLOR = 'gray'
EVENT_DEDUPE_TTL = 86400  # 24 h

KNOWN_ORDER_STATUSES = frozenset({
    'nuevo', 'programado', 'disponible', 'asignado', 'confirmado', 'en_ruta',
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


def _event_created_at(data: dict):
    """When the push happened, used as the mirror's creation time."""
    value = parse_datetime(str(data.get('occurred_at') or ''))
    if value is None:
        return timezone.now()
    if timezone.is_naive(value):
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value


def _apply_stop_fields(stop: OrderStop, entry: dict) -> None:
    """Mirror the payload stop snapshot onto the local stop."""
    if entry.get('address'):
        stop.dest_address = str(entry['address'])
    if entry.get('description'):
        stop.description = str(entry['description'])[:500]
    if entry.get('observation') is not None:
        stop.observation = str(entry['observation'] or '')[:500]
    if entry.get('service_type'):
        stop.service_type = str(entry['service_type'])[:40]
    if entry.get('price') is not None:
        stop.price = _as_int(entry.get('price'))
    if entry.get('lat') is not None:
        stop.lat = entry['lat']
    if entry.get('lng') is not None:
        stop.lng = entry['lng']


def _client_id(data) -> int | None:
    client = data.get('client') if isinstance(data.get('client'), dict) else {}
    try:
        return int(client.get('id'))
    except (TypeError, ValueError):
        return None


def _event_stop_no(data) -> int | None:
    """Stop index from the event payload (comandas share one order number)."""
    try:
        stop_no = int(data.get('stop'))
    except (TypeError, ValueError):
        return None
    return stop_no if stop_no > 0 else None


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

    # Un pedido programado sigue programado hasta que ops lo active, salvo
    # cancelación total. La activación llega como una parada ya
    # asignada/disponible, así que el estado se destraba.
    if (order.status or '').strip().lower() == 'programado' and not all_canceled:
        activated = any(s not in ('nuevo', '') for s in normalized)
        if not activated:
            new_status = 'programado'

    order.status = new_status
    total = sum((s.price or 0) for s in order.stops.all())
    # Keep the last known total when the payload has no per-stop prices yet
    # (events sent before the price enrichment reached ops): zeroing a real
    # total would make the card show $0.
    if total or not order.total:
        order.total = total
    return new_status


def adopt_order(conversation, data, order_number: int):
    """Mirror an ops batch that was not created from the Messager.

    Panel/mobile orders only reach us as events; the first one creates the
    local batch and its stops so the conversation's ``Pedidos activos`` card
    can show it (and refresh/cancel/notify work like any other order). A batch
    adopted after the fact never messages the client about a transition that
    already happened (entregado/cancelado are seeded into
    ``notified_statuses``); later ones notify normally.

    Used by the webhook (``OrderEventsView._locate``) and by the link-time
    backfill (``adoption.sync_active_orders``), so both share one mirror shape.
    """
    # Another event may have adopted the batch already.
    existing = (
        OrderStop.objects
        .select_related('order')
        .filter(ops_order_number=order_number)
        .first()
    )
    if existing is not None:
        return existing.order, existing

    client = data.get('client') if isinstance(data.get('client'), dict) else {}
    courier = data.get('courier') if isinstance(data.get('courier'), dict) else {}
    event_status = str(data.get('status') or '').strip().lower() or 'disponible'
    scheduled_raw = data.get('scheduled_at')
    scheduled_for = parse_datetime(str(scheduled_raw)) if scheduled_raw else None
    now = timezone.now()

    entries = [e for e in (data.get('stops') or []) if isinstance(e, dict)]
    if not entries:
        entries = [{'stop': 1, 'status': event_status}]

    order = Order.objects.create(
        conversation=conversation,
        ops_batch_id=str(data.get('batch_id') or '')[:32] or None,
        ops_client_user_id=_client_id(data),
        ops_courier_user_id=_as_int(courier.get('id')) or None,
        courier_name=str(courier.get('name') or '')[:255],
        courier_code=str(data.get('courier_code') or courier.get('code') or '')[:12],
        client_name=str(client.get('name') or '')[:255],
        origin_address=str(data.get('origin') or ''),
        total=_as_int(data.get('total')),
        status=event_status,
        scheduled_for=scheduled_for,
        payload={'ops_event': data, 'adopted': True},
        last_synced_at=now,
    )
    # ``auto_now_add`` overrides created_at on create; the card's "hace X"
    # should read the push time, not the adoption moment.
    created_at = _event_created_at(data)
    Order.objects.filter(pk=order.pk).update(created_at=created_at)
    order.created_at = created_at

    wanted = _event_stop_no(data)
    located = None
    for index, entry in enumerate(entries, start=1):
        stop_no = _as_int(entry.get('stop')) or index
        stop = OrderStop.objects.create(
            order=order,
            stop_no=stop_no,
            ops_order_number=order_number,
            service_type=str(entry.get('service_type') or '')[:40],
            dest_address=str(entry.get('address') or ''),
            lat=entry.get('lat'),
            lng=entry.get('lng'),
            description=str(entry.get('description') or '')[:500],
            observation=str(entry.get('observation') or '')[:500],
            price=_as_int(entry.get('price')),
            status=str(entry.get('status') or event_status).strip().lower(),
            payload={
                'status_label': str(data.get('status_label') or ''),
                'courier_code': str(data.get('courier_code') or courier.get('code') or ''),
                'last_event': str(data.get('event') or ''),
            },
            last_synced_at=now,
        )
        if located is None or (wanted is not None and stop_no == wanted):
            located = stop

    recompute_order_status(order)
    updates = ['status', 'total']
    transition = resolve_transition(order)
    if transition in ('entregado', 'cancelado'):
        order.notified_statuses = [transition]
        updates.append('notified_statuses')
    order.save(update_fields=updates)
    return order, located


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
        stop_no = _event_stop_no(data) or 0
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
            order.ops_client_user_id = order.ops_client_user_id or _client_id(data)
            if data.get('origin'):
                order.origin_address = str(data['origin'])
            client = data.get('client') if isinstance(data.get('client'), dict) else {}
            if client.get('name'):
                order.client_name = str(client['name'])[:255]
            # Phase 8: el código corto del domi viaja en cada evento; con él el
            # card muestra quién tiene el pedido aunque no se haya elegido acá.
            courier = data.get('courier') if isinstance(data.get('courier'), dict) else {}
            courier_code = str(
                data.get('courier_code') or courier.get('code') or ''
            ).strip()[:12]
            # Un pedido que ops libera (p. ej. el domi programado ya no estaba
            # disponible): el payload no trae courier y hay que soltar el espejo.
            if status_value in ('disponible', 'nuevo') and not courier.get('id') and not courier_code:
                order.ops_courier_user_id = None
                order.courier_name = ''
                order.courier_code = ''
            else:
                if courier_code:
                    order.courier_code = courier_code
                if courier.get('id'):
                    order.ops_courier_user_id = _as_int(courier['id']) or None
                if courier.get('name'):
                    order.courier_name = str(courier['name'])[:255]
            scheduled_raw = data.get('scheduled_at')
            if scheduled_raw:
                parsed_scheduled = parse_datetime(str(scheduled_raw))
                if parsed_scheduled is not None:
                    order.scheduled_for = parsed_scheduled
            # Un pedido programado pre-asignado que ops liberó al activarse.
            if (
                status_value in ('disponible', 'nuevo')
                and (data.get('scheduled_mode') == 'manual')
            ):
                order.scheduled_released = True
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

    def _match_conversation_stop(
        self, conversation, order_number: int, stop_no: int | None = None,
    ):
        """The local stop for ``order_number`` anywhere in the conversation.

        Newest order first, so a stop that already carries the number is always
        preferred over the unnumbered fallback (a local batch still waiting for
        the ops response).
        """
        stops = (
            OrderStop.objects
            .select_related('order')
            .filter(order__conversation=conversation)
            .order_by('-order__created_at', '-order_id', 'stop_no')
        )
        numbered = stops.filter(ops_order_number=order_number)
        if stop_no is not None:
            stop = numbered.filter(stop_no=stop_no).first()
            if stop:
                return stop
        stop = numbered.first()
        if stop:
            return stop

        unnumbered = stops.filter(ops_order_number__isnull=True)
        if stop_no is not None:
            stop = unnumbered.filter(stop_no=stop_no).first()
            if stop:
                return stop
        return unnumbered.first()

    def _linked_conversation(self, data) -> Conversation | None:
        """Conversation linked to the event's ops client.

        Phone-less clients are linked by hand (name search), so the ops client
        id is the only way their events can find the conversation. With several
        linked conversations (the same client writing from different numbers)
        the phone in the payload wins; otherwise the most recently active one
        owns the batch.
        """
        client_id = _client_id(data)
        if not client_id:
            return None

        linked = Conversation.objects.filter(ops_client_user_id=client_id)
        client = data.get('client') if isinstance(data.get('client'), dict) else {}
        phone = to_wa(client.get('phone'))
        if phone:
            preferred = linked.filter(contact_phone=phone).first()
            if preferred:
                return preferred
        return linked.order_by(F('last_message_at').desc(nulls_last=True), '-id').first()

    def _locate(self, data, order_number: int):
        stop_no = _event_stop_no(data)

        # 1) A stop of a batch already mirrored (created from the Messager, or
        #    adopted by an earlier event).
        qs = (
            OrderStop.objects
            .select_related('order', 'order__conversation')
            .filter(ops_order_number=order_number)
        )
        stop = qs.filter(stop_no=stop_no).first() if stop_no is not None else None
        stop = stop or qs.first()
        if stop:
            return stop.order, stop

        # 2) A local batch created from the Messager whose ops response has not
        #    assigned the order numbers yet.
        batch_id = (data.get('batch_id') or '').strip()
        if batch_id:
            order = Order.objects.select_related('conversation').filter(ops_batch_id=batch_id).first()
            if order:
                stop = self._match_stop(order, order_number, stop_no)
                if stop is not None and stop.ops_order_number is None:
                    stop.ops_order_number = order_number
                return order, stop

        # 3) The conversation ↔ ops-client link (manual links for phone-less
        #    clients are the only way panel-created orders find their chat),
        #    then the client phone.
        conversation = self._linked_conversation(data)
        if conversation is None:
            client = data.get('client') if isinstance(data.get('client'), dict) else {}
            phone = to_wa(client.get('phone'))
            conversation = (
                Conversation.objects.filter(contact_phone=phone).first()
                if phone else None
            )

        if conversation is not None:
            stop = self._match_conversation_stop(conversation, order_number, stop_no)
            if stop is not None:
                if stop.ops_order_number is None:
                    stop.ops_order_number = order_number
                return stop.order, stop
            return adopt_order(conversation, data, order_number)

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
            _apply_stop_fields(stop, entry)

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
                _apply_stop_fields(stop, entry)
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


class SlaAlertView(IntegrationAPIView):
    """POST /api/integrations/notifications/sla/ — courier SLA reminder.

    Ops' ``domii:sla-alerts`` command sends one per courier alert; the Messager
    answers with the ``aviso_sla`` utility template over WhatsApp. Idempotent on
    ``alert_key`` (24 h), so ops queue retries never duplicate the message.
    """

    integration_scope = SCOPE_ORDERS_WRITE

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}

        alert_key = str(data.get('alert_key') or '').strip()
        if not alert_key or len(alert_key) > 200:
            return Response(
                {'ok': False, 'error': 'alert_key requerido.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        courier = data.get('courier') if isinstance(data.get('courier'), dict) else {}
        order = data.get('order') if isinstance(data.get('order'), dict) else {}

        try:
            order_number = int(order.get('order_number'))
        except (TypeError, ValueError):
            return Response(
                {'ok': False, 'error': 'order.order_number requerido.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        status_value = str(order.get('status') or '').strip().lower()
        if status_value not in SLA_ALERT_STATUSES:
            return Response(
                {'ok': False, 'error': f'order.status inválido: {status_value}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            minutes = max(0, int(order.get('minutes')))
        except (TypeError, ValueError):
            minutes = 0
        try:
            threshold = int(order.get('threshold'))
        except (TypeError, ValueError):
            threshold = None

        result = send_sla_alert({
            'alert_key': alert_key,
            'courier': {
                'id': courier.get('id'),
                'name': str(courier.get('name') or '')[:255],
                'phone': courier.get('phone'),
            },
            'order': {
                'order_number': order_number,
                'status': status_value,
                'minutes': minutes,
                'threshold': threshold,
                # Pedidos/domis de prueba: el módulo los omite (test_order).
                'is_test': order.get('is_test', False),
            },
        })

        return Response({'ok': True, **result})
