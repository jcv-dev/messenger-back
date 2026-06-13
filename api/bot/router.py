"""Pre-LLM message router — handles greetings, thanks, and FAQ without the LLM.

Sits between input sanitization and the LLM call in handle_inbound().
Returns a pre-canned reply when the message matches a known pattern,
avoiding an LLM call entirely.
"""

from __future__ import annotations

import re
from typing import Callable


def _build_hours_response() -> str:
    """Build the hours FAQ response from BotSchedule."""
    from api.models import BotSchedule
    rows = BotSchedule.objects.filter(is_active=True).order_by('day_of_week')
    if not rows.exists():
        return "Nuestro horario de atención:\n- No configurado."
    lines = ["Nuestro horario de atención:"]
    days = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo y Festivos']
    for r in rows:
        if r.day_of_week is not None:
            if r.close_time:
                lines.append(f"- {days[r.day_of_week]}: {r.open_time.strftime('%-I:%M %p')} a {r.close_time.strftime('%-I:%M %p')}")
            else:
                lines.append(f"- {days[r.day_of_week]}: Cerrado")
        elif r.date:
            if r.close_time:
                lines.append(f"- {r.date.strftime('%-d/%-m/%Y')} ({r.label}): {r.open_time.strftime('%-I:%M %p')} a {r.close_time.strftime('%-I:%M %p')}")
            else:
                lines.append(f"- {r.date.strftime('%-d/%-m/%Y')} ({r.label}): Cerrado")
    return "\n".join(lines)

PAYMENT_RESPONSE = (
    "Aceptamos:\n"
    "- *Efectivo* (sin recargo)\n"
    "- *Nequi* (+$500 COP de recargo)\n"
    "El pago se realiza al recibir el domicilio."
)

COVERAGE_RESPONSE = (
    "Cubrimos el casco urbano de Tulu\u00e1 y el Valle del Cauca. "
    "Para destinos fuera del \u00e1rea (Cali, Buga, etc.) aplican tarifas fijas."
)

SERVICES_RESPONSE = (
    "Ofrecemos:\n"
    "\u2022 Domicilios\n"
    "\u2022 Mensajer\u00eda\n"
    "\u2022 Compras por encargo\n"
    "\u2022 Tr\u00e1mites\n"
    "\u2022 Bancarios\n"
    "\u2022 Domii Fijo (domiciliario dedicado)"
)

DOMII_FIJO_RESPONSE = (
    "Domii Fijo es un domiciliario dedicado por horas o d\u00edas, ideal para "
    "negocios. Incluye un domiciliario exclusivo para tus env\u00edos durante "
    "el tiempo contratado. \u00bfTe interesa cotizar uno?"
)

# FAQ patterns — ordered list, first match wins.  Each pattern is narrow /
# question-phrased to avoid false matches on flow messages like "nequi" or
# "domicilios".
FAQ_PATTERNS: list = [
    (
        re.compile(
            r"\b(horario|cual\s+es\s+(el\s+)?horario|a\s+que\s+hora\s+(abren|cierran|atienden)|"
            r"horas?\s+de\s+atenci[o\u00f3]n)\b",
            re.IGNORECASE,
        ),
        _build_hours_response,
    ),
    (
        re.compile(
            r"\b(como\s+(puedo|se\s+puede)\s+pagar|m[e\u00e9]todos?\s+de\s+pago|"
            r"formas?\s+de\s+pago|aceptan\s+(nequi|efectivo|tarjeta)|"
            r"con\s+que\s+(puedo|se\s+puede)\s+pagar)\b",
            re.IGNORECASE,
        ),
        PAYMENT_RESPONSE,
    ),
    (
        re.compile(
            r"\b(d[o\u00f3]nde\s+(entregan|hacen\s+domicilios?|llegan|cubren)|"
            r"hasta\s+d[o\u00f3]nde\s+(llegan|entregan|cubren)|"
            r"zonas?\s+de\s+cobertura|tienen\s+cobertura)\b",
            re.IGNORECASE,
        ),
        COVERAGE_RESPONSE,
    ),
    (
        re.compile(
            r"\b(qu[e\u00e9]\s+servicios?\s+(tienen|ofrecen|manejan|hay)|"
            r"cu[\u00e1a]les\s+(son\s+)?(los\s+)?servicios)\b",
            re.IGNORECASE,
        ),
        SERVICES_RESPONSE,
    ),
    (
        re.compile(
            r"\b(qu[e\u00e9]\s+es\s+(domii\s*fijo|domiciliario\s*dedicado))\b",
            re.IGNORECASE,
        ),
        DOMII_FIJO_RESPONSE,
    ),
]

# ── Greetings ───────────────────────────────────────────────────────────────

GREETING_PATTERN = re.compile(
    r"^(hola|holi|buenos?\s*d[i\u00ed]as|buenas?\s*tardes|buenas?\s*noches|buenas|hey|alo|al[\u00f3o])\s*[.!]*\s*$",
    re.IGNORECASE,
)

GREETING_REPLY = (
    "\u00a1Hola! Soy el asistente virtual de Domii Tulu\u00e1. \U0001f680\n\n"
    "\u00bfQu\u00e9 deseas hacer?\n\n"
    "1. Cotizar un domicilio o mensajer\u00eda\n"
    "2. Domii Fijo (domiciliario dedicado)\n"
    "3. Hablar con un asesor\n"
    "4. Preguntas frecuentes"
)

# ── Thanks / acknowledgments ────────────────────────────────────────────────

THANKS_PATTERNS: frozenset[str] = frozenset({
    "gracias", "muchas gracias", "mil gracias",
    "ok", "ok gracias", "vale", "vale gracias",
    "genial", "genial gracias", "perfecto", "perfecto gracias",
    "listo", "entendido", "de acuerdo", "dale", "dale gracias",
})

THANKS_REPLY = "\u00a1De nada! \u00bfHay algo m\u00e1s en lo que pueda ayudarte?"

# ── Router ──────────────────────────────────────────────────────────────────


def try_route_message(session: dict, text: str) -> str | None:
    """Try to handle a user message without calling the LLM.

    Returns the reply string if routed, ``None`` if the LLM should handle it.
    """
    if not text or not text.strip():
        return None

    clean = text.strip()

    # 1. Thanks / acknowledgments — always safe, any point in conversation
    if clean.lower() in THANKS_PATTERNS:
        return THANKS_REPLY

    # 2. Greetings — only when session is fresh (≤ 2 history entries)
    history_len = len(session.get("history", []))
    if history_len <= 2 and GREETING_PATTERN.match(clean):
        return GREETING_REPLY

    # 3. FAQ — only for short messages; long ones are likely flow-specific
    if len(clean) <= 100:
        for pattern, response in FAQ_PATTERNS:
            if pattern.search(clean):
                return response() if callable(response) else response

    return None
