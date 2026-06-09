import re
from .fallback import FallbackRequired

FAQ = [
    (re.compile(r"\b(horario|hora|abierto|atienden|abren|cierre)\b", re.I),
     "Atendemos de lunes a sábado de 8:00 AM a 8:00 PM. Domingos y festivos de 9:00 AM a 6:00 PM."),
    (re.compile(r"\b(cobertura|zona|[áa]rea|hasta d[oó]nde|llegan)\b", re.I),
     "Cubrimos todo el casco urbano de Tuluá y veredas cercanas. Para destinos fuera del área (Cali, Buga, etc.) aplican tarifas fijas."),
    (re.compile(r"\b(pago|pagar|m[eé]todo|nequi|efectivo|transferencia|bancolombia|daviplata)\b", re.I),
     "Aceptamos pago en efectivo (sin recargo) y Nequi (recargo de $500 COP). El pago se realiza al recibir el domicilio."),
    (re.compile(r"\b(c[oó]mo funciona|qu[eé] es|explicar|c[oó]mo hago)\b", re.I),
     "Es muy sencillo:\n1. Eliges el tipo de servicio (domicilio, mensajería, compras, etc.)\n2. Ingresas la dirección de origen y destino\n3. Te mostramos el precio\n4. Confirmas y enviamos un domiciliario\n\n¿Quieres calcular un domicilio ahora?"),
]


def _is_yes(text):
    return text.strip().lower() in ("si", "sí", "s", "yes", "1")


def menu_message():
    return (
        "Preguntas Frecuentes:\n\n"
        "1. Horarios de atención\n"
        "2. Cobertura\n"
        "3. Métodos de pago\n"
        "4. ¿Cómo funciona?\n\n"
        "Elige un número o escribe tu pregunta."
    )


def handle(session, user_text: str) -> str:
    if _is_yes(user_text):
        session["mode"] = "delivery"
        session["state"] = "SELECT_PROFILE"
        from api.bot.session import save_session
        save_session(session.get("_conversation_id"), session)
        return "¿Eres un cliente final o un negocio?\n\n1. Cliente final\n2. Negocio"

    for pattern, response in FAQ:
        if pattern.search(user_text):
            return response + "\n\n¿Necesitas algo más? (1. Calcular domicilio / 2. Volver al menú principal / 3. Hablar con un asesor)"

    return "No encontré respuesta a tu pregunta. ¿Quieres hablar con un asesor? (sí/no)"
