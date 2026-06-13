"""Runtime bot configuration from BotConfig model with fallback to settings."""

import datetime
import logging
import os
import time

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("api.bot")

_CONFIG_CACHE = {}
_CONFIG_CACHE_TTL = 30
_CONFIG_CACHE_TS = 0


def _get_config():
    """Get all BotConfig entries as a dict, cached for 30 seconds."""
    global _CONFIG_CACHE, _CONFIG_CACHE_TS
    now = time.time()
    if _CONFIG_CACHE and (now - _CONFIG_CACHE_TS) < _CONFIG_CACHE_TTL:
        return _CONFIG_CACHE
    try:
        from api.models import BotConfig
        entries = BotConfig.objects.all().values_list('key', 'value')
        _CONFIG_CACHE = dict(entries)
        _CONFIG_CACHE_TS = now
    except Exception:
        logger.exception("Failed to read BotConfig")
        _CONFIG_CACHE = {}
    return _CONFIG_CACHE


def get_config(key, default=None):
    """Read a config value, falling back to provided default."""
    cfg = _get_config()
    return cfg.get(key, default)


def get_faq_info_section():
    """Return the FAQ/info section text to inject into the system prompt."""
    return get_config(
        'faq_info_section',
        (
            'SERVICIOS: Domicilios, Mensajería, Compras por encargo, Trámites, Bancarios, '
            'Domii Fijo (domiciliario dedicado por horas/días).\n'
            f'HORARIOS: {settings.BOT_OPERATING_HOURS}.\n'
            'COBERTURA: Tuluá urbano y veredas. Fuera del área (Cali, Buga) = tarifas fijas.\n'
            'PAGO: Efectivo (sin recargo) o Nequi (+$500). Pago al recibir.'
        ),
    )


def get_outside_hours_reply():
    return get_config(
        'outside_hours_reply',
        'Gracias por escribirnos. Actualmente estamos fuera de nuestro horario de atención. '
        'Te responderemos en cuanto estemos disponibles. ¡Gracias por tu paciencia!',
    )


def get_state_machine_enabled():
    env_val = os.environ.get("BOT_STATE_MACHINE", "")
    if env_val in ("1", "true", "yes"):
        return True
    return bool(get_config('state_machine_enabled', False))


def get_llm_temperature():
    return float(get_config('llm_temperature', settings.BOT_LLM_TEMPERATURE))


def get_llm_max_tokens():
    return int(get_config('llm_max_tokens', settings.BOT_LLM_MAX_OUTPUT_TOKENS))


def get_llm_retry_count():
    return int(get_config('llm_retry_count', settings.BOT_LLM_RETRY_COUNT))


def get_tools_cache_ttl():
    return int(get_config('tools_cache_ttl', settings.BOT_TOOLS_CACHE_TTL))


def get_max_user_message_length():
    return int(get_config('max_user_message_length', settings.BOT_MAX_USER_MESSAGE_LENGTH))


def get_allowed_url_domains():
    domains = get_config('allowed_url_domains', [])
    if not isinstance(domains, list):
        domains = []
    return list(domains) + list(settings.BOT_ALLOWED_OUTPUT_URL_DOMAINS)


def is_within_operating_hours() -> bool:
    """Check if current time (UTC-05) falls within BotSchedule."""
    import pytz
    from api.models import BotSchedule

    bogota = pytz.timezone('America/Bogota')
    now_bog = timezone.now().astimezone(bogota)
    today = now_bog.date()
    current_time = now_bog.time()

    # Date overrides take priority
    override = BotSchedule.objects.filter(date=today, is_active=True).first()
    if override:
        if override.close_time is None:
            return False
        return override.open_time <= current_time <= override.close_time

    # Recurring day-of-week schedule
    schedule = BotSchedule.objects.filter(
        day_of_week=today.weekday(), is_active=True,
    ).first()
    if schedule:
        if schedule.close_time is None:
            return False
        return schedule.open_time <= current_time <= schedule.close_time

    # No schedule configured — assume open
    return True


def _clear_cache():
    global _CONFIG_CACHE
    _CONFIG_CACHE = {}
