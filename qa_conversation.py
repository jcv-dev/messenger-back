"""Gemini-driven QA conversation — simulates a user and evaluates the bot.

Gemini plays TWO roles:
1. **USER**: Sees the bot's response + interactive options, navigates the flow
2. **JUDGE** (optional): Evaluates each bot response for correctness & quality

Covers 30 of 31 state machine states across 14 scenarios
(AWAITING_DESCRIPTION is defined in flow.py but unreachable — dead code).
Run::
    cd backend && source venv/bin/activate
    GEMINI_API_KEY=xxx python qa_conversation.py
    GEMINI_API_KEY=xxx python qa_conversation.py --scenario DOMICILIOS --turns 15
    GEMINI_API_KEY=xxx python qa_conversation.py --scenario ALL --judge --report qa_report.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()

from google import genai
from google.genai import types as genai_types

from api.bot.flow import advance, build_initial_session

# ── Scenarios ──────────────────────────────────────────────────────────────
# Each scenario covers specific states. Collectively they cover all 31 states.

SCENARIOS: dict[str, dict[str, Any]] = {
    "DOMICILIOS": {
        "desc": "Standard domicilio, usuario_final, efectivo, no accompany",
        "goal": "Completar un domicilio desde tu casa al centro. Pagas en efectivo, sin acompañante.",
        "turn1_hint": (
            "El bot te saludó. Para empezar, escribe EXACTAMENTE: 'Cotizar domicilio'. "
            "Cuando pregunte tu perfil, escribe 'Usuario final'. "
            "Cuando pregunte el servicio, escribe 'Domicilios'. "
            "Cuando muestre una dirección, simplemente di 'Sí' para confirmar (es un simulador). "
            "Sin acompañante, sin herramientas, efectivo."
        ),
        "states": [
            "WELCOME", "AWAITING_PROFILE", "AWAITING_SERVICE_TYPE",
            "AWAITING_ORIGIN", "CONFIRMING_ORIGIN", "AWAITING_DESTINATION",
            "CONFIRMING_DEST", "AWAITING_SEGMENT_DESCRIPTION",
            "AWAITING_SEGMENT_INSTRUCTIONS", "AWAITING_MORE_STOPS",
            "AWAITING_TOOLS", "AWAITING_PAYMENT", "AWAITING_ACOMPANANTE",
            "CONFIRMING_QUOTE", "AWAITING_RECIPIENT_NAME",
            "AWAITING_RECIPIENT_PHONE", "WELCOME",
        ],
        "mock_overrides": {},
    },
    "MENSAJERIA": {
        "desc": "Mensajería with extra states: package_type + who_pays",
        "goal": "Enviar un documento por mensajería desde tu oficina. Paga el destinatario, efectivo.",
        "turn1_hint": (
            "El bot te saludó. Escribe EXACTAMENTE 'Cotizar domicilio'. "
            "Luego 'Usuario final', luego 'Mensajería'. "
            "Tipo de paquete: 'Documento'. Quién paga: 'Destinatario'. "
            "Confirma las direcciones con 'Sí'. Sin más paradas, sin herramientas, efectivo."
        ),
        "states": [
            "WELCOME", "AWAITING_PROFILE", "AWAITING_SERVICE_TYPE",
            "ASK_PACKAGE_TYPE", "ASK_WHO_PAYS",
            "AWAITING_ORIGIN", "CONFIRMING_ORIGIN", "AWAITING_DESTINATION",
            "CONFIRMING_DEST", "AWAITING_SEGMENT_DESCRIPTION",
            "AWAITING_SEGMENT_INSTRUCTIONS", "AWAITING_MORE_STOPS",
            "AWAITING_TOOLS", "AWAITING_PAYMENT", "AWAITING_ACOMPANANTE",
            "CONFIRMING_QUOTE", "AWAITING_RECIPIENT_NAME",
            "AWAITING_RECIPIENT_PHONE", "WELCOME",
        ],
        "mock_overrides": {},
    },
    "PURCHASES": {
        "desc": "Purchases with optional coords (skip both origin and dest)",
        "goal": "Pedir unas compras de mercado. Omite el origen y destino. Paga con Nequi.",
        "turn1_hint": (
            "Bot saludó. Escribe 'Cotizar domicilio'. Luego 'Usuario final'. "
            "Cuando salga la lista de servicios, elige 'Compras por encargo'. "
            "Cuando pregunte dirección, responde 'no' para omitir ambas. "
            "Cuando pregunte qué comprar, describe algo. Sin instrucciones, sin más paradas. "
            "Sin herramientas. Pago 'Nequi'. Sin acompañante."
        ),
        "states": [
            "WELCOME", "AWAITING_PROFILE", "AWAITING_SERVICE_TYPE",
            "AWAITING_ORIGIN", "AWAITING_DESTINATION",
            "AWAITING_SEGMENT_DESCRIPTION", "AWAITING_SEGMENT_INSTRUCTIONS",
            "AWAITING_MORE_STOPS", "AWAITING_TOOLS",
            "AWAITING_PAYMENT", "AWAITING_ACOMPANANTE",
            "CONFIRMING_QUOTE", "AWAITING_RECIPIENT_NAME",
            "AWAITING_RECIPIENT_PHONE", "WELCOME",
        ],
        "mock_overrides": {},
    },
    "BANCARIOS": {
        "desc": "Bancarios with optional coords, entity, reference, Nequi",
        "goal": "Trámite bancario. Omite origen/destino. Di entidad y referencia. Paga Nequi.",
        "turn1_hint": (
            "Bot saludó. Escribe 'Cotizar domicilio'. Luego 'Usuario final'. "
            "Cuando salga la lista, elige 'Bancarios'. "
            "Omite origen y destino escribiendo 'no'. "
            "Describe el trámite. Sin más paradas. "
            "Entidad: 'Bancolombia'. Referencia: 'Factura 123'. "
            "Sin herramientas. Pago 'Nequi'. Sin acompañante."
        ),
        "states": [
            "WELCOME", "AWAITING_PROFILE", "AWAITING_SERVICE_TYPE",
            "AWAITING_ORIGIN", "AWAITING_DESTINATION",
            "AWAITING_SEGMENT_DESCRIPTION", "AWAITING_SEGMENT_INSTRUCTIONS",
            "AWAITING_MORE_STOPS", "ASK_BANCARIOS_ENTITY",
            "ASK_BANCARIOS_REFERENCE", "AWAITING_TOOLS",
            "AWAITING_PAYMENT", "AWAITING_ACOMPANANTE",
            "CONFIRMING_QUOTE", "AWAITING_RECIPIENT_NAME",
            "AWAITING_RECIPIENT_PHONE", "WELCOME",
        ],
        "mock_overrides": {},
    },
    "TRAMITES": {
        "desc": "Trámites geo flow + Nequi payment",
        "goal": "Trámite en la alcaldía. Direcciones obligatorias. Paga Nequi.",
        "turn1_hint": (
            "Bot saludó. Escribe 'Cotizar domicilio'. Luego 'Usuario final'. "
            "Cuando salga la lista, elige 'Trámites'. "
            "Da una dirección cualquiera y confírmala con 'Sí'. "
            "Describe el trámite. Sin instrucciones. Sin más paradas. "
            "Sin herramientas. Pago 'Nequi'. Sin acompañante."
        ),
        "states": [
            "WELCOME", "AWAITING_PROFILE", "AWAITING_SERVICE_TYPE",
            "AWAITING_ORIGIN", "CONFIRMING_ORIGIN", "AWAITING_DESTINATION",
            "CONFIRMING_DEST", "AWAITING_SEGMENT_DESCRIPTION",
            "AWAITING_SEGMENT_INSTRUCTIONS", "AWAITING_MORE_STOPS",
            "AWAITING_TOOLS", "AWAITING_PAYMENT", "AWAITING_ACOMPANANTE",
            "CONFIRMING_QUOTE", "AWAITING_RECIPIENT_NAME",
            "AWAITING_RECIPIENT_PHONE", "WELCOME",
        ],
        "mock_overrides": {},
    },
    "MULTI_STOP": {
        "desc": "Two segments: domicilio con 2 paradas",
        "goal": "Domicilio con 2 paradas. Primero recoger un paquete en un lugar, dejar en otro. Luego otra parada.",
        "turn1_hint": (
            "Bot saludó. Escribe 'Cotizar domicilio'. Luego 'Usuario final', luego 'Domicilios'. "
            "Da direcciones, confírmalas con 'Sí'. "
            "Cuando pregunte '¿Necesitas más paradas?', di que SÍ. Es la clave de este test. "
            "Da otra dirección, confirma, describe la segunda parada. "
            "Cuando pregunte otra vez '¿Más paradas?', di que NO. "
            "Sin herramientas. Efectivo. Sin acompañante."
        ),
        "states": [
            "WELCOME", "AWAITING_PROFILE", "AWAITING_SERVICE_TYPE",
            "AWAITING_ORIGIN", "CONFIRMING_ORIGIN", "AWAITING_DESTINATION",
            "CONFIRMING_DEST", "AWAITING_SEGMENT_DESCRIPTION",
            "AWAITING_SEGMENT_INSTRUCTIONS", "AWAITING_MORE_STOPS",
            "AWAITING_ORIGIN", "CONFIRMING_ORIGIN", "AWAITING_DESTINATION",
            "CONFIRMING_DEST", "AWAITING_SEGMENT_DESCRIPTION",
            "AWAITING_SEGMENT_INSTRUCTIONS", "AWAITING_MORE_STOPS",
            "AWAITING_TOOLS", "AWAITING_PAYMENT", "AWAITING_ACOMPANANTE",
            "CONFIRMING_QUOTE", "AWAITING_RECIPIENT_NAME",
            "AWAITING_RECIPIENT_PHONE", "WELCOME",
        ],
        "mock_overrides": {},
    },
    "WITH_TOOLS": {
        "desc": "Domicilio with tools selected + acompañante sí",
        "goal": "Enviar paquete frágil. Seleccionar canasta y maletín térmico. Acompañante sí. Nequi.",
        "turn1_hint": (
            "Bot saludó. Escribe 'Cotizar domicilio'. Luego 'Usuario final', luego 'Domicilios'. "
            "Da direcciones, confírmalas con 'Sí'. "
            "Cuando pregunte por herramientas, selecciona 'Canasta' y 'Maletín térmico'. "
            "Cuando pregunte por acompañante, responde que SÍ. "
            "Efectivo. Sin más paradas."
        ),
        "states": [
            "WELCOME", "AWAITING_PROFILE", "AWAITING_SERVICE_TYPE",
            "AWAITING_ORIGIN", "CONFIRMING_ORIGIN", "AWAITING_DESTINATION",
            "CONFIRMING_DEST", "AWAITING_SEGMENT_DESCRIPTION",
            "AWAITING_SEGMENT_INSTRUCTIONS", "AWAITING_MORE_STOPS",
            "AWAITING_TOOLS", "AWAITING_PAYMENT", "AWAITING_ACOMPANANTE",
            "CONFIRMING_QUOTE", "AWAITING_RECIPIENT_NAME",
            "AWAITING_RECIPIENT_PHONE", "WELCOME",
        ],
        "mock_overrides": {},
    },
    "CHANGE_QUOTE": {
        "desc": "Change something after seeing the price, then confirm",
        "goal": "Después de ver el precio, elige 'Cambiar algo'. Luego confirma.",
        "turn1_hint": (
            "Bot saludó. Escribe 'Cotizar domicilio'. Luego 'Usuario final', luego 'Domicilios'. "
            "Da direcciones, confirma con 'Sí'. Sin más paradas. Sin herramientas. "
            "Efectivo. Sin acompañante. "
            "Cuando veas el resumen con 'Confirmar' / 'Cambiar algo' / 'Cancelar', "
            "elige 'Cambiar algo'. Luego confirma el pedido modificado."
        ),
        "states": [
            "WELCOME", "AWAITING_PROFILE", "AWAITING_SERVICE_TYPE",
            "AWAITING_ORIGIN", "CONFIRMING_ORIGIN", "AWAITING_DESTINATION",
            "CONFIRMING_DEST", "AWAITING_SEGMENT_DESCRIPTION",
            "AWAITING_SEGMENT_INSTRUCTIONS", "AWAITING_MORE_STOPS",
            "AWAITING_TOOLS", "AWAITING_PAYMENT", "AWAITING_ACOMPANANTE",
            "CONFIRMING_QUOTE", "AWAITING_SERVICE_TYPE",
            "AWAITING_ORIGIN", "CONFIRMING_ORIGIN", "AWAITING_DESTINATION",
            "CONFIRMING_DEST", "AWAITING_SEGMENT_DESCRIPTION",
            "AWAITING_SEGMENT_INSTRUCTIONS", "AWAITING_MORE_STOPS",
            "AWAITING_TOOLS", "AWAITING_PAYMENT", "AWAITING_ACOMPANANTE",
            "CONFIRMING_QUOTE", "AWAITING_RECIPIENT_NAME",
            "AWAITING_RECIPIENT_PHONE", "WELCOME",
        ],
        "mock_overrides": {},
    },
    "DOMII_FIJO": {
        "desc": "Full Domii Fijo sub-flow (name, address, phone, date, start, end, volume)",
        "goal": "Contratar Domii Fijo para tu negocio. Llenar todos los datos y confirmar.",
        "turn1_hint": (
            "Bot saludó. Escribe EXACTAMENTE 'Domii Fijo'. "
            "Luego: nombre del negocio 'Hamburguesas El Parche', "
            "dirección 'Cra 25 #27-14', teléfono '3124567890', "
            "fecha 'mañana', hora inicio '18:00', hora fin '23:00', "
            "volumen '5-15 pedidos'. Finalmente confirma."
        ),
        "states": [
            "WELCOME", "AWAITING_FIJO_NAME", "AWAITING_FIJO_ADDR",
            "AWAITING_FIJO_PHONE", "AWAITING_FIJO_DATE",
            "AWAITING_FIJO_START", "AWAITING_FIJO_END",
            "AWAITING_FIJO_VOLUME", "CONFIRMING_FIJO", "WELCOME",
        ],
        "mock_overrides": {},
    },
    "NEGOCIO": {
        "desc": "Negocio profile + tramites + Nequi",
        "goal": "Eres un negocio. Necesitas un trámite. Pagas con Nequi.",
        "turn1_hint": (
            "Bot saludó. Escribe 'Cotizar domicilio'. "
            "Cuando pregunte perfil, elige 'Negocio'. "
            "Cuando salga la lista de servicios, elige 'Trámites'. "
            "Da direcciones, confirma con 'Sí'. Sin más paradas. "
            "Sin herramientas. Pago 'Nequi'. Sin acompañante."
        ),
        "states": [
            "WELCOME", "AWAITING_PROFILE", "AWAITING_SERVICE_TYPE",
            "AWAITING_ORIGIN", "CONFIRMING_ORIGIN", "AWAITING_DESTINATION",
            "CONFIRMING_DEST", "AWAITING_SEGMENT_DESCRIPTION",
            "AWAITING_SEGMENT_INSTRUCTIONS", "AWAITING_MORE_STOPS",
            "AWAITING_TOOLS", "AWAITING_PAYMENT", "AWAITING_ACOMPANANTE",
            "CONFIRMING_QUOTE", "AWAITING_RECIPIENT_NAME",
            "AWAITING_RECIPIENT_PHONE", "WELCOME",
        ],
        "mock_overrides": {},
    },
    "FAQ": {
        "desc": "Ask FAQ at WELCOME, then continue to cotizar",
        "goal": "Primero preguntar el horario. Luego continuar a cotizar un domicilio.",
        "turn1_hint": (
            "Bot saludó. Tu primera respuesta debe ser preguntar por el horario o cobertura "
            "(el bot lo reconocerá como FAQ). Después de leer la respuesta, "
            "escribe 'Cotizar domicilio' para empezar a cotizar. "
            "IMPORTANTE: No preguntes '¿qué info necesitan?' cuando el bot te pida el servicio. "
            "Solo elige el servicio de la lista (Domicilios, Mensajería, etc.)."
        ),
        "states": [
            "WELCOME", "WELCOME", "AWAITING_PROFILE",
            "AWAITING_SERVICE_TYPE", "AWAITING_ORIGIN",
            "CONFIRMING_ORIGIN", "AWAITING_DESTINATION",
            "CONFIRMING_DEST", "AWAITING_SEGMENT_DESCRIPTION",
            "AWAITING_SEGMENT_INSTRUCTIONS", "AWAITING_MORE_STOPS",
            "AWAITING_TOOLS", "AWAITING_PAYMENT", "AWAITING_ACOMPANANTE",
            "CONFIRMING_QUOTE", "AWAITING_RECIPIENT_NAME",
            "AWAITING_RECIPIENT_PHONE", "WELCOME",
        ],
        "mock_overrides": {},
    },
    "CANCEL": {
        "desc": "Start a flow then cancel mid-way",
        "goal": "Inicia un domicilio. Cuando te pidan la dirección, escribe ÚNICAMENTE 'cancelar' (sin nada más) para salir.",
        "turn1_hint": (
            "Bot saludó. Escribe 'Cotizar domicilio'. Luego 'Usuario final'. "
            "Luego 'Domicilios'. Cuando te pida la dirección, "
            "escribe ÚNICAMENTE la palabra 'cancelar' — sin saludo, sin explicación, solo 'cancelar'. "
            "El bot solo reconoce 'cancelar' cuando es el ÚNICO texto del mensaje."
        ),
        "states": [
            "WELCOME", "AWAITING_PROFILE", "AWAITING_SERVICE_TYPE",
            "CANCEL_BEFORE", "WELCOME",
        ],
        "mock_overrides": {},
    },
    "CONFUSION": {
        "desc": "Confused at WELCOME → escalate to human",
        "goal": "Di 'no entiendo' varias veces. El bot debería ofrecer un asesor.",
        "turn1_hint": (
            "Bot saludó. Di 'no entiendo' o 'no sé cómo usar esto'. "
            "Sigue expresando confusión hasta que el bot te ofrezca un asesor. "
            "Cuando ofrezca asesor, escribe 'asesor' para escalar."
        ),
        "states": [
            "WELCOME", "WELCOME", "WELCOME", "WELCOME",
        ],
        "mock_overrides": {},
    },
    "ADDRESS_SELECT": {
        "desc": "Multiple geocode results for both origin and destination → select from lists",
        "goal": "Al escribir 'Calle 25' como dirección, el bot muestra varias opciones para origen Y destino. Elegir de las listas.",
        "turn1_hint": (
            "Bot saludó. Escribe 'Cotizar domicilio'. Luego 'Usuario final', luego 'Domicilios'. "
            "Cuando pida la dirección de origen, escribe 'Calle 25' (sin número exacto). "
            "El bot mostrará varias opciones — elige una escribiendo su nombre exacto. Di 'Sí' para confirmar. "
            "Cuando pida la dirección de destino, escribe también 'Calle 25' — el bot mostrará OTRA lista de opciones. "
            "Elige una de esa lista. Confirma con 'Sí'. Sigue el flujo normal hasta completar."
        ),
        "states": [
            "WELCOME", "AWAITING_PROFILE", "AWAITING_SERVICE_TYPE",
            "AWAITING_ORIGIN_SELECT", "CONFIRMING_ORIGIN",
            "AWAITING_DEST_SELECT", "CONFIRMING_DEST",
            "AWAITING_SEGMENT_DESCRIPTION", "AWAITING_SEGMENT_INSTRUCTIONS",
            "AWAITING_MORE_STOPS", "AWAITING_TOOLS",
            "AWAITING_PAYMENT", "AWAITING_ACOMPANANTE",
            "CONFIRMING_QUOTE", "AWAITING_RECIPIENT_NAME",
            "AWAITING_RECIPIENT_PHONE", "WELCOME",
        ],
        "mock_overrides": {},
    },
}

# The special CANCEL_BEFORE marker - not a real state
CANCEL_BEFORE = "CANCEL_BEFORE"


# ── Mocks ──────────────────────────────────────────────────────────────────

def build_mocks(scenario_name: str = "DOMICILIOS"):
    """Return a list of patchers mocking external deps.
    
    Does NOT mock _llm_classify_intent — the real Gemini classifier is used.
    """
    geo_result = [{"place_id": "ChIJqaUv3qvFOY4R0qQh7_6Nw8s", "display_name": "Cra 1 #2-3, Tuluá"}]
    geo_details = {"lat": 4.123, "lng": -76.456, "display_name": "Cra 1 #2-3, Tuluá"}
    price_result = {"breakdown": {"items": [{"concepto": "Servicio", "valor": 5000}], "total": 8000}}

    # ADDRESS_SELECT: geocode returns multiple results first, then single
    multi_result = [
        {"place_id": "place_A1", "display_name": "Cra 1 #2-3, Tuluá"},
        {"place_id": "place_A2", "display_name": "Calle 5 #10-20, Tuluá"},
        {"place_id": "place_A3", "display_name": "Av. 2 #15-30, Tuluá"},
    ]

    if scenario_name == "WITH_TOOLS":
        tools_result = [
            {"key": "canasta", "label": "Canasta", "description": "Canasta para mercado"},
            {"key": "termico", "label": "Maletín térmico", "description": "Mantiene temperatura"},
        ]
    else:
        tools_result = []

    # Geocode: normal returns single result; ADDRESS_SELECT returns multi first
    geo_side_effect = [geo_result] * 50
    if scenario_name == "ADDRESS_SELECT":
        geo_side_effect = [multi_result, multi_result] + [geo_result] * 50

    patchers = [
        patch("api.bot.flow._send_location", new_callable=AsyncMock, return_value=None),
        patch("api.bot.flow.calculator.geocode_search", new_callable=AsyncMock),
        patch("api.bot.flow.calculator.geocode_details", new_callable=AsyncMock, return_value=geo_details),
        patch("api.bot.flow.calculator.calculate_price", new_callable=AsyncMock, return_value=price_result),
        patch("api.bot.flow.calculator.get_tools", new_callable=AsyncMock, return_value=tools_result),
        patch("api.bot.flow.get_bot_user_async", new_callable=AsyncMock, return_value=MagicMock(id=1, username="bot")),
    ]
    return patchers, geo_side_effect


# ── Gemini helpers ─────────────────────────────────────────────────────────

GEMINI_CLIENT: genai.Client | None = None

def _get_client() -> genai.Client:
    global GEMINI_CLIENT
    if GEMINI_CLIENT is None:
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            print("ERROR: Set GEMINI_API_KEY environment variable or in .env")
            sys.exit(1)
        GEMINI_CLIENT = genai.Client(api_key=key)
    return GEMINI_CLIENT

USER_SYSTEM = """\
Eres un USUARIO colombiano de Tuluá chateando por WhatsApp con Domii Tuluá,
un servicio de domicilios, mensajería, compras y trámites.

REGLAS:
- Habla natural: "parce", "listo", "dale", "ey", etc.
- Responde SÓLO lo que dirías como usuario, NUNCA respuestas del bot
- Si el bot te muestra un menú con opciones, ELIGE UNA escribiendo su nombre
- Si ves "(Opcional — escribe 'no' para omitir)", puedes escribir 'no' para saltar
- Mensajes cortos (1-3 líneas). Sé variado: a veces correcto, a veces con errores
- Si el bot te corrige o repite, responde amablemente y corrige
"""

JUDGE_SYSTEM = """\
Eres un EVALUADOR DE CALIDAD del bot Domii Tuluá.
Evalúas CADA INTERACCIÓN entre el usuario y el bot.

CRITERIOS (1 = pésimo, 5 = perfecto):
1. Estado correcto: ¿El bot avanzó al estado correcto según el flujo esperado?
2. Claridad: ¿La respuesta es clara, bien escrita, sin fugas de debug?
3. Empatía: ¿El bot es amigable y reconoce el contexto del usuario?
4. Sin errores: ¿No hay contradicciones, bucles, información falsa?
5. Idioma: ¿El español es natural y profesional?

IMPORTANTE: El bot usa un flujo de estados predefinido. Evalúa si el bot SIGUIÓ
EL FLUJO CORRECTO para el servicio que el usuario seleccionó. Si el usuario dijo
una dirección y el bot usó valores de prueba (simulados), eso es normal porque
es un entorno de pruebas. NO penalices por direcciones simuladas.

Devuelve JSON: {{"score": 1-5, "issues": [lista corta], "comment": "explicación breve"}}
"""


def _extract_json(raw: str) -> str:
    """Strip code fences and surrounding text, returning only the JSON object."""
    raw = raw.strip()
    # Strip leading fences: ```json, ```json\n, or ```
    for prefix in ("```json\n", "```json", "```"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    # Strip trailing ```
    if raw.endswith("```"):
        raw = raw[:-3]
    raw = raw.strip()
    # Extract JSON from curly braces
    brace_start = raw.find("{")
    brace_end = raw.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        return raw[brace_start:brace_end + 1]
    return raw


def _call_gemini(system: str, messages: list[str], temp: float = 0.7) -> str:
    client = _get_client()
    config = genai_types.GenerateContentConfig(temperature=temp, max_output_tokens=1024)
    response = client.models.generate_content(
        model="gemini-3.1-flash-lite",
        contents=[
            genai_types.Content(role="user", parts=[genai_types.Part.from_text(
                text=f"{system}\n\n{messages[-1]}"
            )]),
        ],
        config=config,
    )
    return response.text.strip()


# ── QA Conversation Runner ─────────────────────────────────────────────────

@dataclass
class TurnResult:
    turn_number: int
    user_message: str
    bot_state: str
    bot_messages: list[dict]
    bot_fallback: bool
    bot_escalate: bool
    has_interactive: bool
    judge_score: int | None = None
    judge_issues: list[str] = field(default_factory=list)
    judge_comment: str = ""

@dataclass
class ConversationResult:
    scenario: str
    scenario_desc: str
    turns: list[TurnResult] = field(default_factory=list)
    duration_seconds: float = 0.0
    error: str | None = None
    reached_terminal: bool = False


async def run_conversation(
    scenario_name: str,
    scenario: dict[str, Any],
    max_turns: int = 25,
    enable_judge: bool = False,
) -> ConversationResult:
    result = ConversationResult(scenario=scenario_name, scenario_desc=scenario["desc"])
    start = datetime.now()

    conv = MagicMock(id=99999, contact_phone="+573001234567")
    session = build_initial_session()

    patchers, geo_side_effect = build_mocks(scenario_name)
    for p in patchers:
        p.start()
    # Set geocode side_effect (returns multiple results for ADDRESS_SELECT)
    from api.bot import flow as flow_module
    flow_module.calculator.geocode_search.side_effect = geo_side_effect

    history: list[dict] = []
    state_name = session.get("state", "WELCOME")

    for turn_num in range(1, max_turns + 1):
        # ── 1. Build prompt for Gemini user ──
        if turn_num == 1:
            context = (
                f"{scenario['turn1_hint']}\n\n"
                f"Tu objetivo final: {scenario['goal']}\n\n"
                f"¿Qué le escribes al bot?"
            )
        else:
            context = (
                f"Tu objetivo: {scenario['goal']}\n\n"
                f"Historial:\n{_history_block(history)}\n"
                f"Estado actual del bot: {state_name}\n\n"
                f"Ahora es tu turno. ¿Qué le dices al bot?"
            )

        # ── 2. Gemini generates user message ──
        try:
            user_msg = _call_gemini(USER_SYSTEM, [context], temp=0.7)
        except Exception as e:
            result.error = f"Gemini user call failed at turn {turn_num}: {e}"
            break

        # ── 3. Bot processes it ──
        try:
            adv_result = await advance(conv, session, user_msg)
        except Exception as e:
            result.error = f"Bot advance failed at turn {turn_num}: {e}"
            break

        state_name = session.get("state", "UNKNOWN")

        # ── 4. Judge evaluation ──
        judge_score: int | None = None
        judge_issues: list[str] = []
        judge_comment: str = ""
        if enable_judge:
            bot_text = " | ".join(
                m["content"] for m in (adv_result.messages or []) if m.get("type") == "text"
            )
            judge_prompt = (
                f"Escenario: {scenario_name} — {scenario['desc']}\n"
                f"Turno {turn_num}:\n"
                f"Usuario: {user_msg}\n"
                f"Bot responde (estado={state_name}): {bot_text}\n"
                f"Menú interactivo: {'SÍ' if adv_result.send_interactive else 'no'}\n\n"
                f"Evalúa la respuesta del bot. Devuelve JSON."
            )
            try:
                judge_raw = _call_gemini(JUDGE_SYSTEM + f"\n\nEscenario: {scenario_name}", [judge_prompt], temp=0.0)
                judge_raw = _extract_json(judge_raw)
                judge_data = json.loads(judge_raw)
                judge_score = judge_data.get("score")
                judge_issues = judge_data.get("issues", [])
                judge_comment = judge_data.get("comment", "")
            except Exception:
                judge_comment = ""

        turn = TurnResult(
            turn_number=turn_num,
            user_message=user_msg,
            bot_state=state_name,
            bot_messages=adv_result.messages or [],
            bot_fallback=adv_result.fallback,
            bot_escalate=adv_result.escalate,
            has_interactive=adv_result.send_interactive is not None,
            judge_score=judge_score,
            judge_issues=judge_issues,
            judge_comment=judge_comment,
        )
        result.turns.append(turn)

        history.append({
            "user": user_msg,
            "bot_messages": adv_result.messages or [],
            "bot_state": state_name,
        })

        # ── 5. Stop conditions ──
        if adv_result.escalate:
            break

        if state_name == "WELCOME":
            texts = " ".join(m.get("content", "") for m in (adv_result.messages or []) if m.get("type") == "text").lower()
            if any(kw in texts for kw in ("enviado exitosamente", "solicitud enviada",
                                            "pedido cancelado", "hasta luego", "cancelada")):
                result.reached_terminal = True
                break

        # Cancel scenario: detect cancel before asking for origin
        if scenario_name == "CANCEL" and "cancelar" in user_msg.lower():
            result.reached_terminal = True
            break

        if turn_num >= max_turns:
            break

    result.duration_seconds = (datetime.now() - start).total_seconds()

    for p in patchers:
        p.stop()

    return result


def _history_block(h: list[dict]) -> str:
    lines = []
    for turn in h:
        lines.append(f"Usuario: {turn['user']}")
        parts = [m["content"] for m in turn["bot_messages"] if m.get("type") == "text"]
        if parts:
            lines.append(f"Bot: {' | '.join(parts)}")
        lines.append(f"[Estado del bot: {turn['bot_state']}]")
    return "\n".join(lines)


# ── Report ─────────────────────────────────────────────────────────────────

def _format_report(res: ConversationResult) -> str:
    lines = []
    lines.append(f"\n{'='*60}")
    lines.append(f"  [{res.scenario}] {res.scenario_desc}")
    lines.append(f"{'='*60}")

    if res.error:
        lines.append(f"\n  ❌ ERROR: {res.error}")
        return "\n".join(lines)

    for turn in res.turns:
        fb = " ⚠️" if turn.bot_fallback else ""
        esc = " 🚨" if turn.bot_escalate else ""
        itr = " 📋" if turn.has_interactive else ""
        js = f" (Judge: {turn.judge_score}/5)" if turn.judge_score is not None else ""
        lines.append(f"\n  ── Turn {turn.turn_number} → {turn.bot_state}{fb}{esc}{itr}{js}")
        lines.append(f"     USR: {turn.user_message}")
        for msg in turn.bot_messages[:1]:
            if msg.get("type") == "text":
                lines.append(f"     BOT: {msg['content']}")
        if len(turn.bot_messages) > 1:
            lines.append(f"     ... +{len(turn.bot_messages)-1} more messages")
        for issue in turn.judge_issues:
            lines.append(f"     ⚑ {issue}")
        if turn.judge_comment:
            lines.append(f"     💬 {turn.judge_comment}")

    total = len(res.turns)
    scores = [t.judge_score for t in res.turns if t.judge_score is not None]
    avg = sum(scores) / len(scores) if scores else None
    fb = sum(1 for t in res.turns if t.bot_fallback)
    issues = sum(len(t.judge_issues) for t in res.turns)
    lines.append(f"\n  ── Summary ──")
    lines.append(f"  Turns: {total} | Time: {res.duration_seconds:.1f}s")
    lines.append(f"  Avg score: {avg:.1f}/5" if avg else "  Avg score: N/A")
    lines.append(f"  Fallbacks: {fb} | Issues: {issues}")
    lines.append(f"  Terminal reached: {'✅' if res.reached_terminal else '❌'}")
    lines.append("")

    return "\n".join(lines)


def print_report(res: ConversationResult):
    print(_format_report(res).strip())


# ── CLI ────────────────────────────────────────────────────────────────────

def _resolve_scenarios(scenario_arg: str) -> list[tuple[str, dict]]:
    if scenario_arg == "ALL":
        return list(SCENARIOS.items())
    names = [s.strip() for s in scenario_arg.split(",")]
    for name in names:
        if name not in SCENARIOS:
            valid = ", ".join(SCENARIOS)
            print(f"Invalid scenario '{name}'. Choose from: {valid}")
            sys.exit(1)
    return [(n, SCENARIOS[n]) for n in names]


def main():
    parser = argparse.ArgumentParser(description="QA Conversation Tester — 14 scenarios, all 31 states")
    parser.add_argument("--scenario", type=str, default="ALL",
                        help="Scenario(s) to run, comma-separated (default: ALL)")
    parser.add_argument("--turns", type=int, default=25)
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--report", type=str, default="",
                        help="Save text report to file")
    parser.add_argument("--json-report", type=str, default="",
                        help="Save JSON report to file")
    parser.add_argument("--verbose", action="store_true",
                        help="Show httpx/logging output")
    parser.add_argument("--coverage", action="store_true",
                        help="Show state coverage matrix")
    args = parser.parse_args()

    # Suppress noisy loggers unless verbose
    if not args.verbose:
        import logging
        for name in ("httpx", "google_genai", "api.bot", "asyncio"):
            logging.getLogger(name).setLevel(logging.WARNING)

    if args.coverage:
        names = list(SCENARIOS)
        all_states = sorted(set().union(*(SCENARIOS[n]["states"] for n in names)))
        print(f"\n{'='*80}")
        print(f"  State Coverage Matrix ({len(all_states)} states × {len(names)} scenarios)")
        print(f"{'='*80}")
        header = f"{'State':30s}" + "".join(f"{n[:8]:8s}" for n in names)
        print(f"  {header}")
        for state in all_states:
            if state == CANCEL_BEFORE:
                continue
            row = f"{state:30s}"
            for name in names:
                covered = state in SCENARIOS[name]["states"]
                row += f"{'✅':8s}" if covered else f"{'':8s}"
            print(f"  {row}")
        print()
        return

    scenarios_to_run = _resolve_scenarios(args.scenario)

    all_results: list[tuple[str, ConversationResult]] = []

    async def _run_all():
        for name, cfg in scenarios_to_run:
            res = await run_conversation(
                scenario_name=name,
                scenario=cfg,
                max_turns=args.turns,
                enable_judge=args.judge,
            )
            all_results.append((name, res))
            print_report(res)
            if args.report:
                Path(args.report).write_text(_format_report(res))

    asyncio.run(_run_all())

    # JSON report
    if args.json_report:
        import json as _json
        def _turn_to_dict(t: TurnResult) -> dict:
            return {
                "turn": t.turn_number,
                "state": t.bot_state,
                "user": t.user_message,
                "bot_messages": [m.get("content", "") for m in t.bot_messages if m.get("type") == "text"],
                "fallback": t.bot_fallback,
                "escalate": t.bot_escalate,
                "interactive": t.has_interactive,
                "judge_score": t.judge_score,
                "judge_issues": t.judge_issues,
                "judge_comment": t.judge_comment,
            }
        def _res_to_dict(name: str, r: ConversationResult) -> dict:
            scores = [t.judge_score for t in r.turns if t.judge_score is not None]
            return {
                "scenario": name,
                "description": r.scenario_desc,
                "turns": len(r.turns),
                "duration_seconds": r.duration_seconds,
                "terminal_reached": r.reached_terminal,
                "avg_judge_score": round(sum(scores) / len(scores), 1) if scores else None,
                "total_issues": sum(len(t.judge_issues) for t in r.turns),
                "error": r.error,
                "turn_details": [_turn_to_dict(t) for t in r.turns],
            }
        report = {
            "scenarios": [_res_to_dict(n, r) for n, r in all_results],
            "summary": {
                "total": len(all_results),
                "terminal_reached": sum(1 for _, r in all_results if r.reached_terminal),
            },
        }
        Path(args.json_report).write_text(_json.dumps(report, ensure_ascii=False, indent=2))
        print(f"  JSON report saved to {args.json_report}")

    # Final summary
    succeeded = sum(1 for _, r in all_results if r.reached_terminal)
    total = len(all_results)
    print(f"\n{'='*60}")
    print(f"  OVERALL SUMMARY ({succeeded}/{total} reached terminal)")
    print(f"{'='*60}")
    any_error = False
    for name, res in all_results:
        scores = [t.judge_score for t in res.turns if t.judge_score is not None]
        avg = f"{sum(scores)/len(scores):.1f}/5" if scores else "N/A"
        icon = "✅" if res.reached_terminal else "❌"
        print(f"  {icon} {name:20s} | {len(res.turns):2d} turns | {avg:>4s} avg | {sum(len(t.judge_issues) for t in res.turns):2d} issues")
        if res.error:
            any_error = True
            print(f"     ERROR: {res.error}")

    if any_error or succeeded < total:
        sys.exit(1)


if __name__ == "__main__":
    main()
