class FallbackRequired(Exception):
    """Raised in a handler to signal the dispatcher should increment fallback."""


def message(session) -> str:
    count = session.get("fallback_count", 0)
    remaining = 3 - count
    if remaining > 0:
        return (
            f"No entendí tu mensaje. (Quedan {remaining} intento(s))\n\n"
            "Responde con el número de la opción que deseas, o escribe 'menú' para volver al inicio."
        )
    return None
