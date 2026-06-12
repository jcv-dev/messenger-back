"""Gemini-powered test input generator for bot state machine.

Generates diverse, natural-language WhatsApp messages for each bot state.
Run from the backend directory::

    python api/tests/generate_flow_inputs.py

Output: ``api/tests/test_flow_data/*.json`` — one file per state, each
containing a JSON array of 30 diverse user messages.

Requires GEMINI_API_KEY in .env or environment.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    print("google-genai not installed. Run: pip install google-genai")
    sys.exit(1)

# Paths
THIS_DIR = Path(__file__).parent
OUTPUT_DIR = THIS_DIR / "test_flow_data"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── State descriptions for the prompt ──────────────────────────────────────
# Each entry: state_name -> description of what the bot is asking the user

STATE_DESCRIPTIONS: dict[str, str] = {
    "WELCOME": "Bot just started. Showing main menu: Cotizar domicilio, Domii Fijo, Preguntas frecuentes, Hablar con asesor",
    "AWAITING_PROFILE": "Bot asks: 'Eres usuario final o negocio?' with buttons for 'Usuario final' or 'Negocio'",
    "AWAITING_SERVICE_TYPE": "Bot shows service list: Domicilios, Mensajería, Compras por encargo, Trámites, Bancarios",
    "AWAITING_ORIGIN": "Bot asks for the pickup/origin address. For some services it's optional ('escribe no para omitir')",
    "CONFIRMING_ORIGIN": "Bot shows a location it found and asks '¿Es correcta?' with Sí/No buttons",
    "AWAITING_DESTINATION": "Bot asks for the delivery/destination address. For some services it's optional",
    "CONFIRMING_DEST": "Bot shows the destination location and asks '¿Es correcta?' with Sí/No buttons",
    "AWAITING_SEGMENT_DESCRIPTION": "Bot asks what the user is sending/purchasing. Depends on service type (paquete, compra, trámite)",
    "AWAITING_SEGMENT_INSTRUCTIONS": "Bot asks for special delivery instructions. User can say 'no' to skip",
    "AWAITING_MORE_STOPS": "Bot asks if user needs more stops with Sí/No buttons. Can also specify a different service type",
    "AWAITING_TOOLS": "Bot shows list of additional tools (canasta, maletín térmico, etc.) and asks if needed",
    "AWAITING_PAYMENT": "Bot asks payment method: Efectivo or Nequi (+$500)",
    "AWAITING_ACOMPANANTE": "Bot asks if user needs an assistant for heavy items (Sí/No)",
    "CONFIRMING_QUOTE": "Bot shows price summary and asks Confirmar/Cambiar algo/Cancelar",
    "AWAITING_RECIPIENT_NAME": "Bot asks for the recipient's name",
    "AWAITING_RECIPIENT_PHONE": "Bot asks for the recipient's phone number (10+ digits)",
    "ASK_BANCARIOS_ENTITY": "Bot asks which bank entity for the bancario trámite",
    "ASK_BANCARIOS_REFERENCE": "Bot asks for the reference number or invoice for the bancario trámite",
}

# ── Few-shot examples per state (optional, helps Gemini) ────────────────────

EXAMPLES: dict[str, list[str]] = {
    "WELCOME": [
        "Hola", "Cotizar domicilio", "Quiero un domicilio", "Domii Fijo", "Preguntas frecuentes",
        "hablar con un asesor", "Ayuda", "Buenas, necesito un envío", "Hola, me ayudas?",
    ],
    "AWAITING_ORIGIN": [
        "Cra 1 #2-3, Tuluá", "Calle 10 #5-30", "Enviar desde mi casa",
        "no", "omitir", "Ninguna, no aplica",
        "cll 5 # 10 20 tulua", "La dirección es carrera 5 numero 10-20",
    ],
    "AWAITING_DESTINATION": [
        "Cra 10 #20-30, Tuluá", "Al parque", "Centro de Tuluá",
        "no", "omitir",
    ],
}

# ── Prompt building ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
Eres un generador de datos de prueba para un bot de WhatsApp colombiano.
Generas mensajes REALISTAS que un usuario colombiano podría enviar al bot.

Reglas importantes:
- Mensajes en español colombiano, con jerga y modismos colombianos
- Incluye: typos, faltas de ortografía, abreviaciones, errores de dedo
- Mensajes cortos (1-3 palabras) y largos (oraciones completas)
- También incluye respuestas correctas (según la opción que espera el bot)
- Incluye: usuarios confundidos, impacientes, educados, que se desvían del tema
- También frases que NO son respuestas válidas (fuera de tema, groserías, cosas graciosas)
- NO uses emojis que no sean comunes en WhatsApp colombiano
- NO generes respuestas del bot, solo lo que el USUARIO escribe

Formato: JSON array de strings, cada string es un mensaje de usuario.
""".strip()


def build_prompt(state_name: str, description: str, examples: list[str]) -> str:
    prompt = f"## Estado: {state_name}\n\n{description}\n\n"
    if examples:
        prompt += "Ejemplos del tipo de mensajes que el bot espera:\n"
        for ex in examples:
            prompt += f"- \"{ex}\"\n"
        prompt += "\n"
    prompt += (
        "Genera 30 mensajes diversos que un usuario colombiano real escribiría "
        "en esta situación. Incluye respuestas correctas, incorrectas, typos, "
        "jerga, y casos borde.\n\n"
        "Devuelve SOLO el JSON array, sin explicaciones ni marcas de código."
    )
    return prompt


# ── Gemini API call ────────────────────────────────────────────────────────

def generate_inputs(state_name: str, description: str, examples: list[str]) -> list[str]:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: Set GEMINI_API_KEY environment variable")
        return []

    client = genai.Client(api_key=api_key)
    prompt = build_prompt(state_name, description, examples)

    config = genai_types.GenerateContentConfig(
        temperature=0.7,
        max_output_tokens=4096,
    )

    response = client.models.generate_content(
        model="gemini-3.1-flash-lite",
        contents=[
            genai_types.Content(role="user", parts=[genai_types.Part.from_text(
                text=f"{SYSTEM_PROMPT}\n\n{prompt}"
            )]),
        ],
        config=config,
    )

    text = response.text.strip()
    # Remove code fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"  WARNING: Could not parse response for {state_name}, saving raw")
        print(f"  Raw response:\n{text[:500]}")
        return []


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    if not OUTPUT_DIR.exists():
        OUTPUT_DIR.mkdir(parents=True)

    states_to_generate = list(STATE_DESCRIPTIONS.items())

    print(f"Generating inputs for {len(states_to_generate)} states...")
    print(f"Output: {OUTPUT_DIR}/")

    for state_name, description in states_to_generate:
        examples = EXAMPLES.get(state_name, [])
        print(f"\n[{state_name}] {description[:60]}...")

        inputs = generate_inputs(state_name, description, examples)

        if inputs:
            output_path = OUTPUT_DIR / f"{state_name}.json"
            output_path.write_text(json.dumps(inputs, ensure_ascii=False, indent=2))
            print(f"  -> {len(inputs)} inputs saved to {output_path.name}")
        else:
            print(f"  -> SKIPPED (no inputs generated)")

    print("\nDone!")
    print(f"\nTo use the generated inputs in tests, run your test suite:")
    print(f"    python manage.py test")


if __name__ == "__main__":
    main()
