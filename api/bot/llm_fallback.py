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
        {"id": "cotizar", "label": "Cotizar domicilio / pedido / envío / calcular precio / quiero un domicilio / necesito enviar / mandar algo / domicilio / delivery / mensajería / cotizar envío / hacer domicilio"},
        {"id": "domii_fijo", "label": "Domii Fijo / domiciliario dedicado / contratar / por horas / por días / mensajero fijo / empleado / necesito un domiciliario / quiero un mensajero"},
        {"id": "faq", "label": "Preguntas frecuentes / horario / cobertura / pago / información / dudas / cómo funciona / necesito info / qué tal / cómo es / información general / consulta"},
        {"id": "escalate", "label": "Hablar con un asesor / agente humano / persona / ayuda / asesor / me atiende alguien / necesito ayuda / ayúdame / operador / atención al cliente"},
    ],
    "AWAITING_PROFILE": [
        {"id": "final", "label": "Usuario final / persona natural / cliente / cliente final / soy persona / soy usuario / soy cliente / consumidor / persona particular / usuario particular / persona común / usuario común"},
        {"id": "negocio", "label": "Negocio / empresa / restaurante / tienda / soy empresa / soy negocio / local comercial / comercio"},
    ],
    "AWAITING_SERVICE_TYPE": [
        {"id": "domicilios", "label": "Domicilios / comida / restaurante / envío / delivery / llevar / mandar / domicilio / domicilio de comida / envío de comida"},
        {"id": "mensajeria", "label": "Mensajería / paquete / documento / envío de documentos / enviar documento / mensajero / sobre"},
        {"id": "purchases", "label": "Compras por encargo / mercado / supermercado / que compren / me traigan / mercadito / hacer mercado / víveres / compras / mandado"},
        {"id": "tramites", "label": "Trámites / favores / diligencias / vuelta / hacer una vuelta / encargo / hacer un trámite / papeles"},
        {"id": "bancarios", "label": "Bancarios / banco / bancolombia / nequi / pagar factura / ir al banco / consignar / depositar / pago de servicios / recarga"},
    ],
    "ASK_PACKAGE_TYPE": [
        {"id": "documento", "label": "Documento / carta / sobre / papeles / documentos / folder / carpeta"},
        {"id": "paquete", "label": "Paquete / caja / bulto / cajita / paquetico / encargo / ropa / mercancía / repuestos / producto"},
        {"id": "fragil", "label": "Frágil / vidrio / delicado / rompible / cristal / loza / cerámica / electrónico"},
        {"id": "alimento", "label": "Alimento / comida / bebida / helado / almuerzo / comida preparada / mercado / víveres / fresco"},
        {"id": "otro", "label": "Otro / no sé / diferente / varias cosas / no estoy seguro"},
    ],
    "ASK_WHO_PAYS": [
        {"id": "remitente", "label": "Remitente / yo / quien envía / lo pago yo / yo pago / pago yo / el que manda / quien manda / yo mismo"},
        {"id": "destinatario", "label": "Destinatario / el que recibe / la otra persona / quien recibe / paga él / lo paga el / el que lo recibe"},
    ],
    "CONFIRMING_ORIGIN": [
        {"id": "yes", "label": "Sí / correcto / bien / sí es / ok / dale / confirmo / está bien / así es / esa es / sí señor / sí esa es / correcta / confirmar"},
        {"id": "no", "label": "No / incorrecto / mal / otra / error / no es / no es esa / esa no / equivocada / cambiar / diferente"},
    ],
    "CONFIRMING_DEST": [
        {"id": "yes", "label": "Sí / correcto / bien / sí es / ok / dale / confirmo / está bien / así es / esa es / sí señor / sí esa es / correcta / confirmar"},
        {"id": "no", "label": "No / incorrecto / mal / otra / error / no es / no es esa / esa no / equivocada / cambiar / diferente"},
    ],
    "AWAITING_MORE_STOPS": [
        {"id": "yes", "label": "Sí / más / otra parada / agregar / otro / otra vuelta / más paradas / sí otra / siguiente / adicional"},
        {"id": "no", "label": "No / así está bien / continuar / seguir / no más / listo / eso es todo / finalizar / terminar / no gracias / así quedo"},
    ],
    "AWAITING_TOOLS": [
        {"id": "none", "label": "Ninguna / no / sin herramientas / nada / no necesito / no gracias / sin nada"},
        {"id": "done", "label": "Listo / continuar / ya terminé / eso es todo / seguir / finalizar / listo con herramientas / continuar con el pedido"},
        # Dynamic tool IDs are added per-call
    ],
    "AWAITING_PAYMENT": [
        {"id": "efectivo", "label": "Efectivo / cash / billete / plata en mano / pago en efectivo / pago en persona / contado / en físico / cancelar en efectivo"},
        {"id": "nequi", "label": "Nequi / neki / transacción / app / transferencia / pagar con nequi / transferencia bancaria / pago digital / billetera digital / cancelar con nequi"},
    ],
    "AWAITING_ACOMPANANTE": [
        {"id": "yes", "label": "Sí / acompañante / ayuda / se necesita / necesito ayuda / sí acompañante / con acompañante / apoyo"},
        {"id": "no", "label": "No / sin acompañante / solo / nada / no necesito / sin ayuda / voy yo solo"},
    ],
    "CONFIRMING_QUOTE": [
        {"id": "confirm", "label": "Confirmar / sí / dale / enviar / hacer pedido / ok / confirmo / adelante / hágale / envíalo / proceder / listo / cancelar / cancelar pedido / pagar"},
        {"id": "change", "label": "Cambiar / modificar / editar / diferente / otro / corregir / ajustar / cambiar algo / modificar datos / arreglar"},
        {"id": "cancel", "label": "Cancelar / no / cancel / parar / detener / salir / cancelar pedido / no quiero / mejor no / descartar"},
    ],
    "CONFIRMING_FIJO": [
        {"id": "confirm", "label": "Confirmar / sí / enviar / ok / dale / hágale / adelante / listo / confirmo"},
        {"id": "cancel", "label": "Cancelar / no / cancel / salir / detener / no quiero / mejor no / descartar"},
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


async def generate_escalation_summary(
    state: str,
    profile: str | None = None,
    service: str | None = None,
    reason_type: str = "error",
) -> str | None:
    """Ask Gemini to write a one-line escalation summary.

    Args:
        state: The bot state where escalation happened.
        profile: User profile (usuario_final / negocio) if known.
        service: Service type label (Domicilios / Mensajería / etc) if known.
        reason_type: Short description of why escalation triggered.

    Returns:
        A one-line summary, or ``None`` if the LLM call fails.
    """
    client = _get_client()
    if client is None:
        return None

    parts = [p for p in [profile, service] if p]
    context = f" ({' · '.join(parts)})" if parts else ""

    prompt = (
        "Escribe UNA línea que resuma por qué se escala este chat a un asesor humano.\n"
        f"Contexto: estado {state}{context}\n"
        f"Motivo: {reason_type}\n\n"
        "Ejemplo: 'El cliente estaba confundido al inicio de la conversación tras varios intentos.'\n"
        "Responde solo la línea, sin saludos ni introducciones."
    )

    config = genai_types.GenerateContentConfig(
        system_instruction=prompt,
        temperature=0.0,
        max_output_tokens=64,
    )

    try:
        response = await client.aio.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=[genai_types.Content(
                role="user",
                parts=[genai_types.Part.from_text(text=prompt)],
            )],
            config=config,
        )

        if not response.candidates or not response.candidates[0].content.parts:
            return None

        raw = response.candidates[0].content.parts[0].text or ""
        return raw.strip().strip("\"'").strip()[:200]

    except Exception:
        logger.warning("Gemini escalation summary failed for state=%s", state)
        return None
