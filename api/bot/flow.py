"""State machine for the Domii Tuluá WhatsApp bot.

Each conversation has an explicit ``state`` stored in its Redis session.
The dispatcher calls ``advance()`` with the user's input, which runs
the current state's handler, mutates the session, and returns
a ``FlowResult`` describing what to send back to the user.

Usage::

    result = await advance(conversation, session, user_text, button_id)
    if result.escalate:
        # release take, create note, set cooldown
    for msg in result.messages:
        # send each message
    session = result.session  # already mutated
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from asgiref.sync import sync_to_async

from django.utils import timezone

from . import calculator
from .utils import get_bot_user_async
from .router import _build_hours_response
from .config import get_escalate_orders_enabled
from api.models import Conversation, Message
from api.serializers import MessageSerializer
from api.views import publish_conversation_update, _enqueue_outbound_send

logger = logging.getLogger("api.bot.flow")

# ---------------------------------------------------------------------------
#  State names
# ---------------------------------------------------------------------------
WELCOME = "WELCOME"

# Main quote flow (coords-dependent service types)
AWAITING_PROFILE = "AWAITING_PROFILE"
AWAITING_SERVICE_TYPE = "AWAITING_SERVICE_TYPE"

# Mensajeria extras
ASK_PACKAGE_TYPE = "ASK_PACKAGE_TYPE"
ASK_WHO_PAYS = "ASK_WHO_PAYS"

# Geocoding states (domicilios / mensajeria / tramites)
AWAITING_ORIGIN = "AWAITING_ORIGIN"
AWAITING_ORIGIN_SELECT = "AWAITING_ORIGIN_SELECT"
CONFIRMING_ORIGIN = "CONFIRMING_ORIGIN"
AWAITING_DESTINATION = "AWAITING_DESTINATION"
AWAITING_DEST_SELECT = "AWAITING_DEST_SELECT"
CONFIRMING_DEST = "CONFIRMING_DEST"

# Description / instructions per segment
AWAITING_SEGMENT_DESCRIPTION = "AWAITING_SEGMENT_DESCRIPTION"
AWAITING_SEGMENT_INSTRUCTIONS = "AWAITING_SEGMENT_INSTRUCTIONS"
AWAITING_MORE_STOPS = "AWAITING_MORE_STOPS"

# Purchases / bancarios (no coords)
AWAITING_DESCRIPTION = "AWAITING_DESCRIPTION"
ASK_BANCARIOS_ENTITY = "ASK_BANCARIOS_ENTITY"
ASK_BANCARIOS_REFERENCE = "ASK_BANCARIOS_REFERENCE"

# Quote summary states
AWAITING_TOOLS = "AWAITING_TOOLS"
AWAITING_PAYMENT = "AWAITING_PAYMENT"
AWAITING_ACOMPANANTE = "AWAITING_ACOMPANANTE"
SHOW_PRICE = "SHOW_PRICE"
CONFIRMING_QUOTE = "CONFIRMING_QUOTE"

# Recipient + submit
ASK_KNOWS_RECIPIENT = "ASK_KNOWS_RECIPIENT"
AWAITING_RECIPIENT_NAME = "AWAITING_RECIPIENT_NAME"
AWAITING_RECIPIENT_PHONE = "AWAITING_RECIPIENT_PHONE"
AWAITING_SENDER_NAME = "AWAITING_SENDER_NAME"
SUBMIT_ORDER = "SUBMIT_ORDER"

# Domii Fijo sub-flow
AWAITING_FIJO_NAME = "AWAITING_FIJO_NAME"
AWAITING_FIJO_ADDR = "AWAITING_FIJO_ADDR"
AWAITING_FIJO_PHONE = "AWAITING_FIJO_PHONE"
AWAITING_FIJO_DATE = "AWAITING_FIJO_DATE"
AWAITING_FIJO_START = "AWAITING_FIJO_START"
AWAITING_FIJO_END = "AWAITING_FIJO_END"
AWAITING_FIJO_VOLUME = "AWAITING_FIJO_VOLUME"
CONFIRMING_FIJO = "CONFIRMING_FIJO"
SUBMIT_FIJO = "SUBMIT_FIJO"

# Global terminal
ESCALATE = "ESCALATE"

# --- Service types that need geocoding ---
_GEO_SERVICES = frozenset({"domicilios", "mensajeria", "tramites"})
_COORD_OPTIONAL = frozenset({"purchases", "bancarios"})
_SKIP_WORDS = frozenset({"no", "ninguna", "ninguno", "omitir", "skip", "ningun"})
_GEOCODE_FAIL_LIMIT = 2


def _inc_geocode_fail(session: dict) -> None:
    _d(session)["geocode_fail_count"] = _d(session).get("geocode_fail_count", 0) + 1


def _reset_geocode_fail(session: dict) -> None:
    _d(session)["geocode_fail_count"] = 0


async def _check_geocode_fail(session: dict) -> FlowResult | None:
    """Auto-escalate if geocode failures exceed limit."""
    if _d(session).get("geocode_fail_count", 0) >= _GEOCODE_FAIL_LIMIT:
        _reset_geocode_fail(session)
        reason = await _escalation_note(session, "El cliente no pudo indicar su direcci\u00f3n tras varios intentos")
        return FlowResult(
            escalate=True,
            escalate_reason=reason,
            messages=[_text_msg(
                "He tenido dificultades para encontrar tu direcci\u00f3n. "
                "Un asesor te atender\u00e1 pronto y completar\u00e1 el pedido contigo."
            )],
        )
    return None


# ---------------------------------------------------------------------------
#  Result type
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
#  Result type
# ---------------------------------------------------------------------------

@dataclass
class FlowResult:
    """Returned by ``advance()`` describing what the dispatcher should do."""

    messages: list[dict] = field(default_factory=list)
    """Messages to send to the user. Each is ``{type, content, ...}``."""

    send_interactive: Optional[dict] = None
    """Interactive payload (button or list) to send. Overrides ``send_text`` when set."""

    state: str | None = None
    """New state after this turn. ``None`` means terminal (deleted session)."""

    escalate: bool = False
    """Whether to release the conversation to a human agent."""

    escalate_reason: str = ""
    """Reason to write in the escalation note."""

    fallback: bool = False
    """Whether this input was unparseable (increment fallback counter)."""


def _text_msg(text: str) -> dict:
    return {"type": "text", "content": text}


def _interactive(type_: str, body: str, **kwargs) -> dict:
    payload = {"type": type_, "body": {"text": body}}
    footer = kwargs.get("footer")
    if footer:
        payload["footer"] = {"text": footer}
    header_text = kwargs.get("header")
    if header_text:
        payload["header"] = {"type": "text", "text": header_text}

    if type_ == "button":
        payload["action"] = {
            "buttons": [
                {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
                for b in kwargs.get("buttons", [])
            ],
        }
    elif type_ == "list":
        payload["action"] = {
            "button": (kwargs.get("button_label") or "Opciones")[:20],
            "sections": _sections_to_list(kwargs.get("sections", [])),
        }
    return payload


def _sections_to_list(raw: list[dict]) -> list[dict]:
    out = []
    for sec in raw:
        out.append({
            "title": (sec.get("title", "") or "")[:24],
            "rows": [
                {
                    "id": row["id"],
                    "title": (row.get("title", "") or "")[:24],
                    "description": (row.get("description", "") or "")[:72],
                }
                for row in sec.get("rows", [])[:10]
            ],
        })
    return out


def _yes_no_buttons() -> list[dict]:
    return [
        {"id": "yes", "title": "✅ Sí"},
        {"id": "no", "title": "❌ No"},
    ]


# ---------------------------------------------------------------------------
#  Session helpers
# ---------------------------------------------------------------------------

def _d(session: dict) -> dict:
    """Shortcut to ``session['data']['collected']``."""
    return session.setdefault("data", {}).setdefault("collected", {})


def _seg(session: dict, idx: int | None = None) -> dict:
    data = _d(session)
    if idx is None:
        idx = data.get("current_segment", 0)
    segs = data.setdefault("segments", [])
    while len(segs) <= idx:
        segs.append({
            "origin": {"address": None, "lat": None, "lng": None, "confirmed": False},
            "destination": {"address": None, "lat": None, "lng": None, "confirmed": False},
            "description": None,
            "instructions": None,
        })
    return segs[idx]


def _cur_seg(session: dict) -> dict:
    return _seg(session, _d(session).get("current_segment", 0))


# ---------------------------------------------------------------------------
#  Location message sender
# ---------------------------------------------------------------------------

async def _send_location(conversation, lat: float, lng: float, display_name: str):
    """Send a WhatsApp location message and publish SSE."""
    payload = {
        "longitude": float(lng),
        "latitude": float(lat),
        "name": (display_name or "Ubicación")[:100],
        "address": display_name,
    }
    bot = await get_bot_user_async()
    msg = await sync_to_async(Message.objects.create)(
        conversation=conversation,
        direction="outbound",
        message_type="location",
        content=f"{payload['name']} ({lat}, {lng})",
        sender_name="Bot",
        sender=bot,
        metadata={"location": payload},
    )
    await sync_to_async(lambda: Conversation.objects.filter(
        id=conversation.id,
    ).update(
        last_message=f"📍 {payload['name']}"[:255],
        last_message_at=timezone.now(),
    ))()
    conversation.last_message = f"📍 {payload['name']}"[:255]
    conversation.last_message_at = timezone.now()
    conversation._last_msg_direction = 'outbound'
    await sync_to_async(_enqueue_outbound_send)(msg, schedule_delay=0)
    msg_data = await sync_to_async(lambda: MessageSerializer(msg).data)()
    await sync_to_async(publish_conversation_update)(conversation, msg_data)


# ---------------------------------------------------------------------------
#  Geocode helpers
# ---------------------------------------------------------------------------

async def _geocode_and_store(session: dict, conversation, text: str, field: str) -> FlowResult:
    """Run geocode_search → resolve → send location → store in segment ``field`` (origin/dest)."""
    result = await calculator.geocode_search(text)
    if isinstance(result, dict) and "error" in result:
        return FlowResult(messages=[_text_msg("Error al buscar la dirección. Intenta de nuevo.")])
    if not isinstance(result, list):
        return FlowResult(messages=[_text_msg("Error inesperado. Intenta de nuevo.")])

    if len(result) == 0:
        _inc_geocode_fail(session)
        chk = await _check_geocode_fail(session)
        if chk:
            return chk
        return FlowResult(messages=[
            _text_msg("No encontré esa dirección. Intenta con un formato como 'Cra 1 #2-3, Tuluá'."),
        ])
    elif len(result) == 1:
        return await _store_single_geocode(session, conversation, result[0], field)
    else:
        # Multiple results — store for select state
        select_state = AWAITING_ORIGIN_SELECT if field == "origin" else AWAITING_DEST_SELECT
        _d(session)["_pending_geocode_results"] = [
            {"display_name": r.get("display_name", ""), "place_id": r.get("place_id", "")}
            for r in result[:5]
        ]
        _d(session)["_pending_geocode_field"] = field
        rows = [
            {"id": r["place_id"], "title": r["display_name"][:24], "description": r["display_name"][:72]}
            for r in _d(session)["_pending_geocode_results"]
        ]
        rows.append({"id": "none_match", "title": "Ninguna es correcta", "description": "Escribir otra dirección"})
        return FlowResult(
            state=select_state,
            messages=[_text_msg("Encontré varias direcciones. ¿Cuál es la correcta?")],
            send_interactive=_interactive("list",
                "Selecciona tu dirección",
                button_label="Direcciones",
                sections=[{"title": "Resultados", "rows": rows}],
            ),
        )


async def _store_single_geocode(session: dict, conversation, entry: dict, field: str) -> FlowResult:
    """Store geocode result in segment and send location."""
    place_id = entry.get("place_id") if isinstance(entry, dict) else None
    if place_id:
        details = await calculator.geocode_details(place_id)
    else:
        details = entry if isinstance(entry, dict) else {}

    if isinstance(details, dict) and "error" in details:
        return FlowResult(messages=[_text_msg("Error al obtener coordenadas. Intenta de nuevo.")])

    lat = details.get("lat") if isinstance(details, dict) else None
    lng = details.get("lng") if isinstance(details, dict) else None
    display_name = (details.get("display_name") or details.get("name") or entry.get("display_name", ""))[:300]
    if lat is None or lng is None:
        return FlowResult(messages=[_text_msg("No pude obtener la ubicación exacta. Intenta de nuevo.")])

    seg = _cur_seg(session)
    seg[field] = {
        "address": display_name,
        "lat": float(lat),
        "lng": float(lng),
        "confirmed": False,
    }
    geocoded = _d(session).setdefault("geocoded_addresses", [])
    geocoded.append(display_name)

    confirming_state = CONFIRMING_ORIGIN if field == "origin" else CONFIRMING_DEST
    label = "origen" if field == "origin" else "destino"

    await _send_location(conversation, float(lat), float(lng), display_name)

    return FlowResult(
        state=confirming_state,
        messages=[_text_msg(
            f"Tu dirección de {label} es:\n{display_name}\n\n¿Es correcta?"
        )],
        send_interactive=_interactive("button", "¿Es correcta?", buttons=_yes_no_buttons()),
    )


async def _handle_geocode_select(session: dict, conversation, button_id: str, field: str) -> FlowResult:
    """Handle user selection from geocode results list."""
    if button_id == "none_match":
        _inc_geocode_fail(session)
        chk = await _check_geocode_fail(session)
        if chk:
            return chk
        return_to = AWAITING_ORIGIN if field == "origin" else AWAITING_DESTINATION
        return FlowResult(
            state=return_to,
            messages=[_text_msg("Claro. Escribe la dirección de nuevo.")],
        )
    results = _d(session).get("_pending_geocode_results", [])
    entry = None
    for r in results:
        if r.get("place_id") == button_id:
            entry = r
            break
    if not entry:
        return FlowResult(
            messages=[_text_msg("Opción no válida. Intenta de nuevo.")],
        )
    return await _store_single_geocode(session, conversation, entry, field)


# ---------------------------------------------------------------------------
#  State handlers
# ---------------------------------------------------------------------------

_STATES: dict[str, Callable] = {}


def _handler(name: str):
    def decorator(func):
        _STATES[name] = func
        return func
    return decorator


# ── WELCOME ────────────────────────────────────────────────────────────────

_GREET_ALT = (
    # single-word
    r"hola+s*|holi(?:s|wi)?|ol[aa]+"
    r"|buen[ao]s?|buenas?"
    r"|hey+|e+y+|h[ea]llo|hi\b"
    r"|al[\u00f3o]+|halo"
    r"|tal|bien"
    # multi-word
    r"|(?:muy\s+)?buen(?:[oa]s?)?\s+d[\u00edi]as?"  # buenos días / buen día
    r"|(?:muy\s+)?buenas?\s+tardes?"              # buenas tardes / buena tarde
    r"|(?:muy\s+)?buenas?\s+noches?"              # buenas noches / buena noche
    r"|qu[\u00e9e]\s+tal"
    r"|qu[\u00e9e]\s+hubo"
    r"|qu[\u00e9e]\s+m[\u00e1a]s"
    r"|c[\u00f3o]mo\s+(?:est[\u00e1a]s?\b|va\b|v[\u00e1a]s?\b)"  # cómo estás / como vas
)
_GREETING_RE = re.compile(
    r"^(?:" + _GREET_ALT + r")"
    r"(?:\s+(?:" + _GREET_ALT + r"))*"
    r"(?:[\s,.]*(?:todo\s+bien|bien|gracias))*"
    r"[\s.!]*$",
    re.IGNORECASE,
)

@_handler(WELCOME)
async def handle_welcome(session: dict, text: str, button_id: str | None,
                         conversation) -> FlowResult:
    if button_id:
        if button_id == "cotizar":
            _d(session)["profile"] = None
            _d(session)["service_type"] = None
            return FlowResult(state=AWAITING_PROFILE, messages=[
                _text_msg("¿Eres un cliente final o un negocio?"),
            ], send_interactive=_interactive("button", "Elige tu perfil", buttons=[
                {"id": "final", "title": "Usuario final"},
                {"id": "negocio", "title": "Negocio"},
            ]))
        if button_id == "domii_fijo":
            session.setdefault("domii_fijo_data", {})
            session.pop("data", None)
            return FlowResult(state=AWAITING_FIJO_NAME, messages=[
                _text_msg("¿Cuál es el nombre de tu negocio?"),
            ])
        if button_id == "pedido":
            return FlowResult(escalate=True, escalate_reason="Cliente preguntó sobre su pedido desde el menú principal",
                              messages=[_text_msg("Un asesor te atenderá pronto.")])
        if button_id == "escalate":
            return FlowResult(escalate=True, escalate_reason="Cliente solicitó asesor desde el menú principal",
                              messages=[_text_msg("Un asesor te atenderá pronto.")])
        if button_id == "faq":
            hours_text = await sync_to_async(_build_hours_response)()
            return FlowResult(messages=[
                _text_msg(
                    f"Preguntas frecuentes:\n\n"
                    f"{hours_text}\n"
                    f"• *Cobertura:* Tuluá urbano y veredas. Fuera (Cali, Buga) = tarifas fijas.\n"
                    f"• *Pago:* Efectivo (sin recargo) o Nequi (+$500).\n"
                    f"• *Servicios:* Domicilios, mensajería, compras, trámites, bancarios.\n\n"
                    f"¿Necesitas algo más? elige una opción del menú."
                ),
            ], send_interactive=_welcome_interactive())

    if _GREETING_RE.match(text.strip()):
        return FlowResult(state=WELCOME, messages=[], send_interactive=_welcome_interactive())

    intent = await _llm_classify_intent(text, WELCOME)
    if intent == "cotizar":
        return await handle_welcome(session, text, "cotizar", conversation)
    if intent == "domii_fijo":
        return await handle_welcome(session, text, "domii_fijo", conversation)
    if intent == "pedido":
        return await handle_welcome(session, text, "pedido", conversation)
    if intent == "escalate":
        return await handle_welcome(session, text, "escalate", conversation)
    if intent == "faq":
        return await handle_welcome(session, text, "faq", conversation)

    return FlowResult(fallback=True, messages=[
        _text_msg("Gracias por tu mensaje. Para ayudarte mejor, dime exactamente qu\u00e9 "
                  "necesitas o elige una opci\u00f3n del men\u00fa."),
    ], send_interactive=_welcome_interactive())


def _welcome_interactive() -> dict:
    return _interactive("list",
        "¡Bienvenido a Domii Tuluá! 🚀\n¿Qué deseas hacer?",
        button_label="Menú",
        sections=[{"title": "Opciones", "rows": [
            {"id": "cotizar", "title": "Cotizar domicilio", "description": "Calcula el precio de un envío"},
            {"id": "domii_fijo", "title": "Domii Fijo", "description": "Domiciliario dedicado por horas/días"},
            {"id": "faq", "title": "Preguntas frecuentes", "description": "Horarios, cobertura, pagos"},
            {"id": "pedido", "title": "Preguntar sobre pedido", "description": "Consulta el estado de tu pedido"},
            {"id": "escalate", "title": "Hablar con un asesor", "description": "Atención personalizada"},
        ]}],
    )


# ── AWAITING_PROFILE ───────────────────────────────────────────────────────

@_handler(AWAITING_PROFILE)
async def handle_profile(session: dict, text: str, button_id: str | None,
                         conversation) -> FlowResult:
    value = button_id if button_id else await _llm_classify_intent(text, AWAITING_PROFILE)
    if value == "final":
        _d(session)["profile"] = "usuario_final"
        return FlowResult(state=AWAITING_SERVICE_TYPE, messages=[
            _text_msg("¿Qué servicio necesitas?"),
        ], send_interactive=_service_type_list())
    if value == "negocio":
        _d(session)["profile"] = "negocio"
        return FlowResult(state=AWAITING_SERVICE_TYPE, messages=[
            _text_msg("¿Qué servicio necesita tu negocio?"),
        ], send_interactive=_service_type_list())
    return FlowResult(fallback=True, messages=[
        _text_msg("Elige 'Usuario final' o 'Negocio'."),
    ], send_interactive=_interactive("button", "Elige tu perfil", buttons=[
        {"id": "final", "title": "Usuario final"},
        {"id": "negocio", "title": "Negocio"},
    ]))


# ── AWAITING_SERVICE_TYPE ──────────────────────────────────────────────────

_SERVICE_LABELS: dict[str, str] = {
    "domicilios": "Domicilios",
    "mensajeria": "Mensajería",
    "purchases": "Compras por encargo",
    "tramites": "Trámites",
    "bancarios": "Bancarios",
}

_ORDERED_SERVICES = ["domicilios", "mensajeria", "purchases", "tramites", "bancarios"]

def _build_escalation_context(session: dict, reason: str) -> str:
    """Build a contextual escalation reason from session data."""
    data = _d(session)
    parts = []
    profile = data.get("profile")
    if profile:
        parts.append(profile)
    service = data.get("service_type")
    if service:
        label = _SERVICE_LABELS.get(service, service)
        parts.append(label)
    ctx = " · ".join(parts) if parts else "sin datos"
    state = session.get("state", "?")
    return f"{reason} — {ctx} (estado {state})"


async def _escalation_note(session: dict, reason_type: str) -> str:
    """LLM-generated escalation note, falling back to context."""
    data = _d(session)
    profile = data.get("profile")
    service = data.get("service_type")
    if service:
        service = _SERVICE_LABELS.get(service, service)
    note = await _llm_fallback.generate_escalation_summary(
        state=session.get("state", "?"),
        profile=profile,
        service=service,
        reason_type=reason_type,
    )
    if note:
        return note
    return _build_escalation_context(session, reason_type)


def _service_type_list() -> dict:
    return _interactive("list",
        "Selecciona el tipo de servicio",
        button_label="Servicios",
        sections=[{"title": "Servicios", "rows": [
            {"id": k, "title": _SERVICE_LABELS.get(k, k)[:24], "description": ""}
            for k in _ORDERED_SERVICES
        ]}],
    )


@_handler(AWAITING_SERVICE_TYPE)
async def handle_service_type(session: dict, text: str, button_id: str | None,
                              conversation) -> FlowResult:
    value = button_id if button_id else await _llm_classify_intent(text, AWAITING_SERVICE_TYPE)
    if value not in _SERVICE_LABELS:
        return FlowResult(fallback=True, messages=[
            _text_msg("Selecciona un tipo de servicio de la lista."),
        ], send_interactive=_service_type_list())

    _d(session)["service_type"] = value
    _d(session)["current_segment"] = 0
    _d(session)["segments"] = []
    _cur_seg(session)["service_type"] = value

    # Extra states for mensajeria
    if value == "mensajeria":
        return FlowResult(state=ASK_PACKAGE_TYPE, messages=[
            _text_msg("¿Qué tipo de paquete envías?"),
        ], send_interactive=_interactive("button",
            "Tipo de paquete",
            buttons=[
                {"id": "documento", "title": "Documento"},
                {"id": "paquete", "title": "Paquete"},
                {"id": "fragil", "title": "Frágil"},
                {"id": "alimento", "title": "Alimento"},
                {"id": "otro", "title": "Otro"},
            ],
        ))

    coords_optional = value in _COORD_OPTIONAL
    _d(session)["coords_optional"] = coords_optional

    origin_prompt = (
        "¿Dirección de origen? (Opcional — escribe 'no' para omitir)"
        if coords_optional
        else "¿Cuál es la dirección de origen? (Escríbela o comparte tu ubicación)"
    )
    return FlowResult(state=AWAITING_ORIGIN, messages=[
        _text_msg(origin_prompt),
    ])


# ── Mensajeria extras ──────────────────────────────────────────────────────

@_handler(ASK_PACKAGE_TYPE)
async def handle_package_type(session: dict, text: str, button_id: str | None,
                              conversation) -> FlowResult:
    value = button_id if button_id else await _llm_classify_intent(text, ASK_PACKAGE_TYPE)
    _d(session)["package_type"] = value
    return FlowResult(state=ASK_WHO_PAYS, messages=[
        _text_msg("¿Quién paga el envío?"),
    ], send_interactive=_interactive("button",
        "¿Quién paga?",
        buttons=[
            {"id": "remitente", "title": "Remitente (yo)"},
            {"id": "destinatario", "title": "Destinatario"},
        ],
    ))


@_handler(ASK_WHO_PAYS)
async def handle_who_pays(session: dict, text: str, button_id: str | None,
                          conversation) -> FlowResult:
    value = button_id if button_id else await _llm_classify_intent(text, ASK_WHO_PAYS)
    _d(session)["who_pays"] = value
    return FlowResult(state=AWAITING_ORIGIN, messages=[
        _text_msg("¿Cuál es la dirección de origen? (Escríbela o comparte tu ubicación)"),
    ])


# ── AWAITING_ORIGIN ────────────────────────────────────────────────────────

@_handler(AWAITING_ORIGIN)
async def handle_origin(session: dict, text: str, button_id: str | None,
                        conversation) -> FlowResult:
    if button_id:
        return FlowResult(messages=[_text_msg("Escribe la dirección o comparte tu ubicación.")])
    if not text.strip():
        return FlowResult(messages=[_text_msg("Escribe una dirección.")])
    if _d(session).get("coords_optional") and text.strip().lower() in _SKIP_WORDS:
        _cur_seg(session)["origin"] = {"address": None, "lat": None, "lng": None, "confirmed": True}
        dest_prompt = "¿Dirección de destino? (Opcional — escribe 'no' para omitir)"
        return FlowResult(state=AWAITING_DESTINATION, messages=[
            _text_msg(dest_prompt),
        ])
    return await _geocode_and_store(session, conversation, text.strip(), "origin")


# ── AWAITING_ORIGIN_SELECT ─────────────────────────────────────────────────

@_handler(AWAITING_ORIGIN_SELECT)
async def handle_origin_select(session: dict, text: str, button_id: str | None,
                               conversation) -> FlowResult:
    if not button_id:
        return FlowResult(messages=[_text_msg("Selecciona una dirección de la lista.")])
    return await _handle_geocode_select(session, conversation, button_id, "origin")


# ── CONFIRMING_ORIGIN ──────────────────────────────────────────────────────

@_handler(CONFIRMING_ORIGIN)
async def handle_confirm_origin(session: dict, text: str, button_id: str | None,
                                conversation) -> FlowResult:
    value = button_id if button_id else await _llm_classify_intent(text, CONFIRMING_ORIGIN)
    if value == "yes":
        _reset_geocode_fail(session)
        _cur_seg(session)["origin"]["confirmed"] = True
        dest_prompt = (
            "¿Dirección de destino? (Opcional — escribe 'no' para omitir)"
            if _d(session).get("coords_optional")
            else "¿Cuál es la dirección de destino?"
        )
        return FlowResult(state=AWAITING_DESTINATION, messages=[
            _text_msg(dest_prompt),
        ])
    if value == "no":
        _inc_geocode_fail(session)
        chk = await _check_geocode_fail(session)
        if chk:
            return chk
        _cur_seg(session)["origin"] = {"address": None, "lat": None, "lng": None, "confirmed": False}
        origin_prompt = (
            "¿Dirección de origen? (Opcional — escribe 'no' para omitir)"
            if _d(session).get("coords_optional")
            else "Claro. ¿Cuál es la dirección correcta?"
        )
        return FlowResult(state=AWAITING_ORIGIN, messages=[
            _text_msg(origin_prompt),
        ])
    return FlowResult(fallback=True, messages=[
        _text_msg("Responde si la dirección es correcta o no."),
    ], send_interactive=_interactive("button", "¿Es correcta?", buttons=_yes_no_buttons()))


# ── AWAITING_DESTINATION ───────────────────────────────────────────────────

@_handler(AWAITING_DESTINATION)
async def handle_destination(session: dict, text: str, button_id: str | None,
                             conversation) -> FlowResult:
    if button_id:
        return FlowResult(messages=[_text_msg("Escribe la dirección o comparte tu ubicación.")])
    if not text.strip():
        return FlowResult(messages=[_text_msg("Escribe una dirección.")])
    if _d(session).get("coords_optional") and text.strip().lower() in _SKIP_WORDS:
        _cur_seg(session)["destination"] = {"address": None, "lat": None, "lng": None, "confirmed": True}
        st = _d(session).get("service_type", "")
        if st == "purchases":
            desc_prompt = "¿Qué necesitas que compren?"
        elif st == "bancarios":
            desc_prompt = "¿Qué trámite bancario necesitas?"
        else:
            desc_prompt = "¿Qué estás enviando? (Describe el paquete o producto)"
        return FlowResult(state=AWAITING_SEGMENT_DESCRIPTION, messages=[
            _text_msg(desc_prompt),
        ])
    return await _geocode_and_store(session, conversation, text.strip(), "destination")


# ── AWAITING_DEST_SELECT ───────────────────────────────────────────────────

@_handler(AWAITING_DEST_SELECT)
async def handle_dest_select(session: dict, text: str, button_id: str | None,
                             conversation) -> FlowResult:
    if not button_id:
        return FlowResult(messages=[_text_msg("Selecciona una dirección de la lista.")])
    return await _handle_geocode_select(session, conversation, button_id, "destination")


# ── CONFIRMING_DEST ────────────────────────────────────────────────────────

@_handler(CONFIRMING_DEST)
async def handle_confirm_dest(session: dict, text: str, button_id: str | None,
                              conversation) -> FlowResult:
    value = button_id if button_id else await _llm_classify_intent(text, CONFIRMING_DEST)
    if value == "yes":
        _reset_geocode_fail(session)
        _cur_seg(session)["destination"]["confirmed"] = True
        st = _d(session).get("service_type", "")
        if st == "purchases":
            desc_prompt = "¿Qué necesitas que compren?"
        elif st == "bancarios":
            desc_prompt = "¿Qué trámite bancario necesitas?"
        else:
            desc_prompt = "¿Qué estás enviando? (Describe el paquete o producto)"
        return FlowResult(state=AWAITING_SEGMENT_DESCRIPTION, messages=[
            _text_msg(desc_prompt),
        ])
    if value == "no":
        _inc_geocode_fail(session)
        chk = await _check_geocode_fail(session)
        if chk:
            return chk
        _cur_seg(session)["destination"] = {"address": None, "lat": None, "lng": None, "confirmed": False}
        dest_prompt = (
            "¿Dirección de destino? (Opcional — escribe 'no' para omitir)"
            if _d(session).get("coords_optional")
            else "Claro. ¿Cuál es la dirección correcta?"
        )
        return FlowResult(state=AWAITING_DESTINATION, messages=[
            _text_msg(dest_prompt),
        ])
    return FlowResult(fallback=True, messages=[
        _text_msg("Responde si la dirección es correcta o no."),
    ], send_interactive=_interactive("button", "¿Es correcta?", buttons=_yes_no_buttons()))


# ── AWAITING_SEGMENT_DESCRIPTION ──────────────────────────────────────────

@_handler(AWAITING_SEGMENT_DESCRIPTION)
async def handle_seg_description(session: dict, text: str, button_id: str | None,
                                 conversation) -> FlowResult:
    if not text.strip():
        st = _d(session).get("service_type", "")
        if st == "purchases":
            err = "Describe lo que necesitas que compren."
        elif st == "bancarios":
            err = "Describe qué trámite bancario necesitas."
        else:
            err = "Describe lo que envías."
        return FlowResult(messages=[_text_msg(err)])
    _cur_seg(session)["description"] = text.strip()[:500]
    return FlowResult(state=AWAITING_SEGMENT_INSTRUCTIONS, messages=[
        _text_msg("¿Alguna instrucción especial? (Ej: 'Tocar timbre', 'Llamar al llegar')\n\nEscribe 'no' si no hay instrucciones."),
    ])


# ── AWAITING_SEGMENT_INSTRUCTIONS ─────────────────────────────────────────

@_handler(AWAITING_SEGMENT_INSTRUCTIONS)
async def handle_seg_instructions(session: dict, text: str, button_id: str | None,
                                  conversation) -> FlowResult:
    text_clean = text.strip() if text else ""
    if text_clean.lower() in ("no", "ninguna", ""):
        _cur_seg(session)["instructions"] = None
    else:
        _cur_seg(session)["instructions"] = text_clean[:500]
    return await _prompt_more_stops_or_tools(session)


async def _prompt_more_stops_or_tools(session: dict) -> FlowResult:
    return FlowResult(state=AWAITING_MORE_STOPS, messages=[
        _text_msg("¿Necesitas más paradas?\n\n*Si alguna requiere un servicio distinto, solo dilo.*"),
    ], send_interactive=_interactive("button", "¿Más paradas?", buttons=[
        {"id": "yes", "title": "✅ Sí, agregar otra"},
        {"id": "no", "title": "❌ No, continuar"},
    ]))


# ── AWAITING_MORE_STOPS ───────────────────────────────────────────────────

@_handler(AWAITING_MORE_STOPS)
async def handle_more_stops(session: dict, text: str, button_id: str | None,
                            conversation) -> FlowResult:
    value = button_id if button_id else await _llm_classify_intent(text, AWAITING_MORE_STOPS)
    if value == "yes":
        data = _d(session)
        data["current_segment"] = data.get("current_segment", 0) + 1
        prev_dest = _seg(session, data["current_segment"] - 1).get("destination", {})
        new_seg = _cur_seg(session)
        new_seg["service_type"] = data.get("service_type")
        if prev_dest.get("address"):
            new_seg["origin"] = dict(prev_dest, confirmed=False)
        return FlowResult(state=AWAITING_ORIGIN, messages=[
            _text_msg("¿Dirección de la siguiente parada?"),
        ])

    if value == "no":
        st = _d(session).get("service_type", "")
        if st == "bancarios":
            return FlowResult(state=ASK_BANCARIOS_ENTITY, messages=[
                _text_msg("¿Para qué entidad bancaria es el trámite?"),
            ])
        return FlowResult(state=AWAITING_TOOLS, messages=[
            _text_msg("¿Necesitas herramientas adicionales? (Canasta, maletín térmico, etc.)"),
        ], send_interactive=await _build_tools_interactive(session))

    # Maybe user said "sí, y esa es mensajería"
    ses = _d(session)
    for svc in _SERVICE_LABELS:
        if svc in text.lower():
            ses["current_segment"] = ses.get("current_segment", 0) + 1
            prev_dest = _seg(session, ses["current_segment"] - 1).get("destination", {})
            new_seg = _cur_seg(session)
            new_seg["service_type"] = svc
            if prev_dest.get("address"):
                new_seg["origin"] = dict(prev_dest, confirmed=False)
            return FlowResult(state=AWAITING_ORIGIN, messages=[
                _text_msg("¿Dirección de la siguiente parada?"),
            ])

    return FlowResult(fallback=True, messages=[
        _text_msg("Responde 'Sí' para más paradas o 'No' para continuar."),
    ], send_interactive=_interactive("button", "¿Más paradas?", buttons=[
        {"id": "yes", "title": "✅ Sí"},
        {"id": "no", "title": "❌ No"},
    ]))


# ── Purchases/bancarios: AWAITING_DESCRIPTION ─────────────────────────────

@_handler(AWAITING_DESCRIPTION)
async def handle_description(session: dict, text: str, button_id: str | None,
                             conversation) -> FlowResult:
    if not text.strip():
        return FlowResult(messages=[_text_msg("Describe lo que necesitas.")])
    _d(session)["purchase_description"] = text.strip()[:500]
    st = _d(session).get("service_type")
    if st == "bancarios":
        return FlowResult(state=ASK_BANCARIOS_ENTITY, messages=[
            _text_msg("¿Para qué entidad bancaria es el trámite?"),
        ])
    return FlowResult(state=AWAITING_TOOLS, messages=[
        _text_msg("¿Necesitas herramientas adicionales?"),
    ], send_interactive=await _build_tools_interactive(session))


# ── Bancarios entity + reference ──────────────────────────────────────────

@_handler(ASK_BANCARIOS_ENTITY)
async def handle_bancarios_entity(session: dict, text: str, button_id: str | None,
                                  conversation) -> FlowResult:
    if not text.strip():
        return FlowResult(messages=[_text_msg("¿Qué banco o entidad?")])
    _d(session)["entity"] = text.strip()[:100]
    return FlowResult(state=ASK_BANCARIOS_REFERENCE, messages=[
        _text_msg("¿Cuál es la referencia o número de factura?"),
    ])


@_handler(ASK_BANCARIOS_REFERENCE)
async def handle_bancarios_reference(session: dict, text: str, button_id: str | None,
                                     conversation) -> FlowResult:
    _d(session)["reference"] = (text.strip() or "")[:100]
    return FlowResult(state=AWAITING_TOOLS, messages=[
        _text_msg("¿Necesitas herramientas adicionales?"),
    ], send_interactive=await _build_tools_interactive(session))


# ── AWAITING_TOOLS ─────────────────────────────────────────────────────────

_TOOLS_CACHE: list[dict] = []


async def _build_tools_interactive(session: dict) -> dict:
    global _TOOLS_CACHE
    tool_keys = _d(session).get("tool_keys", [])
    
    rows = []
    if tool_keys:
        rows.append({"id": "done", "title": "✅ Listo / Continuar", "description": "Continuar con el pedido"})
    else:
        rows.append({"id": "none", "title": "Ninguna", "description": "Sin herramientas adicionales"})
        
    try:
        _TOOLS_CACHE = await calculator.get_tools()
        for t in _TOOLS_CACHE:
            if isinstance(t, dict) and "key" in t:
                if t["key"] not in tool_keys:
                    rows.append({
                        "id": t["key"],
                        "title": (t.get("label") or t["key"])[:24],
                        "description": (t.get("description") or "")[:72],
                    })
    except Exception:
        pass  # empty tools list if API fails
    return _interactive("list",
        "Herramientas adicionales (elige las que necesites)",
        button_label="Herramientas",
        sections=[{"title": "Disponibles", "rows": rows}],
    )


@_handler(AWAITING_TOOLS)
async def handle_tools(session: dict, text: str, button_id: str | None,
                       conversation) -> FlowResult:
    data = _d(session)
    if button_id:
        if button_id == "done":
            return await _advance_to_payment(session)
        if button_id == "none":
            data["tool_keys"] = []
            return await _advance_to_payment(session)
            
        data.setdefault("tool_keys", [])
        if button_id not in data["tool_keys"]:
            data["tool_keys"].append(button_id)
        return FlowResult(messages=[_text_msg(f"✅ Herramienta agregada. ¿Alguna más?")],
                          send_interactive=await _build_tools_interactive(session))

    # Free text — LLM extracts tools, or "ninguna"
    intent = await _llm_classify_intent(text, AWAITING_TOOLS)
    if intent == "done":
        return await _advance_to_payment(session)
    if intent == "none" or intent == "ninguna":
        if not data.get("tool_keys"):
            data["tool_keys"] = []
        return await _advance_to_payment(session)
    if intent and intent in _TOOL_KEYS_CACHED():
        data.setdefault("tool_keys", [])
        if intent not in data["tool_keys"]:
            data["tool_keys"].append(intent)
        return FlowResult(messages=[_text_msg(f"✅ Agregada. ¿Alguna herramienta más?")],
                          send_interactive=await _build_tools_interactive(session))
    return FlowResult(fallback=True, messages=[
        _text_msg("Elige herramientas de la lista o presiona 'Listo' si ya terminaste."),
    ], send_interactive=await _build_tools_interactive(session))


def _TOOL_KEYS_CACHED():
    return {t["key"] for t in _TOOLS_CACHE if isinstance(t, dict)}


async def _advance_to_payment(session: dict) -> FlowResult:
    return FlowResult(state=AWAITING_PAYMENT, messages=[
        _text_msg("¿Cómo prefieres pagar?"),
    ], send_interactive=_interactive("button", "Método de pago", buttons=[
        {"id": "efectivo", "title": "Efectivo"},
        {"id": "nequi", "title": "Nequi (+$500)"},
    ]))


# ── AWAITING_PAYMENT ──────────────────────────────────────────────────────

@_handler(AWAITING_PAYMENT)
async def handle_payment(session: dict, text: str, button_id: str | None,
                         conversation) -> FlowResult:
    value = button_id if button_id else await _llm_classify_intent(text, AWAITING_PAYMENT)
    if value in ("efectivo", "nequi"):
        _d(session)["payment_method"] = value
        return FlowResult(state=AWAITING_ACOMPANANTE, messages=[
            _text_msg("¿Necesitas acompañante para objetos pesados?"),
        ], send_interactive=_interactive("button", "¿Acompañante?", buttons=_yes_no_buttons()))
    return FlowResult(fallback=True, messages=[
        _text_msg("Elige 'Efectivo' o 'Nequi'."),
    ], send_interactive=_interactive("button", "Método de pago", buttons=[
        {"id": "efectivo", "title": "Efectivo"},
        {"id": "nequi", "title": "Nequi (+$500)"},
    ]))


# ── AWAITING_ACOMPANANTE ──────────────────────────────────────────────────

@_handler(AWAITING_ACOMPANANTE)
async def handle_acompanante(session: dict, text: str, button_id: str | None,
                             conversation) -> FlowResult:
    value = button_id if button_id else await _llm_classify_intent(text, AWAITING_ACOMPANANTE)
    if value == "yes":
        _d(session)["acompanante"] = True
    elif value == "no":
        _d(session)["acompanante"] = False
    else:
        return FlowResult(fallback=True, messages=[
            _text_msg("Responde 'Sí' o 'No'."),
        ], send_interactive=_interactive("button", "¿Acompañante?", buttons=_yes_no_buttons()))

    # Call calculate_price
    return await _call_calculate_price(session)


# ── SHOW_PRICE (internal, called from ACOMPANANTE) ────────────────────────

async def _call_calculate_price(session: dict) -> FlowResult:
    data = _d(session)
    segments = data.get("segments", [])
    if not segments:
        return FlowResult(escalate=True, escalate_reason="No hay segmentos para calcular precio")

    service_type = data.get("service_type", "domicilios")
    profile = data.get("profile", "usuario_final")
    payment_method = data.get("payment_method", "efectivo")
    acompanante = data.get("acompanante", False)
    tool_keys = data.get("tool_keys", [])

    # Build segments for API
    api_segments = []
    for seg in segments:
        origin = seg.get("origin", {})
        destination = seg.get("destination", {})
        api_segments.append({
            "service_type": seg.get("service_type", service_type),
            "description": seg.get("description") or data.get("purchase_description", ""),
            "origin": {
                "address": origin.get("address") or "Tuluá centro",
                "lat": origin.get("lat"),
                "lng": origin.get("lng"),
            },
            "destination": {
                "address": destination.get("address") or "Tuluá centro",
                "lat": destination.get("lat"),
                "lng": destination.get("lng"),
            },
            "instructions": seg.get("instructions"),
        })

    try:
        price_result = await calculator.calculate_price(
            profile=profile,
            segments=api_segments,
            tools=tool_keys,
            payment_method=payment_method,
            acompanante=acompanante,
        )
    except Exception as e:
        logger.exception("calculate_price failed for conv")
        return FlowResult(messages=[
            _text_msg("Ocurrió un error al calcular el precio. Intenta de nuevo o habla con un asesor."),
        ])

    if not isinstance(price_result, dict) or "error" in price_result:
        return FlowResult(messages=[
            _text_msg("No se pudo calcular el precio. Verifica los datos e intenta de nuevo."),
        ])

    data["last_price_result"] = price_result
    breakdown = price_result.get("breakdown", {})
    total = breakdown.get("total", 0)
    total_km = price_result.get("route", {}).get("total_km", 0)
    is_fixed = price_result.get("route", {}).get("is_fixed_route", False)

    lines = ["*Resumen de tu pedido:*\n"]
    for i, seg in enumerate(segments):
        svc = seg.get("service_type", service_type)
        label = _SERVICE_LABELS.get(svc, svc)
        o = seg.get("origin", {}).get("address") or "No especificado"
        d = seg.get("destination", {}).get("address") or "No especificado"
        desc = seg.get("description") or ""
        lines.append(f"*Parada {i + 1}: {label}*")
        if desc:
            lines.append(f"📦 {desc}")
        lines.append(f"📍 {o} → {d}")
        lines.append("")

    if is_fixed:
        lines.append(f"💵 *Total: ${total:,} COP* (tarifa fija)")
    else:
        lines.append(f"📏 Distancia: {total_km:.1f} km")

    lines.append(f"💵 *Total: ${total:,} COP*")
    if payment_method == "nequi":
        lines.append("(+$500 recargo Nequi)")

    weather = price_result.get("weather", {})
    if weather.get("is_raining"):
        lines.append("🌧 Recargo por lluvia: +$500 COP")

    lines.append("")
    lines.append("¿Confirmas el pedido?")

    return FlowResult(
        state=CONFIRMING_QUOTE,
        messages=[_text_msg("\n".join(lines))],
        send_interactive=_interactive("button", "¿Confirmas?", buttons=[
            {"id": "confirm", "title": "✅ Confirmar"},
            {"id": "change", "title": "✏️ Cambiar algo"},
            {"id": "cancel", "title": "❌ Cancelar"},
        ]),
    )


# ── CONFIRMING_QUOTE ──────────────────────────────────────────────────────

@_handler(CONFIRMING_QUOTE)
async def handle_confirm_quote(session: dict, text: str, button_id: str | None,
                               conversation) -> FlowResult:
    value = button_id if button_id else await _llm_classify_intent(text, CONFIRMING_QUOTE)
    if value == "confirm":
        return FlowResult(state=ASK_KNOWS_RECIPIENT, messages=[
            _text_msg("¿Sabes quién recibe el pedido?"),
        ], send_interactive=_interactive("button", "¿Sabes quién recibe?", buttons=_yes_no_buttons()))
    if value == "change":
        # Back to service type, but keep collected data
        return FlowResult(state=AWAITING_SERVICE_TYPE, messages=[
            _text_msg("Vamos a empezar de nuevo. ¿Qué servicio necesitas?"),
        ], send_interactive=_service_type_list())
    if value == "cancel":
        return FlowResult(state=WELCOME, messages=[
            _text_msg("Pedido cancelado. \u00a1Hasta luego!"),
        ])
    return FlowResult(fallback=True, messages=[
        _text_msg("Elige 'Confirmar', 'Cambiar' o 'Cancelar'."),
    ], send_interactive=_interactive("button", "¿Confirmas?", buttons=[
        {"id": "confirm", "title": "✅ Confirmar"},
        {"id": "change", "title": "✏️ Cambiar algo"},
        {"id": "cancel", "title": "❌ Cancelar"},
    ]))


# ── ASK_KNOWS_RECIPIENT ────────────────────────────────────────────────────

@_handler(ASK_KNOWS_RECIPIENT)
async def handle_knows_recipient(session: dict, text: str, button_id: str | None,
                                 conversation) -> FlowResult:
    if len(_d(session).get("segments", [])) > 1:
        return FlowResult(state=AWAITING_SENDER_NAME, messages=[
            _text_msg("¿Cuál es tu nombre?"),
        ])
    value = button_id if button_id else await _llm_classify_intent(text, ASK_KNOWS_RECIPIENT)
    if value == "yes":
        return FlowResult(state=AWAITING_RECIPIENT_NAME, messages=[
            _text_msg("¿Nombre de la persona que recibe?"),
        ])
    if value == "no":
        return FlowResult(state=AWAITING_SENDER_NAME, messages=[
            _text_msg("¿Cuál es tu nombre?"),
        ])
    return FlowResult(fallback=True, messages=[
        _text_msg("Responde si sabes o no quién recibe el pedido."),
    ], send_interactive=_interactive("button", "¿Sabes quién recibe?", buttons=_yes_no_buttons()))


# ── AWAITING_RECIPIENT_NAME ───────────────────────────────────────────────

@_handler(AWAITING_RECIPIENT_NAME)
async def handle_recipient_name(session: dict, text: str, button_id: str | None,
                                conversation) -> FlowResult:
    if not text.strip():
        return FlowResult(fallback=True, messages=[_text_msg("Escribe el nombre del receptor.")])
    _d(session)["recipient_name"] = text.strip()[:100]
    return FlowResult(state=AWAITING_RECIPIENT_PHONE, messages=[
        _text_msg("¿Teléfono del receptor?"),
    ])


# ── AWAITING_RECIPIENT_PHONE ──────────────────────────────────────────────

@_handler(AWAITING_RECIPIENT_PHONE)
async def handle_recipient_phone(session: dict, text: str, button_id: str | None,
                                 conversation) -> FlowResult:
    phone = re.sub(r"\D", "", text.strip()) if text else ""
    if len(phone) < 10:
        return FlowResult(fallback=True, messages=[_text_msg("Ingresa un número válido (ej: 3151234567).")])
    _d(session)["recipient_phone"] = phone
    return await _submit_order(conversation, session)


# ── SUBMIT_ORDER ──────────────────────────────────────────────────────────

async def _submit_order(conversation, session: dict) -> FlowResult:
    data = _d(session)
    segments = data.get("segments", [])
    service_type = data.get("service_type", "domicilios")
    profile = data.get("profile", "usuario_final")
    payment_method = data.get("payment_method", "efectivo")
    total = data.get("last_price_result", {}).get("breakdown", {}).get("total", 0)
    contact_name = data.get("recipient_name") or data.get("sender_name", "")
    contact_phone = data.get("recipient_phone", "")

    lines = ["*Domii Tuluá - Nuevo Pedido*\n"]
    if contact_name and contact_phone:
        lines.append(f"*Contacto:* {contact_name} ({contact_phone})\n")
    elif contact_name:
        lines.append(f"*Contacto:* {contact_name}\n")
    for i, seg in enumerate(segments):
        svc = seg.get("service_type", service_type)
        label = _SERVICE_LABELS.get(svc, svc)
        o = seg.get("origin", {}).get("address") or "N/E"
        d = seg.get("destination", {}).get("address") or "N/E"
        desc = seg.get("description") or ""
        instr = seg.get("instructions") or ""
        lines.append(f"  {i+1}. *{label}*")
        if desc:
            lines.append(f"     📦 {desc}")
        lines.append(f"     {o} → {d}")
        if instr:
            lines.append(f"     📝 {instr}")

    lines.append(f"\n*Perfil:* {profile}")
    tools = data.get("tool_keys", [])
    if tools:
        lines.append(f"*Herramientas:* {', '.join(tool for tool in tools)}")
    if payment_method == "nequi":
        lines.append(f"*Pago:* Nequi (+$500)")
    else:
        lines.append(f"*Pago:* Efectivo")
    lines.append(f"*Total:* ${total:,} COP")

    # TODO: send to operations WhatsApp group
    logger.info("ORDER SUBMITTED for conv=%s\n%s", conversation.id, "\n".join(lines))

    if await sync_to_async(get_escalate_orders_enabled)():
        return FlowResult(
            state=WELCOME,
            escalate=True,
            escalate_reason="\n".join(lines),
            messages=[
                _text_msg("✅ *Pedido enviado exitosamente!*\n\nUn domiciliario será asignado pronto.\n\n¿Necesitas algo más?"),
            ],
        )

    return FlowResult(
        state=WELCOME,
        messages=[
            _text_msg("✅ *Pedido enviado exitosamente!*\n\nUn domiciliario será asignado pronto.\n\n¿Necesitas algo más?"),
        ],
    )


# ── AWAITING_SENDER_NAME ──────────────────────────────────────────────────

@_handler(AWAITING_SENDER_NAME)
async def handle_sender_name(session: dict, text: str, button_id: str | None,
                             conversation) -> FlowResult:
    if not text.strip():
        return FlowResult(fallback=True, messages=[_text_msg("Escribe tu nombre.")])
    _d(session)["sender_name"] = text.strip()[:100]
    return await _submit_order(conversation, session)


# ── DOMII FIJO states ─────────────────────────────────────────────────────

@_handler(AWAITING_FIJO_NAME)
async def handle_fijo_name(session: dict, text: str, button_id: str | None,
                           conversation) -> FlowResult:
    if not text.strip():
        return FlowResult(messages=[_text_msg("Escribe el nombre del negocio.")])
    session["domii_fijo_data"]["business_name"] = text.strip()[:100]
    return FlowResult(state=AWAITING_FIJO_ADDR, messages=[
        _text_msg("¿Dirección del negocio?"),
    ])


@_handler(AWAITING_FIJO_ADDR)
async def handle_fijo_addr(session: dict, text: str, button_id: str | None,
                           conversation) -> FlowResult:
    if not text.strip():
        return FlowResult(messages=[_text_msg("Escribe la dirección.")])
    session["domii_fijo_data"]["address"] = text.strip()[:300]
    return FlowResult(state=AWAITING_FIJO_PHONE, messages=[
        _text_msg("¿Teléfono de contacto?"),
    ])


@_handler(AWAITING_FIJO_PHONE)
async def handle_fijo_phone(session: dict, text: str, button_id: str | None,
                            conversation) -> FlowResult:
    phone = re.sub(r"\D", "", text.strip()) if text else ""
    if len(phone) < 10:
        return FlowResult(messages=[_text_msg("Ingresa un número válido (ej: 3151234567).")])
    session["domii_fijo_data"]["phone"] = phone
    return FlowResult(state=AWAITING_FIJO_DATE, messages=[
        _text_msg("¿Para qué fecha lo necesitas? (Ej: 15/06/2026 o 'hoy', 'mañana')"),
    ])


@_handler(AWAITING_FIJO_DATE)
async def handle_fijo_date(session: dict, text: str, button_id: str | None,
                           conversation) -> FlowResult:
    if not text.strip():
        return FlowResult(messages=[_text_msg("Escribe la fecha.")])
    session["domii_fijo_data"]["date"] = text.strip()[:50]
    return FlowResult(state=AWAITING_FIJO_START, messages=[
        _text_msg("¿A qué hora empieza? (Ej: 08:00)"),
    ])


@_handler(AWAITING_FIJO_START)
async def handle_fijo_start(session: dict, text: str, button_id: str | None,
                            conversation) -> FlowResult:
    if not text.strip():
        return FlowResult(messages=[_text_msg("Escribe la hora de inicio.")])
    session["domii_fijo_data"]["start_time"] = text.strip()[:10]
    return FlowResult(state=AWAITING_FIJO_END, messages=[
        _text_msg("¿A qué hora termina? (Debe ser después de la hora de inicio.)"),
    ])


@_handler(AWAITING_FIJO_END)
async def handle_fijo_end(session: dict, text: str, button_id: str | None,
                          conversation) -> FlowResult:
    if not text.strip():
        return FlowResult(messages=[_text_msg("Escribe la hora de fin.")])
    session["domii_fijo_data"]["end_time"] = text.strip()[:10]
    return FlowResult(state=AWAITING_FIJO_VOLUME, messages=[
        _text_msg("¿Volumen estimado de pedidos?"),
    ], send_interactive=_interactive("button",
        "Volumen estimado",
        buttons=[
            {"id": "1-5", "title": "1-5 pedidos"},
            {"id": "5-15", "title": "5-15 pedidos"},
            {"id": "15+", "title": "Más de 15"},
        ],
    ))


@_handler(AWAITING_FIJO_VOLUME)
async def handle_fijo_volume(session: dict, text: str, button_id: str | None,
                             conversation) -> FlowResult:
    value = button_id if button_id else text.strip()[:20]
    if not value:
        return FlowResult(messages=[_text_msg("Selecciona un volumen.")])
    session["domii_fijo_data"]["volume"] = value
    df = session["domii_fijo_data"]
    summary = (
        f"*Resumen Domii Fijo:*\n\n"
        f"*Negocio:* {(df.get('business_name', 'N/E') or '')[:80]}\n"
        f"*Dirección:* {(df.get('address', 'N/E') or '')[:80]}\n"
        f"*Teléfono:* {df.get('phone', 'N/E')}\n"
        f"*Fecha:* {(df.get('date', 'N/E') or '')[:30]}\n"
        f"*Horario:* {(df.get('start_time', 'N/E') or '')[:10]} - {(df.get('end_time', 'N/E') or '')[:10]}\n"
        f"*Volumen:* {(df.get('volume', 'N/E') or '')[:20]}\n\n"
        "¿Confirmas?"
    )
    return FlowResult(state=CONFIRMING_FIJO, messages=[
        _text_msg(summary),
    ], send_interactive=_interactive("button", "¿Confirmas?", buttons=[
        {"id": "confirm", "title": "✅ Confirmar"},
        {"id": "cancel", "title": "❌ Cancelar"},
    ]))


@_handler(CONFIRMING_FIJO)
async def handle_fijo_confirm(session: dict, text: str, button_id: str | None,
                              conversation) -> FlowResult:
    value = button_id if button_id else await _llm_classify_intent(text, CONFIRMING_FIJO)
    if value == "confirm":
        df = session["domii_fijo_data"]
        lines = [
            "*Domii Tulu\u00e1 - Domii Fijo*",
            "",
            f"*Empresa:* {(df.get('business_name', 'N/E') or '')[:80]}",
            f"*Direcci\u00f3n:* {(df.get('address', 'N/E') or '')[:80]}",
            f"*Tel\u00e9fono:* {df.get('phone', 'N/E')}",
            f"*Fecha:* {(df.get('date', 'N/E') or '')[:30]}",
            f"*Horario:* {(df.get('start_time', 'N/E') or '')[:10]} - {(df.get('end_time', 'N/E') or '')[:10]}",
            f"*Volumen:* {(df.get('volume', 'N/E') or '')[:20]}",
            "",
            "*Nota:* Sujeto a disponibilidad de flota.",
        ]
        logger.info("DOMII FIJO REQUEST conv=%s\n%s", conversation.id, "\n".join(lines))

        if await sync_to_async(get_escalate_orders_enabled)():
            return FlowResult(
                state=WELCOME,
                escalate=True,
                escalate_reason="\n".join(lines),
                messages=[
                    _text_msg("✅ *Solicitud enviada!* Un asesor confirmará la disponibilidad.\n\n¿Necesitas algo más?"),
                ],
            )

        return FlowResult(state=WELCOME, messages=[
            _text_msg("✅ *Solicitud enviada!* Un asesor confirmará la disponibilidad.\n\n¿Necesitas algo más?"),
        ])
    if value == "cancel":
        return FlowResult(state=WELCOME, messages=[
            _text_msg("Solicitud cancelada. \u00a1Hasta luego!"),
        ])
    return FlowResult(fallback=True, messages=[
        _text_msg("Elige 'Confirmar' o 'Cancelar'."),
    ], send_interactive=_interactive("button", "¿Confirmas?", buttons=[
        {"id": "confirm", "title": "✅ Confirmar"},
        {"id": "cancel", "title": "❌ Cancelar"},
    ]))


# ---------------------------------------------------------------------------
#  LLM fallback
# ---------------------------------------------------------------------------

from . import llm_fallback as _llm_fallback


async def _llm_classify_intent(text: str, state_name: str) -> str | None:
    """Classify free text into a button ID for the given state."""
    if not text.strip():
        return None
    try:
        dynamic_options = None
        if state_name == AWAITING_TOOLS:
            dynamic_options = [{"id": t["key"], "label": t.get("label", t["key"])} for t in _TOOLS_CACHE if isinstance(t, dict)]
            
        return await _llm_fallback.classify_free_text(text, state_name, dynamic_options=dynamic_options)
    except Exception:
        logger.exception("LLM fallback failed")
        return None


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
#  Confusion handling
# ---------------------------------------------------------------------------

_CONFUSION_PATTERNS = re.compile(
    r"(no\s+(entiendo|s[eé]|comprendo|entend[ií]|le\s+entiendo|le\s+se|"
    r"sab[ií]a|tengo\s+idea|capto|me\s+queda\s+claro|s[eé]\s+qu[eé]|"
    r"entend[ií]\s+bien|le\s+entend[ií]))|"
    r"sigo\s+sin\s+(entender|comprender|saber)|"
    r"repite|otra\s+vez|expl[ií]came|me\s+explicas?\b|c[oó]mo\s+as[ií]|"
    r"perd[oó]n|disculpa|disculpe|"
    r"no\s+(me\s+)?(acuerdo|recuerdo)|"
    r"no\s+(le|te)\s+entend[ií]|"
    r"no\s+(te\s+)?capto|"
    r"nunca\s+entend[ií]|nunca\s+comprend[ií]|"
    r"(ay[úu]dame|ayuda|me\s+ayudas)",  # frequent in confusion context too
    re.I,
)

# ── Global cancel/escalate patterns (same as dispatcher) ───────────────────
_GLOBAL_CANCEL = re.compile(
    r"\b(?:salir|cancelar|cancel|d[eé]jame|"
    r"ya\s+no\s+(?:quiero|necesito)|no\s+m[áa]s)\b",
    re.I,
)
_GLOBAL_MENU_RETURN = re.compile(
    r"\b(?:"
    r"men[úu]\s+principal|"
    r"volver\s+(?:al?\s+)?(?:men[úu]|inicio|empezar|principio)|"
    r"regresar\s+(?:al?\s+)?(?:men[úu]|inicio|principio)|"
    r"vuelve\s+a\s+(?:men[úu]|inicio)|"
    r"ir\s+(?:al\s+)?men[úu])\b",
    re.I,
)
_GLOBAL_ESCALATE = re.compile(
    r"\b(agente|asesor|humano|persona|operador|"
    r"hablar\s+con|atenci[oó]n|atender|atenderme|"
    r"p[aá]same\s+con|quiero\s+(que\s+)?(me\s+)?(atienda|hable|ayuden|una\s+persona)|"
    r"necesito\s+(ayuda|hablar|una\s+persona|un\s+asesor)|"
    r"ay[uú]dame|ay[uú]da\s+por\s+favor|"
    r"no\s+(funciona|sirve|sirves)|"
    r"esto\s+no|mejor\s+(hablo|llamo|quiero)\s+con|"
    r"comun[ií]came|transfi[eé]reme|"
    r"qu[eé]\s+pereza|qu[eé]\s+fastidio)\b",
    re.I | re.MULTILINE,
)

# Prevent false cancel when "cancelar" means "pagar" in Colombian Spanish
_PAYMENT_WORDS = re.compile(r"\b(cuenta|factura|pago|recibo|total|tarjeta|transferencia|nequi|bancolombia|daviplata|billetera|efectivo|pedido|servicio|pesos|valor)\b", re.I)

# Domii Fijo info-request patterns — when user is in fijo flow but asking for
# FAQ, pricing, or saying "no soy negocio / no quiero contratar"
_DOMII_FIJO_INFO = re.compile(
    r"\b(?:"
    r"c[óo]mo\s+(?:funciona|es|hago|se\s+hace)|"
    r"informaci[óo]n(?!\s+general\b)|"
    r"(?:solo|s[óo]lo)\s+(?:quer[íi]a|necesito|busco)\s+(?:info|saber|conocer|informaci[óo]n)|"
    r"no\s+(?:soy|tengo)\s+(?:negocio|empresa|comercio)|"
    r"no\s+quiero\s+(?:contratar|el\s+servicio|pedir)|"
    r"me\s+equivoqu[ée]|"
    r"cu[áa]nto\s+(?:cuesta|vale|sale|es\s+el\s+precio|se\s+cobra)|"
    r"precio|tarifa)\b",
    re.I,
)
_FIJO_STATES = {"AWAITING_FIJO_NAME", "AWAITING_FIJO_ADDR", "AWAITING_FIJO_PHONE",
                 "AWAITING_FIJO_DATE", "AWAITING_FIJO_START", "AWAITING_FIJO_END",
                 "AWAITING_FIJO_VOLUME", "CONFIRMING_FIJO"}


def _is_actual_cancel(text: str) -> bool:
    if not _GLOBAL_CANCEL.search(text):
        return False
    if "cancelar" in text.lower() and _PAYMENT_WORDS.search(text):
        return False
    # Long messages (>150 chars) are unlikely to be a pure cancel intent —
    # the keyword is likely embedded in a broader request
    if len(text) > 150:
        return False
    return True


_STATE_EXPLANATIONS: dict[str, str] = {
    WELCOME: (
        "Elige una opción escribiendo el nombre o usando los botones:\n"
        "- *Cotizar* para calcular el precio de un domicilio\n"
        "- *Domii Fijo* para un domiciliario dedicado\n"
        "- *Asesor* para hablar con una persona\n"
        "- *Preguntas* para dudas frecuentes"
    ),
    AWAITING_PROFILE: (
        "Elige quién eres:\n"
        "- *Usuario final* si eres una persona natural\n"
        "- *Negocio* si eres un restaurante, tienda o empresa"
    ),
    AWAITING_SERVICE_TYPE: (
        "Elige el servicio:\n"
        "- *Domicilios* — envío de comida, productos\n"
        "- *Mensajería* — documentos, paquetes\n"
        "- *Compras* — que compren por ti en tiendas\n"
        "- *Trámites* — diligencias varias\n"
        "- *Bancarios* — pagos, facturas, bancos"
    ),
    ASK_PACKAGE_TYPE: (
        "¿Qué tipo de paquete envías?\n"
        "- *Documento* — carta, sobre, papeles\n"
        "- *Paquete* — caja, bulto, objeto\n"
        "- *Frágil* — vidrio, electrónico, delicado\n"
        "- *Alimento* — comida, bebida, helado\n"
        "- *Otro* — algo diferente"
    ),
    ASK_WHO_PAYS: (
        "¿Quién paga el envío?\n"
        "- *Remitente* — la persona que envía\n"
        "- *Destinatario* — la persona que recibe"
    ),
    AWAITING_ORIGIN: (
        "Escribe la dirección de origen donde el domiciliario debe recoger.\n"
        "Ejemplo: 'Cra 10 #12-34, Tuluá' o 'La Herradura'."
    ),
    AWAITING_ORIGIN_SELECT: (
        "Elige una de las direcciones de la lista, o selecciona 'Ninguna' para escribir otra."
    ),
    CONFIRMING_ORIGIN: (
        "Confirma si la dirección de origen es correcta. Responde 'Sí' o 'No'."
    ),
    AWAITING_DESTINATION: (
        "Escribe la dirección de destino donde debe llegar el pedido.\n"
        "Ejemplo: 'Calle 5 #8-90, Tuluá'."
    ),
    AWAITING_DEST_SELECT: (
        "Elige una de las direcciones de la lista, o selecciona 'Ninguna' para escribir otra."
    ),
    CONFIRMING_DEST: (
        "Confirma si la dirección de destino es correcta. Responde 'Sí' o 'No'."
    ),
    AWAITING_SEGMENT_DESCRIPTION: (
        "Describe lo que se entrega. Ejemplo: 'Una hamburguesa' o 'Un sobre con documentos'."
    ),
    AWAITING_SEGMENT_INSTRUCTIONS: (
        "Si el domiciliario necesita instrucciones, escríbelas. Ej: 'Tocar timbre dos veces'.\n"
        "Si no hay instrucciones, escribe 'No'."
    ),
    AWAITING_MORE_STOPS: (
        "¿Necesitas alguna parada adicional? Responde 'Sí' para agregar otra o 'No' para continuar."
    ),
    AWAITING_DESCRIPTION: (
        "Describe lo que necesitas. Ejemplo: 'Un mercado pequeño' o 'Un trámite en Bancolombia'."
    ),
    ASK_BANCARIOS_ENTITY: (
        "¿En qué banco o entidad es el trámite?\nEjemplo: Bancolombia, Nequi, Davivienda."
    ),
    ASK_BANCARIOS_REFERENCE: (
        "¿Cuál es el número de referencia o factura? Si no lo tienes, escribe 'No sé'."
    ),
    AWAITING_TOOLS: (
        "Elige las herramientas adicionales que necesites de la lista, o 'Ninguna' si no requieres."
    ),
    AWAITING_PAYMENT: (
        "Elige método de pago:\n- *Efectivo* (sin recargo)\n- *Nequi* (+$500 de recargo)"
    ),
    AWAITING_ACOMPANANTE: (
        "¿Necesitas un acompañante para objetos pesados? Responde 'Sí' o 'No'."
    ),
    CONFIRMING_QUOTE: (
        "Confirma tu pedido:\n- *Confirmar* para enviar\n- *Cambiar* para modificar datos\n- *Cancelar* para descartar"
    ),
    ASK_KNOWS_RECIPIENT: (
        "Responde si sabes o no quién recibirá el pedido."
    ),
    AWAITING_RECIPIENT_NAME: (
        "Escribe el nombre completo de la persona que recibe el pedido."
    ),
    AWAITING_RECIPIENT_PHONE: (
        "Escribe el número de teléfono de la persona que recibe.\nEjemplo: 3151234567."
    ),
    AWAITING_SENDER_NAME: (
        "Escribe tu nombre para que el domiciliario sepa a quién contactar."
    ),
    AWAITING_FIJO_NAME: "Escribe el nombre de tu negocio.",
    AWAITING_FIJO_ADDR: "Escribe la dirección del negocio donde se prestará el servicio.",
    AWAITING_FIJO_PHONE: "Escribe un número de teléfono de contacto. Ejemplo: 3151234567.",
    AWAITING_FIJO_DATE: "¿Para qué fecha? Ejemplo: '15/06/2026', 'hoy' o 'mañana'.",
    AWAITING_FIJO_START: "¿A qué hora empieza? Ejemplo: 08:00, 9:30.",
    AWAITING_FIJO_END: "¿A qué hora termina? Debe ser después de la hora de inicio.",
    AWAITING_FIJO_VOLUME: (
        "Selecciona el volumen estimado de pedidos:\n"
        "- *1-5* pedidos\n- *5-15* pedidos\n- *Más de 15* pedidos"
    ),
    CONFIRMING_FIJO: (
        "Confirma los datos de tu solicitud de Domii Fijo.\n- *Confirmar* para enviar\n- *Cancelar* para descartar"
    ),
}


def _get_state_explanation(state_name: str) -> str:
    return _STATE_EXPLANATIONS.get(state_name, "¿En qué puedo ayudarte?")


async def advance(conversation, session: dict, user_text: str,
                  button_id: str | None = None) -> FlowResult:
    """Process one turn of the state machine.

    Args:
        conversation: Conversation ORM instance.
        session: Bot session dict (mutated in place).
        user_text: The user's message content.
        button_id: If the message was an interactive button/list reply, its ID.

    Returns:
        A FlowResult describing what to send to the user.
    """
    state_name = session.get("state") or WELCOME

    # ── Global menu-return, cancel, escalate keywords (before confusion) ──
    if not button_id and len(user_text) <= 150 and \
       _GLOBAL_MENU_RETURN.search(user_text) and not _is_actual_cancel(user_text):
        session.clear()
        return FlowResult(state=WELCOME, messages=[
            _text_msg("De acuerdo, volvamos al men\u00fa principal."),
        ], send_interactive=_welcome_interactive())

    # ── FAQ request from fijo flow — user wants FAQ, not Domii Fijo info ──
    if not button_id and state_name in _FIJO_STATES and \
       re.search(r"preguntas?\s+(?:frecuentes?|generales)?", user_text, re.I):
        session.clear()
        return FlowResult(state=WELCOME, messages=[
            _text_msg("De acuerdo, volvamos al men\u00fa principal."),
        ], send_interactive=_welcome_interactive())

    # ── Domii Fijo info-request — user is in fijo flow but asking for
    #    FAQ, pricing, or clarifying they aren't a business ──
    if not button_id and state_name in _FIJO_STATES and _DOMII_FIJO_INFO.search(user_text):
        session.clear()
        return FlowResult(state=WELCOME, messages=[
            _text_msg(
                "Entendido. Domii Fijo es nuestro servicio de domiciliario dedicado "
                "por horas o d\u00edas.\n\n"
                "\u2022 *\u00bfC\u00f3mo funciona?* Asignamos un mensajero exclusivo "
                "para tu negocio durante el horario que elijas.\n"
                "\u2022 *Precio:* Depende del volumen estimado y horario.\n"
                "\u2022 *Pago:* Efectivo o Nequi (+$500).\n\n"
                "Elige una opci\u00f3n del men\u00fa cuando quieras continuar."
            ),
        ], send_interactive=_welcome_interactive())

    # ── Domii Fijo mention from a non-fijo flow — user wants to switch ──
    if not button_id and state_name not in _FIJO_STATES and state_name != WELCOME and \
       re.search(r"\bDomii\s+Fijo\b", user_text, re.I):
        session.clear()
        return FlowResult(state=WELCOME, messages=[
            _text_msg(
                "Entendido, hablemos de Domii Fijo.\n\n"
                "\u2022 *\u00bfC\u00f3mo funciona?* Asignamos un mensajero exclusivo "
                "para tu negocio durante el horario que elijas.\n"
                "\u2022 *Precio:* Depende del volumen estimado y horario.\n"
                "\u2022 *Pago:* Efectivo o Nequi (+$500).\n\n"
                "Elige una opci\u00f3n del men\u00fa para continuar."
            ),
        ], send_interactive=_welcome_interactive())

    if not button_id and _is_actual_cancel(user_text):
        session.clear()
        return FlowResult(state=WELCOME, messages=[
            _text_msg("\u00a1Hasta luego! Cuando necesites algo, solo escr\u00edbeme."),
        ])

    if not button_id and len(user_text) <= 150 and _GLOBAL_ESCALATE.search(user_text):
        reason = await _escalation_note(session, "Cliente solicit\u00f3 asesor durante la conversaci\u00f3n")
        session.clear()
        return FlowResult(
            escalate=True,
            escalate_reason=reason,
            messages=[_text_msg("Un asesor te atender\u00e1 pronto.")],
        )

    # ── Confusion detection (only for free text, not button taps) ──
    if not button_id and _CONFUSION_PATTERNS.search(user_text):
        session["error_count"] = session.get("error_count", 0) + 1
        logger.info("Confusion detected in state=%s error_count=%d", state_name, session["error_count"])

        if session["error_count"] >= 2:
            return FlowResult(
                escalate=True,
                escalate_reason=await _escalation_note(session, "Cliente confundido tras varios intentos"),
                messages=[
                    _text_msg("Veo que tienes dificultades. Un asesor te atender\u00e1 pronto."),
                ],
            )

        explanation = _get_state_explanation(state_name)
        return FlowResult(
            state=state_name,
            messages=[_text_msg(
                f"\u00a1Con gusto! Esto es lo que puedes hacer:\n\n{explanation}\n\n"
                "Si prefieres, escribe *asesor* para hablar con una persona."
            )],
            fallback=False,
        )

    # ── Reset confusion on any meaningful input ──
    session["confusion_count"] = 0

    handler = _STATES.get(state_name)
    if not handler:
        logger.warning("Unknown state %r, resetting to WELCOME", state_name)
        session["state"] = WELCOME
        handler = _STATES[WELCOME]

    handler = _STATES.get(state_name)

    result = await handler(session, user_text, button_id, conversation)

    if result.state is not None:
        session["state"] = result.state

    if result.escalate:
        pass
    elif result.fallback:
        session["error_count"] = session.get("error_count", 0) + 1
        if session.get("error_count", 0) >= 2:
            return FlowResult(
                escalate=True,
                escalate_reason=await _escalation_note(
                    session, "Cliente no pudo completar el paso"
                ),
                messages=[_text_msg(
                    "He tenido dificultades para procesar tu solicitud. "
                    "Un asesor te atender\u00e1 pronto."
                )],
            )
    else:
        session["error_count"] = 0

    result.state = session.get("state") or WELCOME
    return result


def get_welcome_interactive() -> dict:
    """Return the WELCOME interactive payload (used for initial greeting)."""
    return _welcome_interactive()


def build_initial_session() -> dict:
    """Create a fresh session dict with default values."""
    return {
        "state": WELCOME,
        "fallback_count": 0,
        "error_count": 0,
        "history": [],
        "data": {
            "collected": {
                "profile": None,
                "service_type": None,
                "segments": [],
                "current_segment": 0,
                "tool_keys": [],
                "payment_method": None,
                "acompanante": None,
                "geocode_fail_count": 0,
            },
        },
    }


def get_state_prompt(state_name: str) -> str | None:
    """Return the prompt text for a given state (for pre-LLM replies)."""
    # This is used by the dispatcher to send the initial prompt when entering a state
    handler = _STATES.get(state_name)
    if not handler:
        return None
    return None  # prompts are generated by handlers dynamically



