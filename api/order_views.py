"""Agent-facing order integration endpoints (plan §4.3).

All require the normal Token auth (DRF defaults); the integration key stays
server-side. Phase 2 exposed the client read paths (search, saved addresses,
order history); Phase 3 adds creation, the ops/calculator proxies and the
local order detail/refresh endpoints.
"""

import logging

from django.core.cache import cache
from django.http import Http404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .integrations import calculator, ops
from .integrations.draft import (
    DraftError,
    DraftNotConfigured,
    draft_from_message,
)
from .integrations.orders import (
    CATALOG_CACHE_TTL,
    FINISHED_STATUSES,
    MAX_STOPS,
    SERVICES_CACHE_KEY,
    OrderCancelError,
    OrderCreationFailed,
    OrderInProgressError,
    OrderValidationError,
    cancel_order,
    cancel_stop,
    create_order,
    refresh_order,
    validate_cancel_reason,
)
from .integrations.services import calculator_service
from .models import Message, Order
from .serializers import (
    ClientDefaultAddressInputSerializer,
    OrderCreateInputSerializer,
    OrderDraftInputSerializer,
    OrderQuoteInputSerializer,
    OrderSerializer,
)

logger = logging.getLogger('api')

TOOLS_CACHE_KEY = 'calculator:tools'


def _ops_error_response(exc: ops.OpsAPIError):
    status_code = 503 if isinstance(exc, ops.OpsNotConfigured) else 502
    return Response({'ok': False, 'error': exc.message}, status=status_code)


def _calculator_error_response(exc: calculator.CalculatorAPIError):
    status_code = 503 if isinstance(exc, calculator.CalculatorNotConfigured) else 502
    return Response({'ok': False, 'error': exc.message}, status=status_code)


def _cache_get(key):
    try:
        return cache.get(key)
    except Exception:
        return None


def _cache_set(key, value, ttl):
    try:
        cache.set(key, value, ttl)
    except Exception:
        logger.warning('Cache unavailable while storing %s', key)


def _conversation_visible_to(user, conversation) -> bool:
    """Mirror ``ConversationViewSet.get_queryset`` group scoping."""
    if user.is_staff:
        return True
    try:
        profile = user.profile
    except Exception:
        return False
    group_id = getattr(profile, 'group_id', None)
    if group_id:
        return conversation.group_id == group_id
    return True


def _get_visible_order(request, order_id) -> Order:
    order = (
        Order.objects.select_related('conversation', 'conversation__group')
        .prefetch_related('stops')
        .filter(id=order_id)
        .first()
    )
    if order is None or not _conversation_visible_to(request.user, order.conversation):
        raise Http404
    return order


# ---------------------------------------------------------------------------
#  Ops / calculator proxies
# ---------------------------------------------------------------------------


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def order_services(request):
    """GET /api/orders/services/ — ops service catalog, cached 10 min."""
    catalog = _cache_get(SERVICES_CACHE_KEY)
    if catalog is None:
        try:
            data = ops.get_services()
        except ops.OpsAPIError as exc:
            logger.warning('Service catalog failed: %s', exc)
            return _ops_error_response(exc)
        catalog = data.get('services') if isinstance(data, dict) else []
        if not isinstance(catalog, list):
            catalog = []
        _cache_set(SERVICES_CACHE_KEY, catalog, CATALOG_CACHE_TTL)

    return Response({'ok': True, 'services': catalog})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def order_tools(request):
    """GET /api/orders/tools/ — calculator tool catalog, cached 10 min."""
    tools = _cache_get(TOOLS_CACHE_KEY)
    if tools is None:
        try:
            tools = calculator.get_tools()
        except calculator.CalculatorAPIError as exc:
            logger.warning('Tool catalog failed: %s', exc)
            return _calculator_error_response(exc)
        if not isinstance(tools, list):
            tools = []
        _cache_set(TOOLS_CACHE_KEY, tools, CATALOG_CACHE_TTL)

    return Response({'ok': True, 'tools': tools})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def order_geocode_search(request):
    """POST /api/orders/geocode/search/ — calculator autocomplete proxy."""
    query = (request.data.get('q') or '').strip()
    if len(query) < 2:
        return Response({'ok': True, 'results': []})

    try:
        results = calculator.geocode_search(query[:200])
    except calculator.CalculatorAPIError as exc:
        logger.warning('Geocode search failed: %s', exc)
        return _calculator_error_response(exc)

    if not isinstance(results, list):
        results = []
    return Response({'ok': True, 'results': results})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def order_geocode_details(request):
    """POST /api/orders/geocode/details/ — calculator place details proxy."""
    place_id = (request.data.get('place_id') or '').strip()
    if not place_id:
        return Response(
            {'ok': False, 'error': 'place_id es obligatorio.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        place = calculator.geocode_details(place_id[:500])
    except calculator.CalculatorAPIError as exc:
        logger.warning('Geocode details failed: %s', exc)
        return _calculator_error_response(exc)

    return Response({'ok': True, 'place': place})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def order_quote(request):
    """POST /api/orders/quote/ — multi-stop quote with a shared origin."""
    serializer = OrderQuoteInputSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(
            {'ok': False, 'error': 'Datos inválidos.', 'details': serializer.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )
    data = serializer.validated_data

    origin = {
        'address': data.get('origin_address') or '',
        'lat': data.get('origin_lat'),
        'lng': data.get('origin_lng'),
    }
    segments = []
    for stop in data['stops']:
        calc_type = calculator_service(stop['service_type'])
        if not calc_type:
            return Response(
                {'ok': False, 'error': f"Tipo de servicio desconocido: {stop['service_type']}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        segments.append({
            'service_type': calc_type,
            'description': stop.get('description') or '',
            'origin': origin,
            'destination': {
                'address': stop.get('dest_address') or '',
                'lat': stop.get('lat'),
                'lng': stop.get('lng'),
            },
        })

    if not segments:
        return Response(
            {'ok': False, 'error': 'Agrega al menos una parada.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        quote = calculator.calculate_price({
            'profile': data.get('profile') or 'usuario_final',
            'segments': segments,
            'tools': data.get('tools') or [],
            'payment_method': data.get('payment_method') or 'efectivo',
            'acompanante': bool(data.get('acompanante')),
        })
    except calculator.CalculatorAPIError as exc:
        logger.warning('Quote failed: %s', exc)
        return _calculator_error_response(exc)

    return Response({'ok': True, 'quote': quote})


# ---------------------------------------------------------------------------
#  Client proxies (Phase 2)
# ---------------------------------------------------------------------------


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def order_client_search(request):
    """GET /api/orders/clients/?q= — ops client search (name, DNI, phone)."""
    query = (request.query_params.get('q') or '').strip()
    if len(query) < 2:
        return Response({'ok': True, 'rows': []})

    try:
        data = ops.search_clients(query)
    except ops.OpsAPIError as exc:
        logger.warning('Client search failed: %s', exc)
        return _ops_error_response(exc)

    if not isinstance(data, dict):
        return Response({'ok': True, 'rows': []})
    return Response({
        'ok': bool(data.get('ok', True)),
        'rows': data.get('rows') or [],
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def order_client_addresses(request, client_id):
    """GET /api/orders/clients/{id}/addresses/ — saved destinations.

    Ops returns up to 30 rows ordered by default/usage; the chips cap (default
    + 4 most recent) is a UI concern (``lib/orders.js``).
    """
    try:
        data = ops.get_client_addresses(client_id)
    except ops.OpsAPIError as exc:
        logger.warning('Client addresses failed for %s: %s', client_id, exc)
        return _ops_error_response(exc)

    if not isinstance(data, dict):
        return Response({'ok': True, 'rows': []})
    return Response({
        'ok': bool(data.get('ok', True)),
        'rows': data.get('rows') or [],
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def order_client_default_address(request, client_id):
    """POST /api/orders/clients/{id}/addresses/default/ — set the default.

    Upserts the address in the ops history and replaces the previous default
    (plan Phase 7).
    """
    serializer = ClientDefaultAddressInputSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(
            {'ok': False, 'error': 'Datos inválidos.', 'details': serializer.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )
    data = serializer.validated_data

    try:
        result = ops.set_client_default_address(
            client_id,
            data['address'].strip(),
            lat=data.get('lat'),
            lng=data.get('lng'),
        )
    except ops.OpsAPIError as exc:
        logger.warning('Set default address failed for %s: %s', client_id, exc)
        return _ops_error_response(exc)

    if not isinstance(result, dict):
        return Response({'ok': True, 'row': None})
    return Response({
        'ok': bool(result.get('ok', True)),
        'row': result.get('row'),
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def order_client_orders(request, client_id):
    """GET /api/orders/clients/{id}/orders/?limit= — recent order history."""
    try:
        limit = int(request.query_params.get('limit', 5))
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, 20))

    try:
        data = ops.get_client_orders(client_id, limit=limit)
    except ops.OpsAPIError as exc:
        logger.warning('Client orders failed for %s: %s', client_id, exc)
        return _ops_error_response(exc)

    if not isinstance(data, dict):
        return Response({'ok': True, 'orders': []})
    return Response({
        'ok': bool(data.get('ok', True)),
        'orders': data.get('orders') or [],
    })


# ---------------------------------------------------------------------------
#  Local orders (conversation-scoped and detail/refresh)
# ---------------------------------------------------------------------------


def list_conversation_orders(request, conversation):
    """Shared body of ``GET /api/conversations/{id}/orders/``."""
    include_finished = (request.query_params.get('include_finished') or '').lower() in (
        '1', 'true', 'yes',
    )
    orders = (
        Order.objects.filter(conversation=conversation)
        .prefetch_related('stops')
        .order_by('-created_at')
    )
    if not include_finished:
        orders = orders.exclude(status__in=FINISHED_STATUSES)

    payload = [OrderSerializer(order).data for order in orders[:50]]
    return Response({'ok': True, 'orders': payload})


def create_conversation_order(request, conversation):
    """Shared body of ``POST /api/conversations/{id}/orders/``."""
    serializer = OrderCreateInputSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(
            {'ok': False, 'error': 'Datos inválidos.', 'details': serializer.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )
    data = serializer.validated_data
    if len(data['stops']) > MAX_STOPS:
        return Response(
            {'ok': False, 'error': f'Máximo {MAX_STOPS} paradas por pedido.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        order, created = create_order(conversation, data, request.user)
    except OrderValidationError as exc:
        return Response(
            {'ok': False, 'error': exc.message, 'field': exc.field},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except OrderInProgressError as exc:
        return Response(
            {'ok': False, 'error': exc.message},
            status=status.HTTP_409_CONFLICT,
        )
    except OrderCreationFailed as exc:
        logger.warning('Order creation failed for conversation %s: %s', conversation.id, exc.message)
        return Response(
            {
                'ok': False,
                'error': exc.message,
                'retry': True,
                'order': OrderSerializer(exc.order).data,
            },
            status=status.HTTP_502_BAD_GATEWAY,
        )

    return Response(
        {'ok': True, 'created': created, 'order': OrderSerializer(order).data},
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


def draft_conversation_order(request, conversation):
    """Shared body of ``POST /api/conversations/{id}/orders/draft/`` (Phase 6).

    Generates an order draft from the selected message onward with the
    configured LLM. Read-only: nothing is sent to ops.
    """
    serializer = OrderDraftInputSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(
            {'ok': False, 'error': 'Datos inválidos.', 'details': serializer.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )

    from_message_id = serializer.validated_data['from_message_id']
    from_message = Message.objects.filter(
        id=from_message_id, conversation=conversation,
    ).first()
    if from_message is None:
        return Response(
            {'ok': False, 'error': 'El mensaje seleccionado no pertenece a esta conversación.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        draft = draft_from_message(conversation, from_message)
    except DraftNotConfigured as exc:
        logger.warning('Order draft not configured: %s', exc.message)
        return Response({'ok': False, 'error': exc.message}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except DraftError as exc:
        logger.warning('Order draft failed for conversation %s: %s', conversation.id, exc.message)
        return Response({'ok': False, 'error': exc.message}, status=status.HTTP_502_BAD_GATEWAY)

    return Response({'ok': True, **draft})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def order_detail(request, order_id):
    """GET /api/orders/{id}/ — local order detail."""
    order = _get_visible_order(request, order_id)
    return Response({'ok': True, 'order': OrderSerializer(order).data})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def order_refresh(request, order_id):
    """POST /api/orders/{id}/refresh/ — force sync from ops."""
    order = _get_visible_order(request, order_id)
    try:
        refresh_order(order)
    except ops.OpsAPIError as exc:
        logger.warning('Order refresh failed for %s: %s', order_id, exc)
        return _ops_error_response(exc)

    order.refresh_from_db()
    return Response({'ok': True, 'order': OrderSerializer(order).data})


def _cancel_reason_or_response(request):
    """Validate the mandatory cancellation reason (plan §6.3)."""
    try:
        return validate_cancel_reason(request.data.get('reason')), None
    except OrderCancelError as exc:
        return None, Response(
            {'ok': False, 'error': exc.message, 'field': exc.field},
            status=status.HTTP_400_BAD_REQUEST,
        )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def order_cancel(request, order_id):
    """POST /api/orders/{id}/cancel/ — cancel the whole pedido with a reason."""
    order = _get_visible_order(request, order_id)
    reason, error = _cancel_reason_or_response(request)
    if error is not None:
        return error

    try:
        result = cancel_order(order, reason, request.user)
    except ops.OpsAPIError as exc:
        logger.warning('Order cancel failed for %s: %s', order_id, exc)
        return _ops_error_response(exc)

    return Response({
        'ok': True,
        'canceled': result['canceled'],
        'order': OrderSerializer(order).data,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def order_stop_cancel(request, order_id, stop_id):
    """POST /api/orders/{id}/stops/{stop_id}/cancel/ — cancel one parada."""
    order = _get_visible_order(request, order_id)
    stop = next((item for item in order.stops.all() if item.id == stop_id), None)
    if stop is None:
        raise Http404

    reason, error = _cancel_reason_or_response(request)
    if error is not None:
        return error

    try:
        result = cancel_stop(order, stop, reason, request.user)
    except ops.OpsAPIError as exc:
        logger.warning('Order stop cancel failed for %s/%s: %s', order_id, stop_id, exc)
        return _ops_error_response(exc)

    return Response({
        'ok': True,
        'canceled': result['canceled'],
        'order': OrderSerializer(order).data,
    })
