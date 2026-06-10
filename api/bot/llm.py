import asyncio
import logging
import time

from asgiref.sync import sync_to_async

from django.conf import settings
from django.utils import timezone

from google import genai
from google.genai import types as genai_types
from google.genai import errors as genai_errors

from . import calculator
from .constants import WELCOME_REPLY
from .utils import get_bot_user_async
from .guard import sanitize_llm_output
from .metrics import incr as incr_metric
from api.models import Conversation, ConversationNote, ConversationTake, Message
from api.serializers import MessageSerializer
from api.views import publish_conversation_update, send_whatsapp_outbound, _send_pool

logger = logging.getLogger("api.bot.llm")

# ---------------------------------------------------------------------------
#  Retry configuration
# ---------------------------------------------------------------------------
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRIES = getattr(settings, "BOT_LLM_RETRY_COUNT", 3)
_BASE_DELAY = 1.0

# ---------------------------------------------------------------------------
#  Cached system-prompt data
# ---------------------------------------------------------------------------
_tools_cache = None
_tools_cache_lock = asyncio.Lock()
TOOLS_CACHE_TTL = getattr(settings, "BOT_TOOLS_CACHE_TTL", 300)

# ---------------------------------------------------------------------------
#  LLM client singleton
# ---------------------------------------------------------------------------
_llm_client = None


async def _release_bot_take(conversation, escalated=False):
    bot = await get_bot_user_async()
    if bot:
        await sync_to_async(lambda: ConversationTake.objects.filter(
            created_by=bot,
            conversation=conversation,
            expires_at__gt=timezone.now(),
        ).update(expires_at=timezone.now()))()
    await sync_to_async(publish_conversation_update)(conversation, escalated=escalated)

    if escalated:
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            await r.setex(
                f"bot:escalated:{conversation.id}",
                600,
                "1",
            )
            await r.aclose()
        except Exception:
            logger.exception("Failed to set escalation flag for conv=%s", conversation.id)


# ---------------------------------------------------------------------------
#  System prompt (unchanged except for dynamic-tools injection)
# ---------------------------------------------------------------------------

def _sanitize_tool_field(val, max_len=200):
    if not isinstance(val, str):
        return ""
    return "".join(ch for ch in val if ord(ch) >= 32 or ch in "\n\r\t")[:max_len].strip()


async def _build_system_prompt():
    global _tools_cache
    now = time.time()

    async with _tools_cache_lock:
        if _tools_cache is None or (now - _tools_cache["fetched_at"]) > TOOLS_CACHE_TTL:
            try:
                raw_tools = await calculator.get_tools()
                sanitized = [
                    {
                        "key": _sanitize_tool_field(t.get("key", ""), 50),
                        "label": _sanitize_tool_field(t.get("label", ""), 100),
                        "description": _sanitize_tool_field(t.get("description", ""), 500),
                    }
                    for t in raw_tools
                    if isinstance(t, dict) and "key" in t
                ]
                _tools_cache = {"data": sanitized, "fetched_at": now}
            except Exception:
                if _tools_cache is not None:
                    logger.warning("Failed to refresh tools cache, using stale data")
                else:
                    _tools_cache = {"data": [], "fetched_at": now}
                    logger.warning("Failed to fetch tools from calculator, using empty list")

    tools_text = "\n".join(
        f'  - "{t["key"]}": {t["label"]} - {t["description"]}'
        for t in (_tools_cache["data"] or [])
    ) or "  - Ninguna disponible"

    return f"""Eres el asistente virtual de Domii Tulu\u00e1, empresa de domicilios y mensajer\u00eda en Tulu\u00e1, Colombia.

IDIOMA: Responde SIEMPRE en espa\u00f1ol colombiano. S\u00e9 amable, profesional y cercano.

FORMATO WHATSAPP:
- *texto* = negrita, _texto_ = cursiva, ~texto~ = tachado
- Precios y totales siempre en negrita: *$4,700 COP*
- send_interactive(type="button") \u2192 hasta 3 botones, IDs cortos
- send_interactive(type="list") \u2192 hasta 10 opciones en secciones
- Despu\u00e9s de send_interactive, DETENTE. No generes m\u00e1s texto ni llames m\u00e1s herramientas.
- USA send_interactive para: men\u00fa inicial, perfil, servicio, pago, confirmaciones s\u00ed/no, resultados de geocoding
- USA TEXTO NORMAL para: explicaciones, resultados de precios, preguntas frecuentes, conversaci\u00f3n natural

SERVICIOS: Domicilios, Mensajer\u00eda, Compras por encargo, Tr\u00e1mites, Bancarios, Domii Fijo (domiciliario dedicado por horas/d\u00edas).
HORARIOS: {settings.BOT_OPERATING_HOURS}.
COBERTURA: Tulu\u00e1 urbano y veredas. Fuera del \u00e1rea (Cali, Buga) = tarifas fijas.
PAGO: Efectivo (sin recargo) o Nequi (+$500). Pago al recibir.

HERRAMIENTAS:
{tools_text}

FLUJO COTIZAR SERVICIO:
1. Perfil: usuario_final o negocio
2. Tipo servicio: domicilios, mensajer\u00eda, compras por encargo, tr\u00e1mites, bancarios
3-4. Origen y destino: usa geocode_search si no hay coordenadas. Si m\u00faltiples resultados \u2192 send_interactive(type="list"), title m\u00e1x 24 chars (barrio/zona), description m\u00e1x 72 chars (direcci\u00f3n). Si un solo resultado \u2192 geocode_details directo. Despu\u00e9s de geocode_details el sistema env\u00eda ubicaci\u00f3n en mapa. SIEMPRE confirma la direcci\u00f3n con el usuario.
5. \u00bfM\u00e1s paradas? (repetir desde 3 si s\u00ed)
6. Herramientas: el usuario escribe cu\u00e1les necesita (ej: "canasta y malet\u00edn", "ninguna"). Extrae los tool keys.
7. Pago: efectivo o Nequi
8. \u00bfAcompa\u00f1ante? (objetos pesados) \u2192 s\u00ed/no
9. calculate_price \u2014 REQUIERE coordenadas (lat, lng) de geocode_details. Si faltan, falla.
10. Resultado: muestra *total*, distancia, m\u00e9todo de pago. Si Nequi, +$500. Pregunta si confirma.
11-12. Si confirma: nombre y tel\u00e9fono del receptor \u2192 pedido enviado.

FLUJO DOMII FIJO:
1. Nombre negocio \u2192 2. Direcci\u00f3n \u2192 3. Tel\u00e9fono \u2192 4. Fecha \u2192 5. Hora inicio \u2192 6. Hora fin \u2192 7. Volumen (1-5, 5-15, +15) \u2192 8. Resumen y confirmar \u2192 9. Solicitud enviada.

ERRORES:
- calculate_price sin coordenadas \u2192 geocode_details y reintenta.
- Sin resultados de geocoding \u2192 "Comparte tu ubicaci\u00f3n por WhatsApp o da m\u00e1s detalles."
- Error al enviar pedido \u2192 informa claramente, ofrece escalate_to_human. NUNCA muestres errores t\u00e9cnicos.

REGLAS:
- Saluda solo en el primer mensaje. Despu\u00e9s s\u00e9 directo y conciso (m\u00e1x 300 caracteres).
- Usa calculate_price siempre. NUNCA inventes precios.
- escalate_to_human si: cliente lo pide, o despu\u00e9s de 3 intentos fallidos.
- Al escalar, escribe en 'reason' un resumen MUY corto de lo que el cliente necesitaba y d\u00f3nde qued\u00f3 el flujo (m\u00e1x 200 caracteres). Esto ayuda al agente humano a retomar r\u00e1pido.
- Si el cliente pregunta algo sobre Domii que no sabes responder (ej: estado de un pedido, datos de contacto espec\u00edficos), escala con escalate_to_human explicando el motivo.
- Si el cliente pregunta algo completamente ajeno a Domii (deportes, clima, noticias, recetas, etc.), responde que solo ayudas con domicilios y mensajer\u00eda en Tulu\u00e1, y redirige al men\u00fa. NO escales en este caso.
- Al completar pedido: agradece y pregunta si necesita algo m\u00e1s.
- Si el cliente escribe "salir", "cancelar" o "men\u00fa", responde con el mensaje de bienvenida."""


DEFAULT_TOOLS = [
    genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="calculate_price",
                description="Calcula el precio del domicilio con todos los datos requeridos.",
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "profile": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            enum=["usuario_final", "negocio"],
                            description="Cliente final o negocio",
                        ),
                        "segments": genai_types.Schema(
                            type=genai_types.Type.ARRAY,
                            items=genai_types.Schema(
                                type=genai_types.Type.OBJECT,
                                properties={
                                    "service_type": genai_types.Schema(
                                        type=genai_types.Type.STRING,
                                        enum=["domicilios", "mensajeria", "purchases", "tramites", "bancarios"],
                                        description="Tipo de servicio",
                                    ),
                                    "description": genai_types.Schema(
                                        type=genai_types.Type.STRING,
                                        description="Opcional",
                                    ),
                                    "origin": genai_types.Schema(
                                        type=genai_types.Type.OBJECT,
                                        properties={
                                            "address": genai_types.Schema(type=genai_types.Type.STRING, description="Direcci\u00f3n"),
                                            "lat": genai_types.Schema(type=genai_types.Type.NUMBER, nullable=True),
                                            "lng": genai_types.Schema(type=genai_types.Type.NUMBER, nullable=True),
                                        },
                                        required=["address", "lat", "lng"],
                                    ),
                                    "destination": genai_types.Schema(
                                        type=genai_types.Type.OBJECT,
                                        properties={
                                            "address": genai_types.Schema(type=genai_types.Type.STRING, description="Direcci\u00f3n"),
                                            "lat": genai_types.Schema(type=genai_types.Type.NUMBER, nullable=True),
                                            "lng": genai_types.Schema(type=genai_types.Type.NUMBER, nullable=True),
                                        },
                                        required=["address", "lat", "lng"],
                                    ),
                                    "instructions": genai_types.Schema(
                                        type=genai_types.Type.STRING,
                                        description="Instrucciones extra",
                                        nullable=True,
                                    ),
                                },
                                required=["service_type", "origin", "destination"],
                            ),
                        ),
                        "tools": genai_types.Schema(
                            type=genai_types.Type.ARRAY,
                            items=genai_types.Schema(type=genai_types.Type.STRING),
                            description="Herramientas adicionales (tool keys)",
                        ),
                        "payment_method": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            enum=["efectivo", "nequi"],
                            description="M\u00e9todo de pago",
                        ),
                        "acompanante": genai_types.Schema(
                            type=genai_types.Type.BOOLEAN,
                            description="Requiere acompa\u00f1ante para objetos pesados",
                        ),
                    },
                    required=["profile", "segments", "payment_method", "acompanante"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="geocode_search",
                description="Busca direcciones por nombre. Devuelve lista con display_name y place_id.",
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "query": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            description="Direcci\u00f3n o lugar a buscar",
                        ),
                    },
                    required=["query"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="geocode_details",
                description="Obtiene coordenadas (lat, lng) de un place_id. Requerido antes de calculate_price.",
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "place_id": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            description="Place ID de geocode_search",
                        ),
                    },
                    required=["place_id"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="send_interactive",
                description="Env\u00eda mensaje interactivo (botones o lista) para men\u00fas y selecciones.",
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "type": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            enum=["button", "list"],
                            description="button (m\u00e1x 3 botones) o list (hasta 10 filas)",
                        ),
                        "header": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            description="Encabezado opcional (m\u00e1x 60)",
                        ),
                        "body": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            description="Texto principal",
                        ),
                        "footer": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            description="Pie opcional (m\u00e1x 60)",
                        ),
                        "button_label": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            description="Texto del bot\u00f3n para listas (m\u00e1x 20)",
                        ),
                        "buttons": genai_types.Schema(
                            type=genai_types.Type.ARRAY,
                            items=genai_types.Schema(
                                type=genai_types.Type.OBJECT,
                                properties={
                                    "id": genai_types.Schema(type=genai_types.Type.STRING, description="ID \u00fanico"),
                                    "title": genai_types.Schema(type=genai_types.Type.STRING, description="Texto del bot\u00f3n (m\u00e1x 20)"),
                                },
                                required=["id", "title"],
                            ),
                            description="Botones para type=button (m\u00e1x 3)",
                        ),
                        "sections": genai_types.Schema(
                            type=genai_types.Type.ARRAY,
                            items=genai_types.Schema(
                                type=genai_types.Type.OBJECT,
                                properties={
                                    "title": genai_types.Schema(type=genai_types.Type.STRING, description="T\u00edtulo de secci\u00f3n (m\u00e1x 24)"),
                                    "rows": genai_types.Schema(
                                        type=genai_types.Type.ARRAY,
                                        items=genai_types.Schema(
                                            type=genai_types.Type.OBJECT,
                                            properties={
                                                "id": genai_types.Schema(type=genai_types.Type.STRING, description="ID \u00fanico"),
                                                "title": genai_types.Schema(type=genai_types.Type.STRING, description="T\u00edtulo (m\u00e1x 24)"),
                                                "description": genai_types.Schema(type=genai_types.Type.STRING, description="Descripci\u00f3n (m\u00e1x 72)"),
                                            },
                                            required=["id", "title"],
                                        ),
                                        description="Filas (m\u00e1x 10 combinadas)",
                                    ),
                                },
                                required=["title", "rows"],
                            ),
                            description="Secciones para type=list",
                        ),
                    },
                    required=["type", "body"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="escalate_to_human",
                description="Escala a un agente humano. Escribe en 'reason' un resumen MUY corto de lo que el cliente solicitaba y en qu\u00e9 punto del flujo qued\u00f3 (m\u00e1x 200 caracteres).",
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "reason": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            description="Resumen corto (ej: 'Quiere domicilio de La Herradura al centro, ya dio ambas direcciones', 'Cliente insiste en pago con tarjeta')",
                        ),
                    },
                    required=["reason"],
                ),
            ),
        ],
    ),
]


def _get_client():
    global _llm_client
    if _llm_client is None:
        key = settings.GEMINI_API_KEY
        if not key:
            logger.error("GEMINI_API_KEY not configured — bot will auto-escalate all conversations")
            return None
        _llm_client = genai.Client(api_key=key)
    return _llm_client


# ---------------------------------------------------------------------------
#  Interactive payload validation
# ---------------------------------------------------------------------------

def _validate_interactive_payload(itype: str, args: dict) -> str | None:
    """Returns ``None`` if valid, error message string if invalid."""
    if itype not in ("button", "list"):
        return f"Invalid interactive type: {itype!r}"
    body = args.get("body", "")
    if not body or not body.strip():
        return "Interactive body is required"
    if itype == "button":
        buttons = args.get("buttons", [])
        if not buttons:
            return "Buttons required for type=button"
        if len(buttons) > 3:
            return f"Too many buttons ({len(buttons)}), max 3"
        for i, b in enumerate(buttons):
            if not b.get("id") or not b.get("title"):
                return f"Button {i} missing id or title"
    elif itype == "list":
        sections = args.get("sections", [])
        if not sections:
            return "Sections required for type=list"
        total_rows = 0
        for si, sec in enumerate(sections):
            rows = sec.get("rows", [])
            total_rows += len(rows)
            for ri, row in enumerate(rows):
                if not row.get("id") or not row.get("title"):
                    return f"Section {si} row {ri} missing id or title"
        if total_rows > 10:
            return f"Too many rows ({total_rows}), max 10"
    return None


# ---------------------------------------------------------------------------
#  Retry wrapper for Gemini API
# ---------------------------------------------------------------------------

async def _generate_with_retry(client, model, contents, config):
    last_error = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return await client.aio.models.generate_content(
                model=model, contents=contents, config=config,
            )
        except genai_errors.APIError as e:
            status = getattr(e, "code", None) or getattr(e, "status_code", None)
            if status in RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                delay = _BASE_DELAY * (2 ** attempt)
                logger.warning("Gemini API %s, retry %d/%d in %.1fs",
                               status, attempt + 1, _MAX_RETRIES, delay)
                incr_metric("llm.retries")
                await asyncio.sleep(delay)
                last_error = e
                continue
            raise
        except (ConnectionError, TimeoutError, asyncio.TimeoutError) as e:
            if attempt < _MAX_RETRIES:
                delay = _BASE_DELAY * (2 ** attempt)
                logger.warning("Gemini connection error, retry %d/%d in %.1fs",
                               attempt + 1, _MAX_RETRIES, delay)
                incr_metric("llm.retries")
                await asyncio.sleep(delay)
                last_error = e
                continue
            raise
    raise last_error  # pragma: no cover


# ---------------------------------------------------------------------------
#  Tool execution
# ---------------------------------------------------------------------------

async def _execute_tool(function_call, conversation, session):
    name = function_call.name
    args = dict(function_call.args) if function_call.args else {}

    try:
        if name == "calculate_price":
            profile = args.get("profile", "usuario_final")
            segments = args.get("segments", [])
            tools = args.get("tools", [])
            payment_method = args.get("payment_method", "efectivo")
            acompanante = args.get("acompanante", False)

            for i, seg in enumerate(segments):
                if isinstance(seg, dict):
                    origin = seg.get("origin", {})
                    dest = seg.get("destination", {})
                    if isinstance(origin, dict) and isinstance(dest, dict):
                        if origin.get("lat") is None or origin.get("lng") is None:
                            return {"error": f"Faltan coordenadas para el origen del segmento {i+1}. Usa geocode_details para obtenerlas."}
                        if dest.get("lat") is None or dest.get("lng") is None:
                            return {"error": f"Faltan coordenadas para el destino del segmento {i+1}. Usa geocode_details para obtenerlas."}

            stored_coords = session.get("pending_coords", [])
            if stored_coords:
                coord_idx = 0
                for seg in segments:
                    if isinstance(seg, dict):
                        for field in ("origin", "destination"):
                            if isinstance(seg.get(field), dict) and coord_idx < len(stored_coords):
                                seg[field]["lat"] = stored_coords[coord_idx]["lat"]
                                seg[field]["lng"] = stored_coords[coord_idx]["lng"]
                                coord_idx += 1
                session["pending_coords"] = stored_coords[coord_idx:]
                if not session["pending_coords"]:
                    session.pop("pending_coords", None)

            result = await calculator.calculate_price(
                profile=profile,
                segments=segments,
                tools=tools,
                payment_method=payment_method,
                acompanante=acompanante,
            )
            incr_metric("tool_calls.succeeded")
            return result

        if name == "geocode_search":
            query = args.get("query", "")
            result = await calculator.geocode_search(query=query)
            incr_metric("tool_calls.succeeded")
            return result

        if name == "geocode_details":
            place_id = args.get("place_id", "")
            result = await calculator.geocode_details(place_id=place_id)
            incr_metric("tool_calls.succeeded")

            lat = result.get("lat") if isinstance(result, dict) else None
            lng = result.get("lng") if isinstance(result, dict) else None
            if lat is not None and lng is not None and "error" not in result:
                session.setdefault("pending_coords", []).append(
                    {"lat": float(lat), "lng": float(lng)},
                )
                display_name = (result.get("display_name") or result.get("name") or "")[:300]
                location_payload = {
                    "longitude": float(lng),
                    "latitude": float(lat),
                    "name": display_name[:100] or "Ubicación",
                    "address": display_name,
                }
                bot = await get_bot_user_async()
                msg = await sync_to_async(Message.objects.create)(
                    conversation=conversation,
                    direction="outbound",
                    message_type="location",
                    content=f"{location_payload['name']} ({lat}, {lng})",
                    sender_name="Bot",
                    sender=bot,
                    metadata={"location": location_payload},
                )
                await sync_to_async(lambda: Conversation.objects.filter(
                    id=conversation.id,
                ).update(
                    last_message=f"📍 {location_payload['name']}"[:255],
                    last_message_at=timezone.now(),
                ))()
                msg_data = await sync_to_async(lambda: MessageSerializer(msg).data)()
                await sync_to_async(publish_conversation_update)(conversation, msg_data)
                _send_pool.submit(
                    send_whatsapp_outbound,
                    'location', location_payload,
                    conversation.contact_phone, msg.id, conversation.id,
                )
                result = dict(result)
                result["map_sent"] = True

            return result

        if name == "send_interactive":
            itype = args.get("type", "button")
            body = args.get("body", "")
            header = args.get("header")
            footer = args.get("footer")

            # Validate payload before sending
            validation_error = _validate_interactive_payload(itype, args)
            if validation_error:
                logger.warning("Invalid interactive payload: %s", validation_error)
                return {"error": f"Payload inválido: {validation_error}"}

            interactive_payload = {"type": itype}
            if header:
                interactive_payload["header"] = {"type": "text", "text": header}
            interactive_payload["body"] = {"text": body}
            if footer:
                interactive_payload["footer"] = {"text": footer}

            if itype == "button":
                buttons = args.get("buttons", [])
                interactive_payload["action"] = {
                    "buttons": [
                        {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
                        for b in buttons[:3]
                    ],
                }
            elif itype == "list":
                button_label = (args.get("button_label") or "Opciones")[:20]
                sections = args.get("sections", [])
                interactive_payload["action"] = {
                    "button": button_label,
                    "sections": [
                        {
                            "title": (sec.get("title", "") or "")[:24],
                            "rows": [
                                {
                                    "id": row["id"],
                                    "title": (row.get("title", "") or "")[:24],
                                    "description": (row.get("description", "") or "")[:72],
                                }
                                for row in sec.get("rows", [])[:10]
                            ],
                        }
                        for sec in sections
                    ],
                }

            _send_pool.submit(
                send_whatsapp_outbound,
                'interactive', interactive_payload,
                conversation.contact_phone, None, conversation.id,
            )

            bot = await get_bot_user_async()
            last_msg_text = body[:255] or 'Mensaje interactivo'
            msg = await sync_to_async(Message.objects.create)(
                conversation=conversation,
                direction="outbound",
                message_type="interactive",
                content=last_msg_text,
                sender_name="Bot",
                sender=bot,
                metadata={"interactive": interactive_payload},
            )
            await sync_to_async(lambda: Conversation.objects.filter(
                id=conversation.id,
            ).update(
                last_message=last_msg_text,
                last_message_at=timezone.now(),
            ))()
            msg_data = await sync_to_async(lambda: MessageSerializer(msg).data)()
            await sync_to_async(publish_conversation_update)(conversation, msg_data)

            incr_metric("tool_calls.succeeded")
            return {"success": True, "message": "Mensaje interactivo enviado"}

        if name == "escalate_to_human":
            await _release_bot_take(conversation, escalated=True)
            reason = (args.get("reason", "") or "")[:250]
            bot = await get_bot_user_async()
            if bot:
                await sync_to_async(ConversationNote.create_note)(
                    conversation=conversation,
                    content=f"[Bot] {reason}",
                    expiry_type='custom',
                    custom_expiry_minutes=10,
                    created_by=bot,
                )
            incr_metric("tool_calls.succeeded")
            return {
                "success": True,
                "message": "La conversación ha sido escalada a un agente humano.",
            }

        return {"error": f"Función desconocida: {name}"}
    except Exception as e:
        logger.warning("Tool %s failed: %s", name, e)
        incr_metric("tool_calls.failed")
        session["fallback_count"] = session.get("fallback_count", 0) + 1
        return {"error": str(e), "hint": "Por favor verifica los datos e intenta de nuevo."}


# ---------------------------------------------------------------------------
#  Content builder
# ---------------------------------------------------------------------------

def _build_contents(session):
    history = session.get("history", [])
    contents = []
    for msg in history[-10:]:
        role = "user" if msg.get("role") == "user" else "model"
        text = msg.get("content", "")
        if text:
            text = text[:300]
            contents.append(genai_types.Content(
                role=role,
                parts=[genai_types.Part.from_text(text=text)],
            ))
    return contents


# ---------------------------------------------------------------------------
#  Main entry point
# ---------------------------------------------------------------------------

async def handle_with_llm(session, conversation):
    logger.info("LLM handling conv=%s", conversation.id)

    client = _get_client()
    if client is None:
        logger.warning("No Gemini client — escalating conv=%s", conversation.id)
        await _release_bot_take(conversation, escalated=True)
        bot = await get_bot_user_async()
        if bot:
            await sync_to_async(ConversationNote.create_note)(
                conversation=conversation,
                content="[Bot] Bot no configurado — escalado autom\u00e1ticamente",
                expiry_type='custom',
                custom_expiry_minutes=10,
                created_by=bot,
            )
        return "Un asesor humano te atender\u00e1 pronto.", True, False

    contents = _build_contents(session)

    if not contents:
        return WELCOME_REPLY, False, False

    system_prompt = await _build_system_prompt()

    temperature = getattr(settings, "BOT_LLM_TEMPERATURE", 0.25)
    max_tokens = getattr(settings, "BOT_LLM_MAX_OUTPUT_TOKENS", 1024)

    config = genai_types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=DEFAULT_TOOLS,
        temperature=temperature,
        max_output_tokens=max_tokens,
    )

    model = "gemini-3.1-flash-lite"
    max_tool_calls = 4
    tool_call_count = 0
    escalated = False
    interactive_text = None

    try:
        response = await _generate_with_retry(client, model, contents, config)
        incr_metric("llm.calls")

        while (
            tool_call_count < max_tool_calls
            and response.candidates
            and response.candidates[0].content.parts
            and response.candidates[0].content.parts[0].function_call
        ):
            function_call = response.candidates[0].content.parts[0].function_call
            tool_call_count += 1
            logger.info("LLM tool call: %s (attempt %d)", function_call.name, tool_call_count)

            result = await _execute_tool(function_call, conversation, session)

            if function_call.name == "send_interactive":
                if isinstance(result, dict) and "error" in result:
                    # Validation/payload error — feed result back to LLM so it retries
                    contents.append(response.candidates[0].content)
                    contents.append(genai_types.Content(
                        role="function",
                        parts=[genai_types.Part.from_function_response(
                            name=function_call.name,
                            response={"result": result},
                        )],
                    ))
                    response = await _generate_with_retry(client, model, contents, config)
                    incr_metric("llm.calls")
                    continue
                # Success — break out; the interactive was sent
                if function_call.args:
                    interactive_text = (function_call.args.get("body") or "")[:300]
                break

            if function_call.name == "escalate_to_human":
                escalated = True

            contents.append(response.candidates[0].content)

            contents.append(genai_types.Content(
                role="function",
                parts=[genai_types.Part.from_function_response(
                    name=function_call.name,
                    response={"result": result},
                )],
            ))

            response = await _generate_with_retry(client, model, contents, config)
            incr_metric("llm.calls")

        if interactive_text is not None:
            reply = interactive_text
        elif response.candidates and response.candidates[0].content.parts:
            reply = response.candidates[0].content.parts[0].text or ""
        else:
            reply = ""

        # --- Output sanitization ---
        reply = sanitize_llm_output(reply)

    except Exception as e:
        logger.exception("Error en Gemini API para conv %s: %s", conversation.id, e)
        incr_metric("llm.failures")
        session["fallback_count"] = session.get("fallback_count", 0) + 1
        reply = "Ocurri\u00f3 un error al procesar tu mensaje. Un asesor te atender\u00e1 pronto."
        await _release_bot_take(conversation, escalated=True)
        bot = await get_bot_user_async()
        if bot:
            await sync_to_async(ConversationNote.create_note)(
                conversation=conversation,
                content="[Bot] Error del sistema al procesar — escalado autom\u00e1ticamente",
                expiry_type='custom',
                custom_expiry_minutes=10,
                created_by=bot,
            )
        escalated = True

    return reply.strip(), escalated, interactive_text is not None
