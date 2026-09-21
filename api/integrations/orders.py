"""Order creation and sync between conversations and ops (plan §4.3, Phase 3).

The flow mirrors the plan:

1. Resolve the client (explicit id → conversation link → phone match → new by
   explicit name).
2. Create the local ``Order`` + ``OrderStop`` rows first (status ``pending``).
3. Call ops once with all stops as ``items[]`` and an idempotency key.
4. Store ``ops_batch_id`` / the shared ``ops_order_number`` and mark the batch
   ``disponible``.
5. Link the conversation when it was not linked yet (``match_source='order'``).
6. Optionally send the confirmation message (``BotConfig.order_created_message``).
7. Publish SSE.

An ops failure marks the batch ``failed`` and keeps the payload, so the agent
can retry with the same idempotency key without creating duplicates in ops.
"""

import logging
from decimal import Decimal, InvalidOperation

from django.core.cache import cache
from django.utils import timezone

from api.models import Order, OrderStop

from . import ops
from .clients import MATCH_SOURCE_ORDER, fetch_client_by_phone, link_conversation
from .phones import to_ops
from .services import OPS_SERVICE_KEYS, normalize_ops_key

logger = logging.getLogger('api')

IDEMPOTENCY_TTL = 600  # 10 min (plan §4.3)
PENDING_MARKER = '__pending__'
MAX_STOPS = 10

DEFAULT_ORDER_CREATED_MESSAGE = (
    '✅ Pedido {order_numbers} recibido. '
    'Un domiciliario lo aceptará pronto.'
)

FINISHED_STATUSES = frozenset({'entregado', 'cancelado', 'failed'})

SERVICES_CACHE_KEY = 'ops:services:catalog'
CATALOG_CACHE_TTL = 600  # 10 min (plan §4.3)

# Fallback when the ops catalog cannot be fetched: only these need an address.
DEFAULT_ADDRESS_SERVICES = frozenset({'domicilio', 'mensajeria'})


def fetch_services_catalog() -> list:
    """Return the ops service catalog, cached 10 min; ``[]`` when unavailable."""
    try:
        cached = cache.get(SERVICES_CACHE_KEY)
    except Exception:
        cached = None
    if cached is not None:
        return cached if isinstance(cached, list) else []

    if not ops.is_configured():
        return []

    try:
        data = ops.get_services()
    except ops.OpsAPIError as exc:
        logger.warning('Service catalog unavailable: %s', exc)
        return []

    services = data.get('services') if isinstance(data, dict) else []
    if not isinstance(services, list):
        services = []
    try:
        cache.set(SERVICES_CACHE_KEY, services, CATALOG_CACHE_TTL)
    except Exception:
        pass
    return services


def service_requires_address(service_type: str, catalog: list | None = None) -> bool:
    """Whether a stop of ``service_type`` needs a destination address."""
    catalog = catalog if catalog is not None else fetch_services_catalog()
    for item in catalog:
        if not isinstance(item, dict):
            continue
        if normalize_ops_key(item.get('key')) == service_type:
            return bool(item.get('requires_address'))
    return service_type in DEFAULT_ADDRESS_SERVICES


class OrderValidationError(Exception):
    """The request cannot be turned into an ops order."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.message = message
        self.field = field


class OrderInProgressError(Exception):
    """A request with the same idempotency key is still in flight."""

    def __init__(self, message='Pedido en proceso, intenta de nuevo en unos segundos.'):
        super().__init__(message)
        self.message = message


class OrderCreationFailed(Exception):
    """Ops rejected the order; the local batch was kept as ``failed``."""

    def __init__(self, order: Order, message: str):
        super().__init__(message)
        self.order = order
        self.message = message


def format_cop(value) -> str:
    """Colombian peso without decimals: 14500 → ``$14.500``."""
    try:
        amount = int(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        amount = 0
    return f'${amount:,}'.replace(',', '.')


def order_numbers_text(order: Order) -> str:
    """Unique order numbers, e.g. ``#1234`` (a comanda shares one number)."""
    seen = []
    for stop in order.stops.order_by('stop_no', 'id'):
        if stop.ops_order_number and stop.ops_order_number not in seen:
            seen.append(stop.ops_order_number)
    return ', '.join(f'#{int(number)}' for number in seen)


def first_order_number_text(order: Order) -> str:
    """First stop order number, e.g. ``#1234`` (for single-code templates)."""
    for stop in order.stops.order_by('stop_no', 'id'):
        if stop.ops_order_number:
            return f'#{int(stop.ops_order_number)}'
    return f'#{order.id}'


def get_order_created_message() -> str:
    from api.bot.config import get_config

    template = get_config('order_created_message', DEFAULT_ORDER_CREATED_MESSAGE)
    if not isinstance(template, str) or not template.strip():
        return DEFAULT_ORDER_CREATED_MESSAGE
    return template


def _render_template(template: str, values: dict) -> str:
    out = template
    for key, value in values.items():
        out = out.replace('{' + key + '}', str(value))
    return out


def build_confirmation_text(order: Order) -> str:
    template = get_order_created_message()
    return _render_template(template, {
        'order_numbers': order_numbers_text(order) or f'#{order.id}',
        'order_number': first_order_number_text(order),
        'total': format_cop(order.total),
        'count': order.stops.count(),
        'client_name': order.client_name or '',
        'origin': order.origin_address or '',
    })


def resolve_client(conversation, client_data: dict | None) -> dict:
    """Resolve the ops client for this order (plan §4.3 step 2)."""
    client_data = client_data or {}

    explicit_id = client_data.get('ops_client_user_id')
    if explicit_id:
        # The agent picked a search result: keep its data as the link snapshot
        # so the client card shows the phone/address without another lookup.
        name = (client_data.get('name') or '').strip()
        phone = to_ops(client_data.get('phone'))
        address = (client_data.get('address') or '').strip()
        snapshot = None
        if name or phone or address:
            snapshot = {
                'id': int(explicit_id),
                'name': name[:255],
                'phone': phone,
                'address': address[:500],
            }
        return {'user_id': int(explicit_id), 'name': '', 'phone': '', 'snapshot': snapshot}

    if conversation.ops_client_user_id:
        snapshot = conversation.ops_client_snapshot or {}
        return {
            'user_id': conversation.ops_client_user_id,
            'name': snapshot.get('name') or '',
            'phone': snapshot.get('phone') or '',
            'snapshot': snapshot or None,
        }

    if conversation.contact_phone:
        found = fetch_client_by_phone(conversation.contact_phone)
        if found:
            return {
                'user_id': found['id'], 'name': found.get('name') or '',
                'phone': found.get('phone') or '', 'snapshot': found,
            }

    name = (client_data.get('name') or '').strip()
    if not name:
        # Ops creates the client from name + phone when no match exists, exactly
        # like its own order creation; fall back to the conversation identity.
        name = (
            conversation.custom_name
            or conversation.contact_name
            or conversation.whatsapp_username
            or ''
        ).strip()
    phone = to_ops(client_data.get('phone') or conversation.contact_phone)
    if name:
        return {'user_id': None, 'name': name[:120], 'phone': phone, 'snapshot': None}

    raise OrderValidationError(
        'No se pudo identificar al cliente. Vincula un cliente registrado o '
        'indica un nombre para crearlo.',
        field='client',
    )


def validate_stops(stops_data: list, catalog: list | None = None) -> list:
    """Normalize and validate the ``stops[]`` request payload."""
    if not isinstance(stops_data, list) or not stops_data:
        raise OrderValidationError('Agrega al menos una parada.', field='stops')
    if len(stops_data) > MAX_STOPS:
        raise OrderValidationError(f'Máximo {MAX_STOPS} paradas por pedido.', field='stops')

    normalized = []
    for index, stop in enumerate(stops_data, start=1):
        service_type = normalize_ops_key(stop.get('service_type'))
        if service_type not in OPS_SERVICE_KEYS:
            raise OrderValidationError(
                f'Tipo de servicio inválido en la parada {index}.', field='stops',
            )

        dest_address = (stop.get('dest_address') or '').strip()
        description = (stop.get('description') or '').strip()
        if service_requires_address(service_type, catalog) and not dest_address:
            raise OrderValidationError(
                f'La parada {index} necesita una dirección de destino.', field='stops',
            )

        try:
            price = int(Decimal(str(stop.get('price') or 0)))
        except (InvalidOperation, TypeError, ValueError):
            price = 0
        if price <= 0:
            raise OrderValidationError(
                f'La parada {index} necesita un precio mayor a cero.', field='stops',
            )

        normalized.append({
            'stop_no': index,
            'service_type': service_type,
            'dest_address': dest_address[:500],
            'description': description[:500],
            'observation': (stop.get('observation') or '').strip()[:500],
            'lat': stop.get('lat'),
            'lng': stop.get('lng'),
            'price': price,
        })

    return normalized


def build_ops_payload(order: Order, client_ref: dict, data: dict) -> dict:
    payload = {
        'origin_address': (data.get('origin_address') or '').strip()[:255],
        'items': [
            {
                'service_type': stop.service_type,
                'dest_address': stop.dest_address,
                'description': stop.description,
                'observation': stop.observation,
                'precio_total': int(stop.price),
            }
            for stop in order.stops.order_by('stop_no', 'id')
        ],
    }
    if client_ref.get('user_id'):
        payload['client_user_id'] = int(client_ref['user_id'])
    else:
        payload['client_name'] = client_ref.get('name') or ''
        if client_ref.get('phone'):
            payload['client_phone'] = client_ref['phone']

    # Phase 8: con un domi elegido la comanda se asigna de inmediato en modo
    # manual (ops valida deuda, suspende su turno y lo notifica). Sin domi el
    # pedido sigue saliendo libre, como hasta ahora.
    courier = data.get('courier') or {}
    if data.get('assignment') == 'manual' and courier.get('ops_courier_user_id'):
        payload['courier_user_id'] = int(courier['ops_courier_user_id'])
        payload['mode'] = 'manual'
    else:
        payload['mode'] = 'libre'
    return payload


def _ops_validation_message(exc) -> str:
    """Mensaje legible de un 422 de ops (deuda, courier inválido, etc.)."""
    payload = exc.payload if isinstance(exc.payload, dict) else {}
    details = payload.get('details')
    if isinstance(details, dict):
        for key in ('courier_user_id', 'items', 'client_user_id', 'origin_address'):
            values = details.get(key)
            if isinstance(values, list) and values:
                return str(values[0])[:300]
            if isinstance(values, str) and values.strip():
                return values[:300]
    error = payload.get('error')
    if isinstance(error, str) and error.strip():
        return error.strip()[:300]
    return getattr(exc, 'message', '') or 'Ops rechazó el pedido.'


def _response_order_numbers(response: dict, stop_count: int) -> list:
    """Order numbers from an ops response.

    Comandas answer ``order_numbers: [1234]`` for N stops; legacy per-stop
    batches answer one number per stop. A single number is shared by every stop.
    """
    numbers = [int(n) for n in (response.get('order_numbers') or []) if n]
    if not numbers and response.get('order_number'):
        try:
            numbers = [int(response['order_number'])]
        except (TypeError, ValueError):
            numbers = []
    if len(numbers) == 1 and stop_count > 1:
        return numbers * stop_count
    return numbers


def apply_ops_response(order: Order, response: dict, client_ref: dict) -> Order:
    """Mirror the ops creation response onto the local batch."""
    now = timezone.now()
    client = response.get('client') or {}
    courier = response.get('courier') if isinstance(response.get('courier'), dict) else {}

    order.ops_batch_id = str(response.get('batch_id') or '')[:32] or None
    order.status = response.get('status') or 'disponible'
    order.ops_client_user_id = client.get('id') or client_ref.get('user_id') or None
    order.client_name = (client.get('name') or client_ref.get('name') or '')[:255]
    if courier.get('id'):
        order.ops_courier_user_id = int(courier['id'])
    if courier.get('name'):
        order.courier_name = str(courier['name'])[:255]
    if courier.get('code'):
        order.courier_code = str(courier['code'])[:12]
    order.last_synced_at = now

    stops = list(order.stops.order_by('stop_no', 'id'))
    numbers = _response_order_numbers(response, len(stops))
    for stop, number in zip(stops, numbers):
        stop.ops_order_number = number
        stop.status = order.status
        stop.last_synced_at = now
        stop.save(update_fields=['ops_order_number', 'status', 'last_synced_at'])

    source = order.payload if isinstance(order.payload, dict) else {}
    source['ops'] = response
    order.payload = source
    order.total = sum((stop.price or 0) for stop in stops)
    order.save(update_fields=[
        'ops_batch_id', 'status', 'ops_client_user_id', 'client_name',
        'ops_courier_user_id', 'courier_name', 'courier_code',
        'last_synced_at', 'payload', 'total', 'updated_at',
    ])
    return order


def link_order_client(conversation, order: Order, client_ref: dict) -> None:
    """Persist the conversation ↔ client link once ops answered with an id."""
    if not order.ops_client_user_id or conversation.ops_client_user_id:
        return
    snapshot = client_ref.get('snapshot') or {}
    if not snapshot and order.client_name:
        snapshot = {
            'id': order.ops_client_user_id,
            'name': order.client_name,
            'phone': client_ref.get('phone') or '',
            'address': conversation.ops_client_snapshot.get('address', '')
            if isinstance(conversation.ops_client_snapshot, dict) else '',
        }
    link_conversation(
        conversation, order.ops_client_user_id, snapshot=snapshot,
        source=MATCH_SOURCE_ORDER,
    )


def _publish_updates(conversation, order: Order) -> None:
    from api.integrations.views import _publish_order_updated
    from api.views import publish_conversation_update

    try:
        publish_conversation_update(conversation)
    except Exception:
        logger.exception('Failed to publish conversation.updated after order create')
    _publish_order_updated(order)


def create_order(conversation, data: dict, user):
    """Create the local mirror, call ops once and sync the response.

    Returns ``(order, created)``. ``created`` is False for an idempotent replay.
    Raises ``OrderValidationError``, ``OrderInProgressError`` or
    ``OrderCreationFailed`` (the local batch is then ``failed``).
    """
    stops_data = validate_stops(data.get('stops') or [])
    client_ref = resolve_client(conversation, data.get('client'))

    # Phase 8: domi elegido en el selector (modo manual). Sin domi el pedido
    # sale libre y ops lo reparte como siempre.
    assignment = (data.get('assignment') or 'libre').strip().lower()
    courier_ref = data.get('courier') or {}
    courier_id = courier_ref.get('ops_courier_user_id')

    if assignment == 'manual':
        if not courier_id:
            raise OrderValidationError(
                'Selecciona un domiciliario para asignar el pedido.', field='courier',
            )
        courier_id = int(courier_id)
    else:
        # ``libre``: un courier suelto en el payload se ignora, el pedido nunca
        # se asigna por accidente.
        assignment = 'libre'
        courier_id = None
        courier_ref = {}

    # Normalized copy for ``build_ops_payload``: a stray courier with
    # ``assignment=libre`` must never reach ops as a manual assignment.
    data = {**data, 'assignment': assignment, 'courier': courier_ref or None}

    idem_key = (data.get('idempotency_key') or '').strip()[:64]
    cache_key = f'order:idem:{user.id}:{idem_key}' if idem_key else None
    if cache_key:
        existing = cache.get(cache_key)
        if existing == PENDING_MARKER:
            raise OrderInProgressError()
        if existing:
            order = Order.objects.filter(id=existing).first()
            if order is not None:
                return order, False
        try:
            cache.add(cache_key, PENDING_MARKER, IDEMPOTENCY_TTL)
        except Exception:
            logger.warning('Idempotency cache unavailable for key %s', idem_key)

    payload = {
        'request': {
            'origin_address': (data.get('origin_address') or '').strip(),
            'payment_method': data.get('payment_method') or 'efectivo',
            'profile': data.get('profile') or 'usuario_final',
            'tools': data.get('tools') or [],
            'acompanante': bool(data.get('acompanante')),
            'send_confirmation': bool(data.get('send_confirmation', True)),
            'idempotency_key': idem_key,
            'assignment': assignment,
            'courier': (
                {
                    'ops_courier_user_id': courier_id,
                    'name': (courier_ref.get('name') or '')[:255],
                    'code': (courier_ref.get('code') or '')[:12],
                }
                if courier_id else None
            ),
        },
        'client_ref': client_ref,
    }
    draft_meta = data.get('draft')
    if draft_meta:
        # Phase 6: provenance of the AI-prefilled order sheet (audit/metrics).
        payload['draft'] = draft_meta

    order = Order.objects.create(
        conversation=conversation,
        ops_client_user_id=client_ref.get('user_id') or None,
        ops_courier_user_id=courier_id or None,
        courier_name=(courier_ref.get('name') or '')[:255],
        courier_code=(courier_ref.get('code') or '')[:12],
        client_name=(client_ref.get('name') or '')[:255],
        origin_address=(data.get('origin_address') or '').strip()[:500],
        payment_method=data.get('payment_method') or 'efectivo',
        profile=data.get('profile') or 'usuario_final',
        tools=data.get('tools') or [],
        acompanante=bool(data.get('acompanante')),
        total=sum(stop['price'] for stop in stops_data),
        status='pending',
        source=data.get('source') or 'agent',
        created_by=user,
        payload=payload,
    )
    for stop in stops_data:
        OrderStop.objects.create(
            order=order,
            stop_no=stop['stop_no'],
            service_type=stop['service_type'],
            dest_address=stop['dest_address'],
            lat=stop['lat'],
            lng=stop['lng'],
            description=stop['description'],
            observation=stop['observation'],
            price=stop['price'],
            status='pending',
            payload={'request': stop},
        )

    ops_payload = build_ops_payload(order, client_ref, data)
    try:
        response = ops.create_order(ops_payload, idempotency_key=idem_key or None)
    except ops.OpsAPIError as exc:
        if cache_key:
            cache.delete(cache_key)
        if exc.status_code == 422:
            # Validación de ops (deuda del domi, courier inválido…): es un dato
            # corregible, no un pedido fallido. Se descarta el borrador local
            # para que el agente corrija en la hoja y reintente.
            order.delete()
            logger.info('Order draft discarded after ops validation error: %s', exc)
            raise OrderValidationError(_ops_validation_message(exc), field='courier') from exc
        order.status = 'failed'
        order.save(update_fields=['status', 'updated_at'])
        logger.warning('Order %s failed at ops: %s', order.id, exc)
        raise OrderCreationFailed(order, exc.message) from exc

    if not isinstance(response, dict) or not response.get('ok', False):
        order.status = 'failed'
        order.save(update_fields=['status', 'updated_at'])
        if cache_key:
            cache.delete(cache_key)
        raise OrderCreationFailed(order, 'Ops no confirmó la creación del pedido.')

    apply_ops_response(order, response, client_ref)
    link_order_client(conversation, order, client_ref)

    if data.get('send_confirmation', True):
        send_confirmation(conversation, order)

    if cache_key:
        try:
            cache.set(cache_key, order.id, IDEMPOTENCY_TTL)
        except Exception:
            pass

    _publish_updates(conversation, order)
    return order, True


def send_confirmation(conversation, order: Order) -> None:
    """Send the configurable confirmation text right away."""
    if not conversation.contact_phone:
        return
    try:
        from .notify import send_text

        send_text(conversation, build_confirmation_text(order), sender_name='Sistema')
    except Exception:
        logger.exception('Failed to send order confirmation for order %s', order.id)


def refresh_order(order: Order) -> Order:
    """Pull the latest stop statuses from ops and recompute the batch.

    A comanda shares one order number across stops, so ops is called once per
    unique number and each local stop reads its own status from the response
    ``stops[]`` entry. The snapshot also refreshes price/coords/content, so a
    panel edit that didn't push an event still lands on Refrescar.
    Raises ``ops.OpsAPIError`` when ops cannot be reached.
    """
    from .views import _apply_stop_fields, recompute_order_status

    now = timezone.now()
    groups: dict[int, list] = {}
    for stop in order.stops.order_by('stop_no', 'id'):
        if stop.ops_order_number:
            groups.setdefault(int(stop.ops_order_number), []).append(stop)

    courier_code = ''
    for number, group in groups.items():
        data = ops.get_order(number)
        if not isinstance(data, dict) or not data.get('ok'):
            continue

        # El detalle de ops trae el código corto del domi (`sn42`); el nombre
        # solo se conoce al crear desde el selector.
        courier = data.get('courier')
        if isinstance(courier, str) and courier.strip():
            courier_code = courier.strip()[:12]
        elif isinstance(courier, dict) and courier.get('code'):
            courier_code = str(courier['code']).strip()[:12]

        entries = {}
        for entry in (data.get('stops') or []):
            if not isinstance(entry, dict):
                continue
            try:
                index = int(entry.get('stop'))
            except (TypeError, ValueError):
                index = None
            if index:
                entries[index] = entry

        for stop in group:
            entry = entries.get(stop.stop_no)
            if entry:
                _apply_stop_fields(stop, entry)
                if entry.get('status'):
                    stop.status = str(entry['status'])[:20]
            elif not entries:
                # Response without a stops snapshot: fall back to the header.
                stop.status = (data.get('status') or stop.status)[:20]
            stop.last_synced_at = now
            payload = stop.payload if isinstance(stop.payload, dict) else {}
            payload['ops'] = data
            stop.payload = payload
            stop.save(update_fields=[
                'status', 'service_type', 'dest_address', 'description',
                'observation', 'price', 'lat', 'lng', 'last_synced_at', 'payload',
            ])

    # ``order`` may carry a stale prefetched stops cache (the view prefetches).
    if hasattr(order, '_prefetched_objects_cache'):
        order._prefetched_objects_cache.pop('stops', None)

    recompute_order_status(order)
    order.last_synced_at = now
    if courier_code:
        order.courier_code = courier_code
    order.save(update_fields=[
        'status', 'total', 'last_synced_at', 'updated_at', 'courier_code',
    ])
    _publish_updates(order.conversation, order)
    return order


# ---------------------------------------------------------------------------
#  Cancellation (Phase 4, plan §6.3)
# ---------------------------------------------------------------------------

CANCEL_REASON_MIN = 3
CANCEL_REASON_MAX = 500

# A stop in one of these states cannot be canceled again.
TERMINAL_STOP_STATUSES = frozenset({'entregado', 'cancelado'})


class OrderCancelError(Exception):
    """The cancellation cannot be applied (validation or local state)."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.message = message
        self.field = field


def validate_cancel_reason(value) -> str:
    """Return the trimmed reason or raise ``OrderCancelError``."""
    reason = (value or '').strip()
    if len(reason) < CANCEL_REASON_MIN:
        raise OrderCancelError(
            f'El motivo de cancelación es obligatorio (mínimo {CANCEL_REASON_MIN} caracteres).',
            field='reason',
        )
    return reason[:CANCEL_REASON_MAX]


def is_active_stop(stop) -> bool:
    return (stop.status or '').strip().lower() not in TERMINAL_STOP_STATUSES


def _ordered_stops(order) -> list:
    """Stops in display order, reusing the prefetched cache when present."""
    stops = list(order.stops.all())
    stops.sort(key=lambda stop: (stop.stop_no or 0, stop.id or 0))
    return stops


def _active_stops(order) -> list:
    return [stop for stop in _ordered_stops(order) if is_active_stop(stop)]


def _active_order_numbers(stops: list) -> list:
    numbers = []
    for stop in stops:
        if not stop.ops_order_number:
            continue
        number = int(stop.ops_order_number)
        if number not in numbers:
            numbers.append(number)
    return numbers


def _record_cancel(order: Order, text: str) -> None:
    """Append the cancellation line to ``Order.payload.cancel`` (plan §6.3)."""
    payload = order.payload if isinstance(order.payload, dict) else {}
    entries = payload.get('cancel')
    if not isinstance(entries, list):
        entries = []
    entries.append(text)
    payload['cancel'] = entries
    order.payload = payload


def _audit_cancel(order: Order, user, detail: str) -> None:
    from api.views import _log_audit

    _log_audit(user, order.conversation, 'cancel_order', detail)


def _drop_prefetched_stops(order: Order) -> None:
    if hasattr(order, '_prefetched_objects_cache'):
        order._prefetched_objects_cache.pop('stops', None)


def cancel_order(order: Order, reason: str, user) -> dict:
    """Cancel every active stop of the pedido (plan §6.3).

    Ops is called once per unique order number: a comanda shares one number, so
    a whole multi-stop pedido is a single ops call. Local stops without an ops
    number (failed batches) are canceled locally. Returns
    ``{'canceled': n, 'order': order}`` and raises ``ops.OpsAPIError`` when ops
    rejects the cancellation (local state is left untouched).
    """
    from .views import recompute_order_status

    active = _active_stops(order)
    if not active:
        # Already canceled/delivered: idempotent no-op, no extra note.
        return {'canceled': 0, 'order': order}

    for number in _active_order_numbers(active):
        ops.cancel_order(number, reason)

    now = timezone.now()
    for stop in active:
        stop.status = 'cancelado'
        stop.canceled_at = stop.canceled_at or now
        stop.cancel_reason = reason[:500]
        stop.last_synced_at = now
        stop.save(update_fields=['status', 'canceled_at', 'cancel_reason', 'last_synced_at'])

    _record_cancel(order, f'[Pedido] Cancelado: {reason}')
    recompute_order_status(order)
    order.last_synced_at = now
    order.save(update_fields=['status', 'total', 'payload', 'last_synced_at', 'updated_at'])
    _drop_prefetched_stops(order)

    _audit_cancel(
        order,
        user,
        f'{order_numbers_text(order) or f"Pedido {order.id}"} cancelado: {reason}',
    )
    _publish_updates(order.conversation, order)
    return {'canceled': len(active), 'order': order}


def cancel_stop(order: Order, stop: OrderStop, reason: str, user) -> dict:
    """Cancel one parada of a pedido (plan §6.3).

    A comanda shares one ops order number, so the stop-level ops endpoint is
    used to cancel only this parada. Already terminal stops are a no-op
    (idempotent). Raises ``ops.OpsAPIError`` when ops rejects the cancellation.
    """
    from .views import recompute_order_status

    if not is_active_stop(stop):
        return {'canceled': 0, 'order': order, 'stop': stop}

    if stop.ops_order_number:
        ops.cancel_order_stop(int(stop.ops_order_number), stop.stop_no, reason)

    now = timezone.now()
    stop.status = 'cancelado'
    stop.canceled_at = stop.canceled_at or now
    stop.cancel_reason = reason[:500]
    stop.last_synced_at = now
    stop.save(update_fields=['status', 'canceled_at', 'cancel_reason', 'last_synced_at'])

    _record_cancel(order, f'[Pedido] Parada {stop.stop_no} cancelada: {reason}')
    recompute_order_status(order)
    order.last_synced_at = now
    order.save(update_fields=['status', 'total', 'payload', 'last_synced_at', 'updated_at'])
    _drop_prefetched_stops(order)

    _audit_cancel(
        order,
        user,
        f'{order_numbers_text(order) or f"Pedido {order.id}"} parada {stop.stop_no} cancelada: {reason}',
    )
    _publish_updates(order.conversation, order)
    return {'canceled': 1, 'order': order, 'stop': stop}
