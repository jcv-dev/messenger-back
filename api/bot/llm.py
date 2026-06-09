import logging

from asgiref.sync import sync_to_async

from django.conf import settings
from django.utils import timezone

from google import genai
from google.genai import types as genai_types

from . import calculator
from api.models import Conversation, ConversationTake, Message
from api.serializers import MessageSerializer
from api.views import publish_conversation_update, send_whatsapp_outbound, _send_pool

logger = logging.getLogger("api.bot.llm")

_tools_cache = None


def _get_bot_user():
    from django.contrib.auth.models import User
    return User.objects.filter(username="bot").first()


_get_bot_user_async = sync_to_async(_get_bot_user)


async def _release_bot_take(conversation):
    bot = await _get_bot_user_async()
    if bot:
        await sync_to_async(lambda: ConversationTake.objects.filter(
            created_by=bot,
            conversation=conversation,
            expires_at__gt=timezone.now(),
        ).update(expires_at=timezone.now()))()
    await sync_to_async(publish_conversation_update)(conversation)


async def _build_system_prompt():
    global _tools_cache
    if _tools_cache is None:
        try:
            _tools_cache = await calculator.get_tools()
        except Exception:
            _tools_cache = []
            logger.warning("Failed to fetch tools from calculator, using empty list")

    tools_text = "\n".join(
        f'  - "{t["key"]}": {t["label"]} — {t["description"]}'
        for t in (_tools_cache or [])
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
  * Menú inicial: type="button" con 3-4 botones
  * Selección de perfil: type="button" (cliente final / negocio)
  * Selección de servicio: type="list" con los 5 tipos de servicio
  * Método de pago: type="button" (efectivo / Nequi)
  * Confirmaciones sí/no: type="button"
  * Herramientas: type="list"
- USA TEXTO NORMAL para:
  * Saludos y bienvenidas
  * Explicaciones del servicio
  * Resultados de precios (formatea bien con *negrita*)
  * Resultados de geocoding (muestra opciones)
  * Respuestas de preguntas frecuentes
  * Conversación natural sin opciones fijas
- send_interactive(type="button") → hasta 3 botones
- send_interactive(type="list") → hasta 10 opciones en secciones
- IDs cortos y descriptivos: "domicilios", "efectivo", "si", "no", "cliente_final"
- Después de send_interactive, detente por completo. No generes más texto ni llames más herramientas. Espera la respuesta del usuario.
- Cuando el usuario responda a un interactivo, llegará como texto con el ID

Tu función es ayudar a los clientes a calcular el precio de un domicilio, solicitar un Domii Fijo (domiciliario dedicado), responder preguntas frecuentes, o escalar a un agente humano cuando sea necesario.

SERVICIOS:
- Domicilios: envíos de todo tipo dentro de Tuluá
- Mensajería: envíos urgentes de documentos o paquetes pequeños
- Purchases: compras por encargo
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

FLUJO PARA CALCULAR UN DOMICILIO:
1. Perfil: usuario final (usuario_final) o negocio (negocio)
2. Tipo de servicio: domicilios, mensajería, purchases, trámites o bancarios
3. Dirección de origen — si el cliente da un nombre (ej: "La herradura"), usa geocode_search para buscar direcciones y geocode_details para obtener coordenadas exactas. Si dice "centro" usa "Tuluá centro" con lat 4.0847, lng -76.1954
4. Dirección de destino — igual que origen, usa geocoding si es necesario
5. ¿Más paradas? Si sí, volver al paso 3. Si no, continuar.
6. Herramientas adicionales: selecciona de la lista de herramientas disponibles (usa la key)
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
- Siempre usa geocode_search para buscar direcciones por nombre (ej: "La herradura", "supercentro", "barrio popular")
- geocode_search devuelve resultados con display_name y place_id
- El cliente debe confirmar la dirección correcta (puedes mostrar las opciones)
- Luego usa geocode_details(place_id) para obtener las coordenadas exactas (lat, lng)
- Las coordenadas son necesarias para calculate_price
- Si la dirección no necesita geocodificación (ej: dirección escrita completa), puedes pasarla directamente sin coordenadas

REGLAS IMPORTANTES:
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


_llm_client = None


def _get_client():
    global _llm_client
    if _llm_client is None:
        key = settings.GEMINI_API_KEY
        if not key:
            raise RuntimeError("GEMINI_API_KEY no está configurada")
        _llm_client = genai.Client(api_key=key)
    return _llm_client


async def _execute_tool(function_call, conversation):
    name = function_call.name
    args = dict(function_call.args) if function_call.args else {}

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
        return result

    if name == "geocode_search":
        query = args.get("query", "")
        result = await calculator.geocode_search(query=query)
        return result

    if name == "geocode_details":
        place_id = args.get("place_id", "")
        result = await calculator.geocode_details(place_id=place_id)
        return result

    if name == "send_interactive":
        itype = args.get("type", "button")
        body = args.get("body", "")
        header = args.get("header")
        footer = args.get("footer")

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

        bot = await _get_bot_user_async()
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


async def handle_with_llm(session, conversation):
    logger.info("LLM handling conv=%s", conversation.id)
    client = _get_client()

    contents = _build_contents(session)

    if not contents:
        reply = "¡Bienvenido a Domii Tuluá! 🚀\n\nSoy el asistente virtual. ¿Qué deseas hacer?\n\n1. Calcular un domicilio o mensajería\n2. Domii Fijo (domiciliario dedicado)\n3. Hablar con un asesor\n4. Preguntas frecuentes\n\nResponde con el número de la opción."
        return reply, False

    system_prompt = await _build_system_prompt()

    config = genai_types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=DEFAULT_TOOLS,
        temperature=0.7,
        max_output_tokens=1024,
    )

    model = "gemini-3.1-flash-lite"
    max_tool_calls = 5
    tool_call_count = 0
    escalated = False

    try:
        response = await client.aio.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )

        while (
            tool_call_count < max_tool_calls
            and response.candidates
            and response.candidates[0].content.parts
            and response.candidates[0].content.parts[0].function_call
        ):
            function_call = response.candidates[0].content.parts[0].function_call
            tool_call_count += 1
            logger.info("LLM tool call: %s (attempt %d)", function_call.name, tool_call_count)

            result = await _execute_tool(function_call, conversation)

            if function_call.name == "send_interactive":
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

            response = await client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )

        if response.candidates and response.candidates[0].content.parts:
            reply = response.candidates[0].content.parts[0].text or ""
        else:
            reply = ""
    except Exception as e:
        logger.exception("Error en Gemini API para conv %s: %s", conversation.id, e)
        reply = "Ocurrió un error al procesar tu mensaje. Un asesor te atenderá pronto."
        await _release_bot_take(conversation)
        escalated = True

    return reply.strip(), escalated
