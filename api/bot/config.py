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


def is_bot_enabled():
    env_val = os.environ.get("BOT_ENABLED", "").lower()
    if env_val in ("0", "false", "no"):
        return False
    if env_val in ("1", "true", "yes"):
        return True
    return bool(get_config('bot_enabled', True))


def get_state_machine_enabled():
    env_val = os.environ.get("BOT_STATE_MACHINE", "")
    if env_val in ("1", "true", "yes"):
        return True
    return bool(get_config('state_machine_enabled', False))


def get_testing_warning_enabled():
    if getattr(settings, 'BOT_TESTING_WARNING', False):
        return True
    return bool(get_config('testing_warning_enabled', False))


def get_escalate_orders_enabled():
    if getattr(settings, 'BOT_ESCALATE_ORDERS', False):
        return True
    return bool(get_config('escalate_orders_enabled', False))


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
    from zoneinfo import ZoneInfo
    from api.models import BotSchedule

    bogota = ZoneInfo('America/Bogota')
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


def _bogota_now():
    from zoneinfo import ZoneInfo
    return timezone.now().astimezone(ZoneInfo('America/Bogota'))


def get_grouped_hours_text() -> str:
    """Return operating hours as a grouped human-readable string.

    Days sharing the same open/close time are grouped together.
    Consecutive ranges use "a" ("Lunes a Viernes"); non-consecutive
    lists use commas and "y" ("Lunes, Miércoles y Viernes").
    Future date overrides are listed at the end.
    """
    from api.models import BotSchedule

    now_bog = _bogota_now()
    today = now_bog.date()

    DAYS = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo y Festivos']

    reg_rows = list(BotSchedule.objects.filter(
        is_active=True, day_of_week__isnull=False,
    ).order_by('day_of_week'))

    if not reg_rows:
        return ""

    def _fmt(t):
        return t.strftime('%-I:%M %p')

    # Group by schedule (open/close pair)
    schedule_map: dict[tuple, list[int]] = {}
    for r in reg_rows:
        sk = (r.open_time, r.close_time)
        schedule_map.setdefault(sk, []).append(r.day_of_week)

    for days in schedule_map.values():
        days.sort()

    def _day_label(days: list[int]) -> str:
        if days == [0, 1, 2, 3, 4, 5, 6]:
            return "Todos los días"
        is_range = len(days) > 1 and all(days[i] == days[0] + i for i in range(len(days)))
        if is_range:
            return f"{DAYS[days[0]]} a {DAYS[days[-1]]}"
        labels = [DAYS[d] for d in days]
        if len(labels) == 1:
            return labels[0]
        if len(labels) == 2:
            return f"{labels[0]} y {labels[1]}"
        return ", ".join(labels[:-1]) + f" y {labels[-1]}"

    lines = []
    for (open_time, close_time), days in sorted(
        schedule_map.items(), key=lambda kv: kv[1][0],
    ):
        label = _day_label(days)
        if close_time is None:
            lines.append(f"{label}: Cerrado")
        else:
            lines.append(f"{label}: {_fmt(open_time)} a {_fmt(close_time)}")

    overrides = BotSchedule.objects.filter(
        is_active=True, date__isnull=False, date__gte=today,
    ).order_by('date')

    for o in overrides:
        date_label = o.date.strftime('%-d/%-m/%Y')
        tag = f" ({o.label})" if o.label else ""
        if o.close_time:
            lines.append(f"{date_label}{tag}: {_fmt(o.open_time)} a {_fmt(o.close_time)}")
        else:
            lines.append(f"{date_label}{tag}: Cerrado")

    return "\n".join(lines)


def _clear_cache():
    global _CONFIG_CACHE
    _CONFIG_CACHE = {}
