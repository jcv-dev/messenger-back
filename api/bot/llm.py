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
from api.models import Conversation, ConversationTake, Message
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


async def _release_bot_take(conversation):
    bot = await get_bot_user_async()
    if bot:
        await sync_to_async(lambda: ConversationTake.objects.filter(
            created_by=bot,
            conversation=conversation,
            expires_at__gt=timezone.now(),
        ).update(expires_at=timezone.now()))()
    await sync_to_async(publish_conversation_update)(conversation)


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

    return f"""Eres el asistente virtual de Domii Tuluá, una empresa de domicilios y mensajería en Tuluá, Colombia.

IDIOMA:
- Responde SIEMPRE en español colombiano. NUNCA uses inglés bajo ninguna circunstancia.
- Sé amable, profesional y cercano.

FORMATO DE WHATSAPP (importante):
- *texto* = negrita (UN solo asterisco a cada lado, NO dos)
- _texto_ = cursiva
- ~texto~ = tachado
- Listas usa: "1. item\n2. item\n3. item"
- Saltos de línea: usa \n entre párrafos
- Precios y totales siempre en negrita: *$4,700 COP*

MENSAJES INTERACTIVOS:
- No uses interactivos para todo. Mezcla naturalmente según el contexto.
- USA send_interactive cuando el usuario deba elegir entre opciones concretas:
  * Menú inicial: type="button" — "Cotizar servicio", "Domii Fijo", "Preguntas", "Asesor"
  * Selección de perfil: type="button" — "Cliente final", "Negocio"
  * Selección de servicio: type="list" con los 5 tipos de servicio
  * Método de pago: type="button" — "Efectivo", "Nequi"
  * Confirmaciones sí/no: type="button" — "Sí", "No"
  * Resultados de geocoding: type="button" con place_id como ID
- USA TEXTO NORMAL para:
  * Explicaciones del servicio
  * Resultados de precios (formatea bien con *negrita*)
  * Respuestas de preguntas frecuentes
  * Conversación natural sin opciones fijas
- send_interactive(type="button") → hasta 3 botones
- send_interactive(type="list") → hasta 10 opciones en secciones
- IDs cortos y descriptivos: "domicilios", "efectivo", "si", "no", "cliente_final"
- Después de send_interactive, detente por completo. No generes más texto ni llames más herramientas. Espera la respuesta del usuario.
- Cuando el usuario responda a un interactivo, llegará como texto con el ID

Tu función es ayudar a los clientes a cotizar un servicio de domicilio o mensajería, solicitar un Domii Fijo (domiciliario dedicado), responder preguntas frecuentes, o escalar a un agente humano cuando sea necesario.

SERVICIOS:
- Domicilios: envíos de todo tipo dentro de Tuluá
- Mensajería: envíos urgentes de documentos o paquetes pequeños
- Compras por encargo
- Trámites: gestión de documentos
- Bancarios: diligencias bancarias
- Domii Fijo: domiciliario dedicado por horas/días (para negocios)

HORARIOS:
- Lunes a sábado: 8:00 AM a 8:00 PM
- Domingos y festivos: 9:00 AM a 6:00 PM

COBERTURA:
- Casco urbano de Tuluá y veredas cercanas
- Destinos fuera del área (Cali, Buga, etc.) aplican tarifas fijas

MÉTODOS DE PAGO:
- Efectivo (sin recargo)
- Nequi (+$500 COP de recargo)
- El pago se realiza al recibir el domicilio

HERRAMIENTAS DISPONIBLES:
{tools_text}

FLUJO PARA COTIZAR UN SERVICIO:
1. Perfil: usuario final (usuario_final) o negocio (negocio)
2. Tipo de servicio: domicilios, mensajería, compras por encargo, trámites o bancarios
3. Dirección de origen — si el cliente da un nombre (ej: "La herradura"), usa geocode_search para buscar direcciones. Si hay varios resultados, preséntalos con send_interactive(type="button") donde cada botón tenga id=place_id y title=display_name. Si un solo resultado, usa geocode_details directamente. Si dice "centro" usa "Tuluá centro" con lat 4.0847, lng -76.1954
4. Dirección de destino — igual que origen, usa geocoding si es necesario
5. ¿Más paradas? Si sí, volver al paso 3. Si no, continuar.
6. Herramientas adicionales: las herramientas disponibles vienen del sistema y son dinámicas. Comunica al usuario las herramientas disponibles con sus descripciones y pídele que escriba cuáles necesita (ej: "canasta y maletín", "solo canasta", "ninguna"). El usuario puede escribir varias. Extrae los tool keys del texto del usuario. Si el usuario no necesita herramientas, tools=[] .
7. Método de pago: efectivo o Nequi
8. ¿Necesitas que el domiciliario lleve un acompañante? (ej: para cargar objetos pesados como tortas, paquetes grandes) → sí o no
9. Calcular precio usando la herramienta calculate_price
10. Cuando calculate_price devuelva el resultado, preséntalo al cliente:
    - Total: $X COP
    - Distancia: X km
    - Método de pago: efectivo/Nequi
    - Si Nequi, el total incluye +$500 de recargo
    - Si está lloviendo (weather.is_raining), puede haber recargo por lluvia
    - Pregunta si confirma el pedido
11. Si confirma, pregunta el nombre y teléfono de la persona que recibirá el pedido
12. Indicar que el pedido ha sido enviado exitosamente con los datos ingresados

FLUJO DOMII FIJO:
1. Nombre del negocio
2. Dirección del negocio
3. Teléfono del negocio
4. Fecha del servicio
5. Hora de inicio
6. Hora de fin
7. Volumen estimado (1-5, 5-15, o +15 pedidos)
8. Mostrar resumen y confirmar
9. Indicar que la solicitud ha sido enviada

GEOCODING:
- Usa geocode_search para buscar direcciones por nombre.
- Si hay múltiples resultados, preséntalos con send_interactive(type="button").
- El ID de cada botón debe ser el place_id del resultado, title el display_name.
- Ejemplo: send_interactive(type="button", body="Selecciona la dirección correcta:",
    buttons=[{{"id":"ChIJvX8...","title":"La Herradura, Tuluá"}},
             {{"id":"ChIJTU8...","title":"La Herradura, Palmira"}}])
- Cuando el usuario seleccione, recibirás el place_id como texto. Llama geocode_details.
- Si hay un solo resultado, usa geocode_details directamente sin preguntar.
- Si no hay resultados, informa al usuario: "No encontré esa dirección. Intenta con más detalles (barrio, puntos de referencia) o comparte tu ubicación."
- Las coordenadas (lat, lng) son necesarias para calculate_price.
- Si el usuario da una dirección precisa (ej: "Calle 10 #20-30, Tuluá"), pásala directamente sin geocoding.

ERRORES:
- Si calculate_price falla o devuelve error, EXPLICA al usuario qué falta (ej: "Necesito la dirección de destino", "Faltan herramientas por seleccionar").
- Si geocode_search no encuentra nada, sugiere alternativas: "No encontré esa dirección. Intenta con más detalles o comparte tu ubicación por WhatsApp."
- Si geocode_details falla, pide al usuario confirmar la dirección manualmente.
- Si un error ocurre al enviar el pedido, informa con claridad: "Ocurrió un error al procesar tu pedido. Un asesor te ayudará."
- NUNCA muestres errores técnicos (códigos, JSON, tracebacks) al usuario.
- Si el problema persiste después de intentar ayudar, ofrece escalate_to_human.

REGLAS IMPORTANTES:
- NO saludes en cada mensaje. Solo saluda en el primer mensaje de la conversación.
- Si el cliente ya está en medio de un flujo (eligiendo perfil, servicio, pago, etc.), responde directo y conciso sin preámbulos ni saludos.
- Respuestas concisas (máximo 300 caracteres)
- Si el cliente se desvía, guíalo de vuelta amablemente
- Usa la herramienta escalate_to_human si:
  1. El cliente pide explícitamente un asesor humano (agente, persona, asesor, operador)
  2. Después de 3 intentos el cliente no logra completar el flujo
- Para calcular precios USA SIEMPRE la herramienta calculate_price (nunca inventes precios)
- Después de completar un pedido, agradece al cliente y pregunta si necesita algo más
- Si el cliente escribe "salir", "cancelar" o "menú", responde SOLO con el mensaje de bienvenida estándar"""


DEFAULT_TOOLS = [
    genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="calculate_price",
                description="Calcula el precio de un domicilio. Llámala cuando tengas todos los datos del cliente (perfil, segmentos, herramientas, método de pago, acompañante).",
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "profile": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            enum=["usuario_final", "negocio"],
                            description="usuario_final para cliente final, negocio para negocio",
                        ),
                        "segments": genai_types.Schema(
                            type=genai_types.Type.ARRAY,
                            items=genai_types.Schema(
                                type=genai_types.Type.OBJECT,
                                properties={
                                    "service_type": genai_types.Schema(
                                        type=genai_types.Type.STRING,
                                        enum=["domicilios", "mensajeria", "purchases", "tramites", "bancarios"],
                                        description="Tipo de servicio para este segmento",
                                    ),
                                    "description": genai_types.Schema(
                                        type=genai_types.Type.STRING,
                                        description="Descripción opcional del segmento",
                                    ),
                                    "origin": genai_types.Schema(
                                        type=genai_types.Type.OBJECT,
                                        properties={
                                            "address": genai_types.Schema(type=genai_types.Type.STRING, description="Dirección de origen"),
                                            "lat": genai_types.Schema(type=genai_types.Type.NUMBER, description="Latitud (null si no se conoce)", nullable=True),
                                            "lng": genai_types.Schema(type=genai_types.Type.NUMBER, description="Longitud (null si no se conoce)", nullable=True),
                                        },
                                        required=["address", "lat", "lng"],
                                    ),
                                    "destination": genai_types.Schema(
                                        type=genai_types.Type.OBJECT,
                                        properties={
                                            "address": genai_types.Schema(type=genai_types.Type.STRING, description="Dirección de destino"),
                                            "lat": genai_types.Schema(type=genai_types.Type.NUMBER, description="Latitud (null si no se conoce)", nullable=True),
                                            "lng": genai_types.Schema(type=genai_types.Type.NUMBER, description="Longitud (null si no se conoce)", nullable=True),
                                        },
                                        required=["address", "lat", "lng"],
                                    ),
                                    "instructions": genai_types.Schema(
                                        type=genai_types.Type.STRING,
                                        description="Instrucciones adicionales para el domiciliario",
                                        nullable=True,
                                    ),
                                },
                                required=["service_type", "origin", "destination"],
                            ),
                        ),
                        "tools": genai_types.Schema(
                            type=genai_types.Type.ARRAY,
                            items=genai_types.Schema(type=genai_types.Type.STRING),
                            description="Lista de herramientas adicionales (keys del sistema de herramientas)",
                        ),
                        "payment_method": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            enum=["efectivo", "nequi"],
                            description="Método de pago",
                        ),
                        "acompanante": genai_types.Schema(
                            type=genai_types.Type.BOOLEAN,
                            description="true si el domiciliario necesita un acompañante para cargar objetos pesados, false si no",
                        ),
                    },
                    required=["profile", "segments", "payment_method", "acompanante"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="geocode_search",
                description="Busca direcciones por nombre para geocodificación. Devuelve una lista de resultados con display_name (nombre legible) y place_id (para obtener coordenadas). Úsala cuando el cliente dé direcciones por nombre (ej: 'La herradura', 'supercentro').",
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "query": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            description="Dirección o lugar a buscar",
                        ),
                    },
                    required=["query"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="geocode_details",
                description="Obtiene coordenadas exactas (lat, lng) de un place_id obtenido con geocode_search. Llámala después de que el usuario confirme la dirección correcta.",
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "place_id": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            description="Place ID del resultado de geocode_search",
                        ),
                    },
                    required=["place_id"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="send_interactive",
                description="Envía un mensaje interactivo con botones o lista de opciones. Úsala para menús, selecciones y confirmaciones en vez de texto numerado.",
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "type": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            enum=["button", "list"],
                            description="button para hasta 3 botones de respuesta rápida, list para una lista de opciones",
                        ),
                        "header": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            description="Texto del encabezado (opcional, máximo 60 caracteres)",
                        ),
                        "body": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            description="Texto principal del mensaje",
                        ),
                        "footer": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            description="Texto del pie de página (opcional, máximo 60 caracteres)",
                        ),
                        "button_label": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            description="Texto del botón para listas (type=list). Máximo 20 caracteres.",
                        ),
                        "buttons": genai_types.Schema(
                            type=genai_types.Type.ARRAY,
                            items=genai_types.Schema(
                                type=genai_types.Type.OBJECT,
                                properties={
                                    "id": genai_types.Schema(type=genai_types.Type.STRING, description="ID único que identifica la opción"),
                                    "title": genai_types.Schema(type=genai_types.Type.STRING, description="Texto visible del botón (máximo 20 caracteres)"),
                                },
                                required=["id", "title"],
                            ),
                            description="Botones para type=button. Máximo 3 botones.",
                        ),
                        "sections": genai_types.Schema(
                            type=genai_types.Type.ARRAY,
                            items=genai_types.Schema(
                                type=genai_types.Type.OBJECT,
                                properties={
                                    "title": genai_types.Schema(type=genai_types.Type.STRING, description="Título de la sección (máximo 24 caracteres)"),
                                    "rows": genai_types.Schema(
                                        type=genai_types.Type.ARRAY,
                                        items=genai_types.Schema(
                                            type=genai_types.Type.OBJECT,
                                            properties={
                                                "id": genai_types.Schema(type=genai_types.Type.STRING, description="ID único de la fila"),
                                                "title": genai_types.Schema(type=genai_types.Type.STRING, description="Título de la fila (máximo 24 caracteres)"),
                                                "description": genai_types.Schema(type=genai_types.Type.STRING, description="Descripción breve (máximo 72 caracteres, opcional)"),
                                            },
                                            required=["id", "title"],
                                        ),
                                        description="Filas de la sección. Máximo 10 filas combinadas entre todas las secciones.",
                                    ),
                                },
                                required=["title", "rows"],
                            ),
                            description="Secciones para type=list. Cada sección tiene título y filas.",
                        ),
                    },
                    required=["type", "body"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="escalate_to_human",
                description="Escala la conversación a un agente humano cuando el cliente lo solicite o no se pueda ayudar.",
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "reason": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            description="Razón de la escalación",
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
                    "sections": sections,
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

            return {"success": True, "message": "Mensaje interactivo enviado"}

        if name == "escalate_to_human":
            await _release_bot_take(conversation)
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
    for msg in history[-20:]:
        role = "user" if msg.get("role") == "user" else "model"
        text = msg.get("content", "")
        if text:
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
        await _release_bot_take(conversation)
        return "Un asesor humano te atenderá pronto.", True, False

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
    max_tool_calls = 5
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
        reply = "Ocurrió un error al procesar tu mensaje. Un asesor te atenderá pronto."
        await _release_bot_take(conversation)
        escalated = True

    return reply.strip(), escalated, interactive_text is not None
