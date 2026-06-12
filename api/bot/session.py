import json
import time
from django.conf import settings
from api.redis_client import get_sync_redis

SESSION_TTL = 600  # 10 minutes (exceeds the 5-minute bot take)
REDIS_KEY = "bot:session"
HISTORY_MAX_STORED = 20  # keep 2x what the LLM uses


def get_session(conversation_id):
    r = get_sync_redis()
    raw = r.hgetall(f"{REDIS_KEY}:{conversation_id}")
    if not raw:
        return None
    def _maybe_decode(val):
        return val.decode() if isinstance(val, bytes) else val
    def _decode(val):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val
    session = {
        _maybe_decode(k): _decode(_maybe_decode(v))
        for k, v in raw.items()
    }
    if "fallback_count" in session:
        session["fallback_count"] = int(session["fallback_count"])
    if "last_activity" in session:
        session["last_activity"] = float(session["last_activity"])
    return session


def save_session(conversation_id, session):
    r = get_sync_redis()
    # Truncate history before storing to avoid unbounded Redis growth
    history = session.get("history", [])
    if len(history) > HISTORY_MAX_STORED:
        session["history"] = history[-HISTORY_MAX_STORED:]

    session["last_activity"] = time.time()

    mapping = {}
    for key, value in session.items():
        if isinstance(value, (dict, list)):
            mapping[key] = json.dumps(value)
        elif isinstance(value, (int, float)):
            mapping[key] = str(value)
        else:
            mapping[key] = value
    r.hset(f"{REDIS_KEY}:{conversation_id}", mapping=mapping)
    r.expire(f"{REDIS_KEY}:{conversation_id}", SESSION_TTL)


def delete_session(conversation_id):
    r = get_sync_redis()
    r.delete(f"{REDIS_KEY}:{conversation_id}")


# ---------------------------------------------------------------------------
#  State tracking — extract known data from user messages and build a
#  structured summary that gets injected into the LLM's system prompt so
#  it doesn't have to re-infer state from the full conversation history.
# ---------------------------------------------------------------------------

COLLECTABLE_FIELDS = {
    "profile": ["usuario_final", "usuario final", "final", "negocio"],
    "service_type": [
        "domicilios", "mensajeria", "mensajer\u00eda",
        "purchases", "compras", "tramites", "tr\u00e1mites", "bancarios",
    ],
    "payment_method": ["efectivo", "nequi"],
}

ACOMPANANTE_YES = ["s\u00ed", "si", "con acompa\u00f1ante", "con acompanante", "requiere acompa\u00f1ante"]
ACOMPANANTE_NO = ["no", "sin acompa\u00f1ante", "sin acompanante"]


def extract_state_from_turn(session, user_text):
    text_lower = user_text.lower().strip()
    if not text_lower:
        return
    collected = dict(session.get("data", {}).get("collected", {}))
    changed = False

    for field, values in COLLECTABLE_FIELDS.items():
        if field not in collected:
            for val in values:
                if val in text_lower:
                    collected[field] = val
                    changed = True
                    break

    if "acompanante" not in collected:
        for val in ACOMPANANTE_YES:
            if val in text_lower:
                collected["acompanante"] = True
                changed = True
                break
        if "acompanante" not in collected:
            for val in ACOMPANANTE_NO:
                if val in text_lower:
                    collected["acompanante"] = False
                    changed = True
                    break

    if changed:
        session.setdefault("data", {})
        session["data"]["collected"] = collected


_FIELD_LABELS = {
    "profile": "Perfil",
    "service_type": "Tipo de servicio",
    "payment_method": "M\u00e9todo de pago",
    "acompanante": "Acompa\u00f1ante",
}


def build_state_summary(session):
    collected = session.get("data", {}).get("collected", {})
    if not collected:
        return ""

    lines = ["ESTADO ACTUAL DE LA CONVERSACI\u00d3N:"]
    lines.append("Datos ya recopilados por ti (NO preguntes de nuevo):")

    for field, label in _FIELD_LABELS.items():
        if field in collected:
            val = collected[field]
            if isinstance(val, bool):
                val = "S\u00ed" if val else "No"
            lines.append(f"- {label}: {val} \u2713")

    geocoded = collected.get("geocoded_addresses", [])
    if geocoded:
        lines.append("")
        lines.append("Direcciones ya consultadas (NO preguntes de nuevo):")
        for i, addr in enumerate(geocoded, 1):
            label = "Origen" if i == 1 else f"Destino {i-1}" if i == 2 else f"Parada {i}"
            lines.append(f"- {label}: {addr} \u2713")

    lines.append("")
    lines.append("Contin\u00faa con el siguiente paso que falte. "
                 "NO preguntes por datos que ya tienen \u2713.")

    return "\n".join(lines)


def create_session(conversation_id, mode="greeting"):
    session = {
        "state": "WELCOME",
        "mode": mode,
        "data": {},
        "history": [],
        "fallback_count": 0,
    }
    save_session(conversation_id, session)
    return session
