import re

ACCENT_MAP = str.maketrans({
    'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u',
    'Á': 'A', 'É': 'E', 'Í': 'I', 'Ó': 'O', 'Ú': 'U',
    'ü': 'u', 'Ü': 'U', 'ñ': 'n', 'Ñ': 'N',
})


def _normalize(text: str) -> str:
    """Remove accents for matching purposes."""
    return text.translate(ACCENT_MAP)


RULES = [
    # Priority 1: Escalation (human request) — match stems
    (re.compile(r"\b(agente|humano|persona|asesor|hablar|comunicar|atencion|operador)", re.I), "escalate"),
    # Priority 2: Delivery intent
    (re.compile(r"\b(domicili|envio|llevar|entregar|mensajeria|paquete|recoger|mandar|traer|recogida)", re.I), "delivery"),
    (re.compile(r"\bdomii[\s-]?fijo|\bdomiciliario[\s-]?dedicado", re.I), "delivery"),
    (re.compile(r"\b(precio|cuanto|tarifa|costo|vale|calcular|presupuesto|cotizar)", re.I), "delivery"),
    # Priority 3: FAQ
    (re.compile(r"\b(horari|cobertura|metodo|pago|nequi|efectivo|funciona|ayuda|info)", re.I), "faq"),
    # Priority 4: Greeting / Default
    (re.compile(r"\b(hola|buen|saludos|gracias|ok)", re.I), "greeting"),
]


def classify(text: str) -> str:
    norm = _normalize(text)
    for pattern, mode in RULES:
        if pattern.search(norm):
            return mode
    return "greeting"
