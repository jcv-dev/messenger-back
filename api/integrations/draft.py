"""LLM order draft from a conversation (plan §4.3/§6.2, Phase 6).

The agent clicks ``✨ Analizar conversación`` and then the message where the
pedido starts. This module:

1. Builds the transcript from that message onward (cap 60, 800 chars each).
2. Loads the ops context the model needs: client link, saved addresses, a
   summary of the last 3 orders, the service catalog and the calculator tools.
3. Runs the DeepSeek call in JSON mode with two tools (address search and
   place details), so addresses are real Tuluá addresses with coordinates.
4. Validates the response against the order schema, retries once when the
   model breaks it, and caches by
   ``(conversation, from_message_id, last_message_id)``.

Nothing is sent to ops: the response only prefills the order sheet.
"""

import json
import logging
from datetime import date

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from api.models import Message

from . import calculator, llm, ops
from .clients import fetch_client_by_phone
from .orders import fetch_services_catalog, format_cop, service_requires_address
from .phones import to_ops
from .services import OPS_SERVICE_KEYS, normalize_ops_key, ops_service

logger = logging.getLogger('api')

MAX_MESSAGES = 60
MAX_MESSAGE_CHARS = 800
MAX_TOOL_ROUNDS = 4
MAX_TOOL_CALLS = 8
DRAFT_CACHE_PREFIX = 'order:draft:'
DRAFT_CACHE_TTL = 900  # 15 min
TOOLS_CACHE_KEY = 'calculator:tools'
TOOL_CACHE_TTL = 600

ALLOWED_PAYMENT = frozenset({'efectivo', 'nequi'})
ALLOWED_PROFILE = frozenset({'usuario_final', 'negocio'})

# Message types that never carry order context (reactions/edits are noise and
# template messages are the automatic status notifications).
SKIPPED_MESSAGE_TYPES = frozenset({'reaction', 'edit', 'template'})


class DraftError(Exception):
    """The draft could not be generated (LLM failed or returned garbage)."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class DraftNotConfigured(DraftError):
    """``ORDER_LLM_*`` settings are missing."""


class DraftResponseError(DraftError):
    """The model response does not match the order schema (retryable once)."""


# ---------------------------------------------------------------------------
#  Transcript
# ---------------------------------------------------------------------------


def _flatten_content(content) -> str:
    """Readable text for a message ``content`` that may be a JSON payload."""
    text = str(content or '').strip()
    if not (text.startswith('{') and text.endswith('}')):
        return text
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if not isinstance(data, dict):
        return text
    for key in ('text', 'body', 'caption', 'content', 'title'):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return text


def message_text(message: Message) -> str:
    """One transcript line for ``message`` ('' when it carries no content)."""
    content = _flatten_content(message.content)
    message_type = (message.message_type or 'text').lower()

    if message_type == 'location':
        location = None
        metadata = message.metadata if isinstance(message.metadata, dict) else {}
        if isinstance(metadata.get('location'), dict):
            location = metadata['location']
        if location is None and message.metadata and isinstance(message.metadata, dict):
            location = metadata
        name = (location or {}).get('name') or ''
        address = (location or {}).get('address') or ''
        parts = [part for part in (name, address) if part]
        detail = ' · '.join(parts) or content or ''
        return f'[ubicación] {detail}'.strip()
    if message_type == 'audio':
        return f'[audio] {content}'.strip()
    if message_type == 'image':
        return f'[imagen] {content}'.strip()
    if message_type == 'video':
        return f'[video] {content}'.strip()
    if message_type == 'sticker':
        return '[sticker]'
    if message_type == 'document':
        filename = ''
        if isinstance(message.metadata, dict):
            filename = message.metadata.get('filename') or ''
        return f'[documento] {filename or content}'.strip()
    return content


def collect_transcript(conversation, from_message: Message) -> list:
    """``[{'role', 'text'}]`` from ``from_message`` onward (cap 60)."""
    messages = (
        Message.objects
        .filter(conversation=conversation, id__gte=from_message.id)
        .exclude(message_type__in=SKIPPED_MESSAGE_TYPES)
        .order_by('created_at', 'id')[:MAX_MESSAGES]
    )
    transcript = []
    for message in messages:
        text = message_text(message)
        if not text:
            continue
        transcript.append({
            'role': 'user' if message.direction == 'inbound' else 'assistant',
            'text': text[:MAX_MESSAGE_CHARS],
        })
    return transcript


def transcript_as_text(transcript: list) -> str:
    lines = []
    for entry in transcript:
        speaker = 'Cliente' if entry['role'] == 'user' else 'Agente'
        lines.append(f'{speaker}: {entry["text"]}')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
#  Ops context
# ---------------------------------------------------------------------------


def resolve_draft_client(conversation) -> dict:
    """Ops client for the draft: conversation link → phone match → contact."""
    if conversation.ops_client_user_id:
        snapshot = conversation.ops_client_snapshot or {}
        name = (
            snapshot.get('name')
            or conversation.custom_name
            or conversation.contact_name
            or ''
        )
        return {
            'ops_client_user_id': conversation.ops_client_user_id,
            'name': str(name)[:255],
            'phone': str(snapshot.get('phone') or to_ops(conversation.contact_phone) or '')[:20],
            'linked': True,
        }

    if conversation.contact_phone:
        found = fetch_client_by_phone(conversation.contact_phone)
        if found:
            return {
                'ops_client_user_id': found.get('id'),
                'name': str(found.get('name') or '')[:255],
                'phone': str(found.get('phone') or '')[:20],
                'linked': False,
            }

    name = (
        conversation.custom_name
        or conversation.contact_name
        or conversation.whatsapp_username
        or ''
    )
    return {
        'ops_client_user_id': None,
        'name': str(name)[:255],
        'phone': to_ops(conversation.contact_phone),
        'linked': False,
    }


def fetch_client_context(client_id) -> dict:
    """Saved addresses + last orders for the draft prompt (best effort)."""
    context = {'addresses': [], 'orders': []}
    if not client_id or not ops.is_configured():
        return context
    try:
        addresses = ops.get_client_addresses(int(client_id))
        if isinstance(addresses, dict):
            context['addresses'] = addresses.get('rows') or []
    except ops.OpsAPIError as exc:
        logger.warning('Draft: saved addresses failed for %s: %s', client_id, exc)
    try:
        orders = ops.get_client_orders(int(client_id), limit=3)
        if isinstance(orders, dict):
            context['orders'] = orders.get('orders') or []
    except ops.OpsAPIError as exc:
        logger.warning('Draft: client orders failed for %s: %s', client_id, exc)
    return context


def summarize_addresses(rows) -> str:
    lines = []
    for row in (rows or [])[:8]:
        if not isinstance(row, dict):
            continue
        address = (row.get('address') or '').strip()
        if not address:
            continue
        coords = ''
        try:
            if row.get('lat') is not None and row.get('lng') is not None:
                coords = f' (lat {float(row["lat"]):.6f}, lng {float(row["lng"]):.6f})'
        except (TypeError, ValueError):
            coords = ''
        suffix = ' (predeterminada)' if row.get('is_default') else ''
        lines.append(f'- {address}{coords}{suffix}')
    return '\n'.join(lines)


def summarize_orders(orders) -> str:
    lines = []
    for order in (orders or [])[:3]:
        if not isinstance(order, dict):
            continue
        number = order.get('order_number') or order.get('id') or ''
        status = order.get('status_label') or order.get('status') or ''
        created = str(order.get('created_at') or '')
        created = created[:16].replace('T', ' ')
        line = f'- Pedido #{number}'
        details = [part for part in (status, created) if part]
        if details:
            line += f' ({", ".join(details)})'
        origin = (order.get('origin') or '').strip()
        if origin:
            line += f' · origen: {origin}'
        if order.get('total') is not None:
            line += f' · total: {format_cop(order.get("total"))}'
        stop_parts = []
        for stop in (order.get('stops') or [])[:5]:
            if not isinstance(stop, dict):
                continue
            parts = []
            if stop.get('stop'):
                parts.append(str(stop['stop']))
            if stop.get('service_type'):
                parts.append(str(stop['service_type']))
            if stop.get('address'):
                parts.append(str(stop['address']))
            if stop.get('description'):
                parts.append(str(stop['description']))
            if parts:
                stop_parts.append(' '.join(parts))
        if stop_parts:
            line += '\n    paradas: ' + ' | '.join(stop_parts)
        lines.append(line)
    return '\n'.join(lines)


def get_tools_catalog() -> list:
    """Calculator tool catalog, reusing the proxy cache (10 min)."""
    try:
        cached = cache.get(TOOLS_CACHE_KEY)
    except Exception:
        cached = None
    if isinstance(cached, list):
        return cached
    if not calculator.is_configured():
        return []
    try:
        tools = calculator.get_tools()
    except calculator.CalculatorAPIError as exc:
        logger.warning('Draft: tool catalog unavailable: %s', exc)
        return []
    if not isinstance(tools, list):
        tools = []
    try:
        cache.set(TOOLS_CACHE_KEY, tools, TOOL_CACHE_TTL)
    except Exception:
        pass
    return tools


def catalog_lines(catalog) -> str:
    lines = []
    for item in catalog or []:
        if not isinstance(item, dict):
            continue
        key = normalize_ops_key(item.get('key'))
        if not key:
            continue
        name = item.get('name') or key
        suffix = 'requiere dirección' if item.get('requires_address') else 'sin dirección'
        lines.append(f'- {key} ({name}, {suffix})')
    return '\n'.join(lines)


def tools_lines(tool_catalog) -> str:
    lines = []
    for tool in tool_catalog or []:
        if not isinstance(tool, dict) or not tool.get('key'):
            continue
        label = tool.get('label') or tool.get('key')
        surcharge = tool.get('surcharge')
        suffix = ''
        if surcharge:
            try:
                suffix = f' (+{format_cop(surcharge)})'
            except Exception:
                suffix = ''
        lines.append(f'- {tool["key"]} ({label}{suffix})')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
#  Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
Eres un asistente experto en logística de Domiitulua (Tuluá, Valle del Cauca, Colombia).
Extraes pedidos de conversaciones de WhatsApp para que un agente los revise y cree
el pedido. Nunca confirmas nada al cliente.

FECHA DE HOY: {today} (America/Bogota). Moneda: pesos colombianos (COP).

CATÁLOGO DE SERVICIOS (usa la clave `key` en `stops[].service_type`):
{services}

HERRAMIENTAS DEL PEDIDO (usa las claves válidas en `tools[]`, la mayoría tiene recargo):
{tools}

CLIENTE DE ESTA CONVERSACIÓN:
- Nombre: {client_name}
- Teléfono: {client_phone}
- Cliente registrado en Domiitulua: {client_linked}
- Direcciones guardadas:
{saved_addresses}
- Últimos pedidos:
{last_orders}

REGLAS
1. `origin_address` es el punto de recogida. Cada elemento de `stops[]` es un destino.
2. Un pedido puede tener varias paradas (`stops[]`), en el orden en que el cliente las
   menciona.
3. Si el cliente no repite sus datos (por ejemplo dice "manda un domiciliario"), usa sus
   direcciones guardadas y sus últimos pedidos para completar origen y destinos. Las
   direcciones guardadas ya están confirmadas: cópialas tal cual, con sus coordenadas
   cuando aparezcan.
4. Para las direcciones que el cliente mencione en la conversación, confírmalas con la
   herramienta `buscar_direccion`. Luego usa `detalles_direccion` con el `place_id`
   elegido y copia EXACTAMENTE la dirección y las coordenadas que devuelve la
   herramienta.
5. Si la conversación no menciona una ciudad, busca siempre en Tuluá (Valle del Cauca).
   Usa otra ciudad únicamente si el cliente la menciona de forma explícita.
6. No inventes direcciones, precios ni datos. Si un dato falta, agrégalo a `missing[]`
   con un texto corto en español (por ejemplo "dirección de la parada 2").
7. `confidence` es un número entre 0 y 1: qué tan seguro estás del pedido extraído.
8. Responde SIEMPRE con un único objeto JSON válido, sin texto extra, con esta forma:
{{
  "origin_address": "dirección de recogida",
  "origin_lat": 4.0,
  "origin_lng": -76.2,
  "payment_method": "efectivo" | "nequi",
  "profile": "usuario_final" | "negocio",
  "acompanante": false,
  "tools": ["canasta"],
  "stops": [
    {{
      "service_type": "domicilio",
      "dest_address": "dirección de entrega",
      "lat": 4.0,
      "lng": -76.2,
      "description": "qué se lleva o se hace",
      "observation": "instrucciones para el domiciliario"
    }}
  ],
  "missing": [],
  "confidence": 0.9
}}

El pedido puede ser de una sola parada. Si el cliente no indicó método de pago usa
"efectivo" y si no indicó perfil usa "usuario_final". Escribe `description` solo con
lo que dijo el cliente (por ejemplo "mercado" o "paquete pequeño")."""


def build_messages(transcript, client_ref, client_context, services, tool_catalog) -> list:
    today = timezone.localdate()
    system = SYSTEM_PROMPT.format(
        today=today.isoformat() if isinstance(today, date) else str(today),
        services=catalog_lines(services) or '- domicilio, mensajeria, compras',
        tools=tools_lines(tool_catalog) or '- (sin herramientas configuradas)',
        client_name=client_ref.get('name') or '(sin nombre)',
        client_phone=client_ref.get('phone') or '(sin teléfono)',
        client_linked='sí' if client_ref.get('ops_client_user_id') else 'no',
        saved_addresses=summarize_addresses(client_context.get('addresses')) or '- (sin direcciones guardadas)',
        last_orders=summarize_orders(client_context.get('orders')) or '- (sin pedidos anteriores)',
    )
    return [
        {'role': 'system', 'content': system},
        {
            'role': 'user',
            'content': (
                'Extrae el pedido de esta conversación. La conversación empieza en el '
                'mensaje que el agente marcó como inicio:\n\n'
                f'{transcript_as_text(transcript)}\n\n'
                'Responde solo con el objeto JSON.'
            ),
        },
    ]


# ---------------------------------------------------------------------------
#  Tools
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        'type': 'function',
        'function': {
            'name': 'buscar_direccion',
            'description': (
                'Busca direcciones reales con el geocodificador de Domiitulua. '
                'Por defecto busca en Tuluá, Valle del Cauca, Colombia. '
                'Devuelve hasta 5 coincidencias con display_name y place_id.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string',
                        'description': 'Dirección o lugar a buscar (por ejemplo "Calle 10 #20-30").',
                    },
                },
                'required': ['query'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'detalles_direccion',
            'description': (
                'Devuelve la dirección normalizada y las coordenadas lat/lng de un '
                'place_id obtenido con buscar_direccion.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'place_id': {
                        'type': 'string',
                        'description': 'place_id devuelto por buscar_direccion.',
                    },
                },
                'required': ['place_id'],
            },
        },
    },
]


def run_tool(name: str, arguments: dict) -> dict:
    """Execute one draft tool against the calculator API."""
    arguments = arguments if isinstance(arguments, dict) else {}
    if name == 'buscar_direccion':
        query = str(arguments.get('query') or '').strip()[:200]
        if not query:
            return {'error': 'query vacío'}
        try:
            results = calculator.geocode_search(query)
        except calculator.CalculatorAPIError as exc:
            return {'error': exc.message}
        rows = []
        for entry in (results or [])[:5]:
            if not isinstance(entry, dict):
                continue
            rows.append({
                'display_name': entry.get('display_name') or '',
                'place_id': entry.get('place_id') or '',
            })
        return {'resultados': rows}
    if name == 'detalles_direccion':
        place_id = str(arguments.get('place_id') or '').strip()[:500]
        if not place_id:
            return {'error': 'place_id vacío'}
        try:
            place = calculator.geocode_details(place_id)
        except calculator.CalculatorAPIError as exc:
            return {'error': exc.message}
        if not isinstance(place, dict):
            return {'error': 'respuesta inválida'}
        return {
            'display_name': place.get('display_name') or '',
            'lat': place.get('lat'),
            'lng': place.get('lng'),
        }
    return {'error': f'herramienta desconocida: {name}'}


def _run_tool_loop(messages) -> str:
    """Chat until the model answers without tool calls; returns its content."""
    max_rounds = int(getattr(settings, 'ORDER_LLM_MAX_TOOL_ROUNDS', MAX_TOOL_ROUNDS))
    for _round in range(max(1, max_rounds)):
        message = llm.chat(messages, tools=TOOL_DEFINITIONS, json_mode=True)
        calls = message.get('tool_calls') or []
        messages.append({
            'role': 'assistant',
            'content': message.get('content') or '',
            **({'tool_calls': [
                {
                    'id': call['id'],
                    'type': 'function',
                    'function': {
                        'name': call['name'],
                        'arguments': json.dumps(call['arguments'], ensure_ascii=False),
                    },
                }
                for call in calls
            ]} if calls else {}),
        })
        if not calls:
            return message.get('content') or ''

        for call in calls[:MAX_TOOL_CALLS]:
            result = run_tool(call['name'], call['arguments'])
            messages.append({
                'role': 'tool',
                'tool_call_id': call['id'],
                'content': json.dumps(result, ensure_ascii=False)[:4000],
            })

    # Tool budget spent: ask for the final JSON without tools.
    final = llm.chat(messages, json_mode=True)
    return final.get('content') or ''


# ---------------------------------------------------------------------------
#  Response validation
# ---------------------------------------------------------------------------


def _as_float(value, *, low, high):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float('inf'), float('-inf')):  # NaN / inf
        return None
    if low <= number <= high:
        return number
    return None


def _as_text(value, limit) -> str:
    return str(value or '').strip()[:limit]


def normalize_draft(raw, catalog, tool_catalog) -> dict:
    """Validate the model JSON against the order schema (plan §4.3).

    Raises ``DraftResponseError`` when there is no usable order (the caller
    retries once).
    """
    if not isinstance(raw, dict):
        raise DraftResponseError('El LLM no devolvió un objeto JSON.')

    missing = []
    raw_missing = raw.get('missing')
    if isinstance(raw_missing, list):
        for item in raw_missing[:20]:
            text = _as_text(item, 200)
            if text:
                missing.append(text)

    origin_address = _as_text(raw.get('origin_address'), 255)
    origin_lat = _as_float(raw.get('origin_lat'), low=-90, high=90)
    origin_lng = _as_float(raw.get('origin_lng'), low=-180, high=180)

    payment_method = normalize_ops_key(raw.get('payment_method'))
    if payment_method not in ALLOWED_PAYMENT:
        payment_method = 'efectivo'
    profile = normalize_ops_key(raw.get('profile'))
    if profile not in ALLOWED_PROFILE:
        profile = 'usuario_final'
    acompanante = bool(raw.get('acompanante'))

    valid_tool_keys = {
        normalize_ops_key(tool.get('key'))
        for tool in (tool_catalog or []) if isinstance(tool, dict)
    }
    tools = []
    for tool in raw.get('tools') or []:
        key = normalize_ops_key(tool)
        if not key or key in tools:
            continue
        if valid_tool_keys and key not in valid_tool_keys:
            continue
        tools.append(key)
    tools = tools[:10]

    stops = []
    raw_stops = raw.get('stops')
    if isinstance(raw_stops, list):
        for index, raw_stop in enumerate(raw_stops[:10], start=1):
            if not isinstance(raw_stop, dict):
                continue
            service_type = normalize_ops_key(raw_stop.get('service_type'))
            if service_type not in OPS_SERVICE_KEYS:
                # Accept calculator-flavored keys (e.g. "domicilios").
                service_type = ops_service(service_type) or 'domicilio'
                missing.append(f'servicio de la parada {index}')
            dest_address = _as_text(raw_stop.get('dest_address'), 500)
            description = _as_text(raw_stop.get('description'), 500)
            observation = _as_text(raw_stop.get('observation'), 500)
            lat = _as_float(raw_stop.get('lat'), low=-90, high=90)
            lng = _as_float(raw_stop.get('lng'), low=-180, high=180)
            if not (dest_address or description or observation):
                continue
            if service_requires_address(service_type, catalog) and not dest_address:
                missing.append(f'dirección de la parada {index}')
            stops.append({
                'stop_no': len(stops) + 1,
                'service_type': service_type,
                'dest_address': dest_address,
                'lat': lat,
                'lng': lng,
                'description': description,
                'observation': observation,
                'price': 0,
            })

    if not stops:
        raise DraftResponseError('No se pudo identificar el pedido en la conversación.')
    if not origin_address:
        missing.append('dirección de origen')

    confidence = _as_float(raw.get('confidence'), low=-1e9, high=1e9)
    if confidence is None:
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    return {
        'origin_address': origin_address,
        'origin_lat': origin_lat,
        'origin_lng': origin_lng,
        'payment_method': payment_method,
        'profile': profile,
        'acompanante': acompanante,
        'tools': tools,
        'stops': stops,
        'missing': missing,
        'confidence': round(confidence, 2),
    }


def _parse_content(content: str) -> dict:
    try:
        parsed = json.loads(content or '')
    except ValueError as exc:
        raise DraftResponseError(f'El LLM no devolvió JSON válido: {exc}') from exc
    if not isinstance(parsed, dict):
        raise DraftResponseError('El LLM no devolvió un objeto JSON.')
    return parsed


def _generate(messages, catalog, tool_catalog) -> dict:
    """Run the tool loop, validate, retry once with a repair message."""
    content = _run_tool_loop(messages)
    try:
        return normalize_draft(_parse_content(content), catalog, tool_catalog)
    except DraftResponseError as first_error:
        logger.info('Draft response invalid, retrying once: %s', first_error.message)
        messages.append({'role': 'assistant', 'content': content[:4000]})
        messages.append({
            'role': 'user',
            'content': (
                'Tu respuesta anterior no cumple el esquema. Responde de nuevo SOLO con '
                'el objeto JSON válido (sin texto adicional), usando exactamente las '
                f'claves indicadas. Error: {first_error.message}'
            ),
        })
        content = _run_tool_loop(messages)
        try:
            return normalize_draft(_parse_content(content), catalog, tool_catalog)
        except DraftResponseError as second_error:
            raise DraftError(
                f'No se pudo interpretar el pedido: {second_error.message}',
            ) from second_error


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------


def draft_from_message(conversation, from_message: Message) -> dict:
    """Return the draft payload for the order starting at ``from_message``.

    Cached by ``(conversation, from_message, last_message)`` so retrying
    without new messages does not spend a second LLM call. Raises
    ``DraftNotConfigured`` (503) / ``DraftError`` (502).
    """
    if not llm.is_configured():
        raise DraftNotConfigured('El análisis con IA no está configurado (ORDER_LLM_API_KEY).')

    last_message_id = (
        Message.objects.filter(conversation=conversation)
        .order_by('-id').values_list('id', flat=True).first()
    )
    cache_key = f'{DRAFT_CACHE_PREFIX}{conversation.id}:{from_message.id}:{last_message_id}'
    try:
        cached = cache.get(cache_key)
    except Exception:
        cached = None
    if isinstance(cached, dict):
        return {**cached, 'cached': True}

    transcript = collect_transcript(conversation, from_message)
    if not transcript:
        raise DraftError('No hay mensajes para analizar desde ese punto.')

    client_ref = resolve_draft_client(conversation)
    client_context = fetch_client_context(client_ref.get('ops_client_user_id'))
    catalog = fetch_services_catalog()
    tool_catalog = get_tools_catalog()
    messages = build_messages(transcript, client_ref, client_context, catalog, tool_catalog)

    try:
        draft = _generate(messages, catalog, tool_catalog)
    except llm.LLMNotConfigured as exc:
        raise DraftNotConfigured(exc.args[0] if exc.args else str(exc)) from exc
    except llm.LLMError as exc:
        raise DraftError(exc.args[0] if exc.args else str(exc)) from exc

    result = {
        'from_message_id': from_message.id,
        'last_message_id': last_message_id,
        'model': getattr(settings, 'ORDER_LLM_MODEL', 'deepseek-flash'),
        'client': {
            'ops_client_user_id': client_ref.get('ops_client_user_id'),
            'name': client_ref.get('name') or '',
            'phone': client_ref.get('phone') or '',
        },
        'generated_at': timezone.now().isoformat(),
        **draft,
    }
    try:
        cache.set(cache_key, result, DRAFT_CACHE_TTL)
    except Exception:
        logger.warning('Draft cache unavailable for %s', cache_key)
    return {**result, 'cached': False}
