"""Minimal LLM fallback classifier for the state machine bot.

Called only when the user sends free text instead of tapping a button.
Classifies the user's intent into one of the known button IDs for the
current state.  Tiny prompt → tiny response (1-3 tokens) → fast + cheap.

Graceful degradation: if Gemini is unavailable or returns junk, the
caller treats the result as ``None`` and increments the fallback counter.
"""

from __future__ import annotations

import json
import logging

from django.conf import settings

from google import genai
from google.genai import types as genai_types
from google.genai import errors as genai_errors

logger = logging.getLogger("api.bot.llm_fallback")

_client = None


# ── State → valid option IDs ──────────────────────────────────────────────

_STATE_OPTIONS: dict[str, list[dict]] = {
    "WELCOME": [
        {"id": "cotizar", "label": "Cotizar domicilio / pedido"},
        {"id": "domii_fijo", "label": "Domii Fijo / domiciliario dedicado"},
        {"id": "faq", "label": "Preguntas frecuentes / horario / cobertura / pago"},
        {"id": "escalate", "label": "Hablar con un asesor / agente humano"},
    ],
    "AWAITING_PROFILE": [
        {"id": "final", "label": "Usuario final / persona natural / cliente"},
        {"id": "negocio", "label": "Negocio / empresa / restaurante / tienda"},
    ],
    "AWAITING_SERVICE_TYPE": [
        {"id": "domicilios", "label": "Domicilios / comida / restaurante / envío"},
        {"id": "mensajeria", "label": "Mensajería / paquete / documento / envío de documentos"},
        {"id": "purchases", "label": "Compras por encargo / mercado / supermercado / que compren / me traigan"},
        {"id": "tramites", "label": "Trámites / favores / diligencias"},
        {"id": "bancarios", "label": "Bancarios / banco / bancolombia / nequi / pagar factura"},
    ],
    "ASK_PACKAGE_TYPE": [
        {"id": "documento", "label": "Documento / carta / sobre"},
        {"id": "paquete", "label": "Paquete / caja / bulto"},
        {"id": "fragil", "label": "Frágil / vidrio / delicado / rompible"},
        {"id": "alimento", "label": "Alimento / comida / bebida / helado / almuerzo"},
        {"id": "otro", "label": "Otro / no sé / diferente"},
    ],
    "ASK_WHO_PAYS": [
        {"id": "remitente", "label": "Remitente / yo / quien envía"},
        {"id": "destinatario", "label": "Destinatario / el que recibe / la otra persona"},
    ],
    "CONFIRMING_ORIGIN": [
        {"id": "yes", "label": "Sí / correcto / bien / sí es / ok / dale / confirmo"},
        {"id": "no", "label": "No / incorrecto / mal / otra / error / no es"},
    ],
    "CONFIRMING_DEST": [
        {"id": "yes", "label": "Sí / correcto / bien / sí es / ok / dale / confirmo"},
        {"id": "no", "label": "No / incorrecto / mal / otra / error / no es"},
    ],
    "AWAITING_MORE_STOPS": [
        {"id": "yes", "label": "Sí / más / otra parada / agregar / otro"},
        {"id": "no", "label": "No / así está bien / continuar / seguir / no más / listo"},
    ],
    "AWAITING_TOOLS": [
        {"id": "none", "label": "Ninguna / no / sin herramientas / nada"},
        # Dynamic tool IDs are added per-call
    ],
    "AWAITING_PAYMENT": [
        {"id": "efectivo", "label": "Efectivo / cash / billete / plata en mano"},
        {"id": "nequi", "label": "Nequi / neki / transacción / app / transferencia"},
    ],
    "AWAITING_ACOMPANANTE": [
        {"id": "yes", "label": "Sí / acompañante / ayuda / se necesita"},
        {"id": "no", "label": "No / sin acompañante / solo / nada"},
    ],
    "CONFIRMING_QUOTE": [
        {"id": "confirm", "label": "Confirmar / sí / dale / enviar / hacer pedido / ok"},
        {"id": "change", "label": "Cambiar / modificar / editar / diferente / otro"},
        {"id": "cancel", "label": "Cancelar / no / cancel / parar / detener / salir"},
    ],
    "CONFIRMING_FIJO": [
        {"id": "confirm", "label": "Confirmar / sí / enviar / ok / dale"},
        {"id": "cancel", "label": "Cancelar / no / cancel / salir / detener"},
    ],
}


def _get_options_for_state(state: str) -> list[dict]:
    return _STATE_OPTIONS.get(state, [{"id": "unknown", "label": "Ninguna"}])


def _get_client():
    global _client
    if _client is None:
        key = settings.GEMINI_API_KEY
        if not key:
            logger.warning("GEMINI_API_KEY not configured — LLM fallback disabled")
            return None
        _client = genai.Client(api_key=key)
    return _client


async def classify_free_text(text: str, state: str,
                              dynamic_options: list[dict] | None = None) -> str | None:
    """Classify user's free text into a button ID for the given state.

    Args:
        text: The user's message.
        state: Current state name.
        dynamic_options: Additional per-call options (e.g. tool IDs).

    Returns:
        A matching option ID, or ``None`` if no match.
    """
    client = _get_client()
    if client is None:
        return None

    options = list(_get_options_for_state(state))
    if dynamic_options:
        for opt in dynamic_options:
            if opt not in options:
                options.append(opt)

    options_text = "; ".join(f'{o["id"]}: {o["label"]}' for o in options)

    system_prompt = (
        "Clasifica el mensaje del usuario en una de las opciones listadas.\n"
        f"Estado actual: {state}\n"
        f"Opciones: {options_text}\n"
        "Responde SOLO con el ID de la opción más adecuada.\n"
        "Si el mensaje no corresponde a ninguna opción, responde 'none'.\n"
        "NO des explicaciones ni respondas otra cosa."
    )

    config = genai_types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=0.0,
        max_output_tokens=16,
    )

    try:
        response = await client.aio.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=[genai_types.Content(
                role="user",
                parts=[genai_types.Part.from_text(text=text)],
            )],
            config=config,
        )

        if not response.candidates or not response.candidates[0].content.parts:
            return None

        raw = response.candidates[0].content.parts[0].text or ""
        result = raw.strip().lower().rstrip(".").rstrip(",").strip()

        # Validate against known IDs
        valid_ids = {o["id"] for o in options} | {"none"}
        if result in valid_ids:
            return result

        # Maybe it's embedded in backticks or quotes
        for prefix in ("id:", "option:", "`", "'", '"'):
            if result.startswith(prefix):
                result = result[len(prefix):].strip().rstrip("`").strip()
                if result in valid_ids:
                    return result

        logger.debug("LLM returned junk for state=%s: %r", state, raw)
        return None

    except genai_errors.APIError as e:
        logger.warning("Gemini API error in LLM fallback: %s", e)
        return None
    except (ConnectionError, TimeoutError) as e:
        logger.warning("Gemini connection error in LLM fallback: %s", e)
        return None
    except Exception:
        logger.exception("Unexpected error in LLM fallback")
        return None
