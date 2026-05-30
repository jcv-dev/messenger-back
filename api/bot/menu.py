def numbered(options: list[str]) -> str:
    return "\n".join(f"{i+1}. {opt}" for i, opt in enumerate(options))


def bold(text: str) -> str:
    return f"*{text}*"


def instruction(text: str) -> str:
    return f"\n\n{text}"
