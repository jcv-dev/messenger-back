import re
from .fallback import FallbackRequired
from . import faq
from api.bot.session import save_session


def welcome_message() -> str:
    return (
        "¡Bienvenido a Domii Tuluá! 🚀\n\n"
        "Soy el asistente virtual. ¿Qué deseas hacer?\n\n"
        "1. Calcular un domicilio o mensajería\n"
        "2. Domii Fijo (domiciliario dedicado)\n"
        "3. Hablar con un asesor\n"
        "4. Preguntas frecuentes\n\n"
        "Responde con el número de la opción."
    )


def _extract_number(text):
    m = re.search(r'\d+', text.strip())
    if m:
        return int(m.group())
    return None


def handle(session, user_text: str, conversation=None) -> str:
    choice = _extract_number(user_text)
    conversation_id = conversation.id if conversation else session.get("_conversation_id")

    if choice == 1:
        session["mode"] = "delivery"
        session["state"] = "SELECT_PROFILE"
        save_session(conversation_id, session)
        return "¿Eres un cliente final o un negocio?\n\n1. Cliente final\n2. Negocio"
    elif choice == 2:
        session["mode"] = "delivery"
        session["state"] = "COLLECT_DF_NAME"
        save_session(conversation_id, session)
        return "Cuéntame sobre tu negocio:\n\n¿Cuál es el nombre del negocio?"
    elif choice == 3:
        session["mode"] = "escalate"
        save_session(conversation_id, session)
        return None
    elif choice == 4:
        session["mode"] = "faq"
        session["state"] = "FAQ_MENU"
        save_session(conversation_id, session)
        return faq.menu_message()
    else:
        raise FallbackRequired()
