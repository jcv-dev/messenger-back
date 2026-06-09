from typing import Callable, Optional
from .fallback import FallbackRequired
from api.bot.session import save_session


class StateDefinition:
    def __init__(self, *, prompt: str, extract: Callable, next_state: Callable,
                 validate: Optional[Callable] = None,
                 error_message: Optional[str] = None,
                 key: Optional[str] = None):
        self.prompt = prompt
        self.extract = extract
        self.next_state = next_state
        self.validate = validate
        self.error_message = error_message or "No entendí. Por favor intenta de nuevo."
        self.key = key


class extractors:
    @staticmethod
    def number(text, session):
        t = text.strip()
        return int(t) if t.isdigit() else None

    @staticmethod
    def yes_no(text, session):
        return text.strip().lower() in ("si", "sí", "s", "yes", "1")

    @staticmethod
    def address(text, session):
        return text.strip()

    @staticmethod
    def text(text, session):
        return text.strip()


SERVICE_CHOICES = ["domicilios", "mensajeria", "purchases", "tramites", "bancarios"]


def _populate_segment_origin(s):
    segs = s["data"].setdefault("segments", [])
    if not segs:
        return
    seg = segs[-1]
    if "origin_input" in s["data"]:
        seg.setdefault("origin", {})
        seg["origin"]["address"] = s["data"].pop("origin_input")
    elif "origin" not in seg:
        seg["origin"] = {"address": "Tuluá centro", "lat": 4.0847, "lng": -76.1954}


def _populate_segment_dest(s):
    segs = s["data"].setdefault("segments", [])
    if not segs:
        return
    seg = segs[-1]
    if "dest_input" in s["data"]:
        seg.setdefault("destination", {})
        seg["destination"]["address"] = s["data"].pop("dest_input")


def _init_segment(session, service_type=None):
    segs = session["data"].setdefault("segments", [])
    seg = {
        "service_type": service_type or "",
        "description": "",
        "origin": {"address": "", "lat": None, "lng": None},
        "destination": {"address": "", "lat": None, "lng": None},
        "instructions": "",
        "packageType": None,
        "whoPays": None,
        "entity": None,
        "reference": None,
    }
    segs.append(seg)
    return seg


STATES: dict[str, StateDefinition] = {
    "SELECT_PROFILE": StateDefinition(
        prompt="¿Eres un cliente final o un negocio?\n\n1. Cliente final\n2. Negocio",
        extract=extractors.number,
        validate=lambda v: v in (1, 2),
        error_message="Responde 1 para cliente final o 2 para negocio.",
        next_state=lambda s: "SELECT_MODE" if s["data"].get("profile_num") == 2 else "SELECT_SVC",
        key="profile_num",
    ),
    "SELECT_MODE": StateDefinition(
        prompt="¿Qué tipo de servicio necesitas?\n\n1. Servicio puntual\n2. Domii Fijo",
        extract=extractors.number,
        validate=lambda v: v in (1, 2),
        error_message="Responde 1 para puntual o 2 para Domii Fijo.",
        next_state=lambda s: "SELECT_SVC" if s["data"].get("mode_num") == 1 else "COLLECT_DF_NAME",
        key="mode_num",
    ),
    "SELECT_SVC": StateDefinition(
        prompt="¿Qué servicio necesitas?\n\n" + "\n".join(
            f"{i+1}. {s.capitalize()}" for i, s in enumerate(SERVICE_CHOICES)),
        extract=extractors.number,
        validate=lambda v: isinstance(v, int) and 1 <= v <= len(SERVICE_CHOICES),
        error_message=f"Elige un número del 1 al {len(SERVICE_CHOICES)}.",
        next_state=lambda s: (
            s["data"].setdefault("segments", []).append(
                {"service_type": SERVICE_CHOICES[s["data"]["svc_num"] - 1]}
            ), "ENTER_ORIGIN"
        ) or "ENTER_ORIGIN",
        key="svc_num",
    ),
    "ENTER_ORIGIN": StateDefinition(
        prompt="¿Cuál es la dirección de origen?\n(Escribe 'centro' para usar Tuluá centro)",
        extract=extractors.address,
        validate=lambda v: len(v) > 2,
        error_message="Escribe una dirección válida o 'centro'.",
        next_state=lambda s: None,
        key="origin_input",
    ),
    "ENTER_DEST": StateDefinition(
        prompt="¿Cuál es la dirección de destino?",
        extract=extractors.address,
        validate=lambda v: len(v) > 2,
        error_message="Escribe una dirección válida.",
        next_state=lambda s: (
            _populate_segment_origin(s),
            _populate_segment_dest(s),
            "ADD_MORE"
        ) or "ADD_MORE",
        key="dest_input",
    ),
    "ADD_MORE": StateDefinition(
        prompt="¿Necesitas más paradas?\n\n1. Sí, agregar otra\n2. No, continuar",
        extract=extractors.number,
        validate=lambda v: v in (1, 2),
        error_message="Responde 1 para más paradas o 2 para continuar.",
        next_state=lambda s: "ENTER_ORIGIN" if s["data"].get("add_more") == 1 else "SELECT_TOOLS",
        key="add_more",
    ),
    "SELECT_TOOLS": StateDefinition(
        prompt="¿Necesitas alguna herramienta adicional?\n\n1. Canasta\n2. Maletín\n3. Ninguna",
        extract=extractors.number,
        validate=lambda v: v in (1, 2, 3),
        error_message="Elige 1, 2 o 3.",
        next_state=lambda s: (
            s["data"].__setitem__("tools", ["canasta"] if s["data"].get("tools_num") == 1 else ["maletin"] if s["data"].get("tools_num") == 2 else []),
            "SELECT_PAY"
        ) or "SELECT_PAY",
        key="tools_num",
    ),
    "SELECT_PAY": StateDefinition(
        prompt="¿Cómo deseas pagar?\n\n1. Efectivo (sin recargo)\n2. Nequi (+$500)",
        extract=extractors.number,
        validate=lambda v: v in (1, 2),
        error_message="Elige 1 para efectivo o 2 para Nequi.",
        next_state=lambda s: (
            s["data"].__setitem__("payment_method", "efectivo" if s["data"].get("pay_num") == 1 else "nequi"),
            "ACOMPANANTE"
        ) or "ACOMPANANTE",
        key="pay_num",
    ),
    "ACOMPANANTE": StateDefinition(
        prompt="¿Llevas acompañante?\n\n1. Sí\n2. No",
        extract=extractors.number,
        validate=lambda v: v in (1, 2),
        error_message="Responde 1 o 2.",
        next_state=lambda s: (
            s["data"].__setitem__("acompanante", s["data"].get("acomp_num") == 1),
            "SHOW_PRICE"
        ) or "SHOW_PRICE",
        key="acomp_num",
    ),
    "SHOW_PRICE": StateDefinition(
        prompt="Calculando precio...",
        extract=extractors.text,
        next_state=lambda s: "CONFIRM",
    ),
    "CONFIRM": StateDefinition(
        prompt="¿Confirmas el pedido?\n\n1. Sí, confirmar\n2. No, cancelar",
        extract=extractors.number,
        validate=lambda v: v in (1, 2),
        error_message="Responde 1 para confirmar o 2 para cancelar.",
        next_state=lambda s: "COLLECT_CONTACT" if s["data"].get("confirm_num") == 1 else "DONE",
        key="confirm_num",
    ),
    "COLLECT_CONTACT": StateDefinition(
        prompt="Por favor, escribe tu nombre completo:",
        extract=extractors.text,
        validate=lambda v: len(v) > 2,
        error_message="Escribe un nombre válido.",
        next_state=lambda s: "COLLECT_PHONE",
        key="contact_name",
    ),
    "COLLECT_PHONE": StateDefinition(
        prompt="Por último, escribe tu número de teléfono:",
        extract=extractors.address,
        validate=lambda v: len(v) >= 7,
        error_message="Escribe un número de teléfono válido.",
        next_state=lambda s: "SUBMIT_ORDER",
        key="contact_phone",
    ),
    "SUBMIT_ORDER": StateDefinition(
        prompt="¡Pedido enviado con éxito! ✅\n\nUn domiciliario será asignado pronto. Te notificaremos por aquí.",
        extract=extractors.text,
        next_state=lambda s: "DONE",
    ),
    "COLLECT_DF_NAME": StateDefinition(
        prompt="¿Cuál es el nombre del negocio?",
        extract=extractors.text,
        validate=lambda v: len(v) > 1,
        error_message="Escribe un nombre válido.",
        next_state=lambda s: "COLLECT_DF_ADDR",
        key="business_name",
    ),
    "COLLECT_DF_ADDR": StateDefinition(
        prompt="¿Cuál es la dirección del negocio?",
        extract=extractors.address,
        validate=lambda v: len(v) > 2,
        error_message="Escribe una dirección válida.",
        next_state=lambda s: "COLLECT_DF_PHONE",
        key="business_address",
    ),
    "COLLECT_DF_PHONE": StateDefinition(
        prompt="¿Cuál es el teléfono del negocio?",
        extract=extractors.address,
        validate=lambda v: len(v) >= 7,
        error_message="Escribe un número de teléfono válido.",
        next_state=lambda s: "COLLECT_DF_DATE",
        key="business_phone",
    ),
    "COLLECT_DF_DATE": StateDefinition(
        prompt="¿Para qué fecha necesitas el servicio?\n(Ej: 2026-05-30, o 'hoy' para hoy)",
        extract=extractors.text,
        validate=lambda v: len(v) > 0,
        error_message="Escribe una fecha válida.",
        next_state=lambda s: "COLLECT_DF_START",
        key="service_date",
    ),
    "COLLECT_DF_START": StateDefinition(
        prompt="¿A qué hora quieres empezar?\n(Ej: 08:00, en formato 24h)",
        extract=extractors.text,
        validate=lambda v: len(v) >= 4,
        error_message="Escribe una hora válida (HH:MM).",
        next_state=lambda s: "COLLECT_DF_END",
        key="start_time",
    ),
    "COLLECT_DF_END": StateDefinition(
        prompt="¿A qué hora termina?\n(Ej: 14:00)",
        extract=extractors.text,
        validate=lambda v: len(v) >= 4,
        error_message="Escribe una hora válida (HH:MM).",
        next_state=lambda s: "COLLECT_DF_VOL",
        key="end_time",
    ),
    "COLLECT_DF_VOL": StateDefinition(
        prompt="¿Cuál es el volumen estimado de pedidos?\n\n1. 1-5 pedidos\n2. 5-15 pedidos\n3. +15 pedidos",
        extract=extractors.number,
        validate=lambda v: v in (1, 2, 3),
        error_message="Elige 1, 2 o 3.",
        next_state=lambda s: (
            s["data"].__setitem__("volume", ["1-5", "5-15", "+15"][s["data"].get("vol_num", 1) - 1]),
            "DF_CONFIRM"
        ) or "DF_CONFIRM",
        key="vol_num",
    ),
    "DF_CONFIRM": StateDefinition(
        prompt="¿Confirmas los datos?\n\n1. Sí, enviar solicitud\n2. No, cancelar",
        extract=extractors.number,
        validate=lambda v: v in (1, 2),
        error_message="Responde 1 o 2.",
        next_state=lambda s: "SUBMIT_DF" if s["data"].get("df_confirm") == 1 else "DONE",
        key="df_confirm",
    ),
    "SUBMIT_DF": StateDefinition(
        prompt="¡Solicitud de Domii Fijo enviada! ✅\n\nUn asesor te contactará para confirmar el bloque. ¡Gracias por preferir Domii!",
        extract=extractors.text,
        next_state=lambda s: "DONE",
    ),
    "DONE": StateDefinition(
        prompt="",
        extract=extractors.text,
        next_state=lambda s: None,
    ),
}


def advance(session, user_text, conversation) -> str:
    state_name = session["state"]
    sd = STATES.get(state_name)
    if not sd:
        raise FallbackRequired()

    value = sd.extract(user_text, session)
    if sd.validate and not sd.validate(value):
        return sd.error_message

    if sd.key:
        session["data"][sd.key] = value

    next_state = sd.next_state(session)
    session["state"] = next_state
    save_session(conversation.id, session)

    next_sd = STATES.get(next_state)
    if not next_sd:
        return None
    return next_sd.prompt
