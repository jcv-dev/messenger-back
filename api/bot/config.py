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


def get_outside_hours_reply(reason: str | None = None):
    msg = get_config(
        'outside_hours_reply',
        'Gracias por escribirnos. Actualmente estamos fuera de nuestro horario de atención. '
        'Te responderemos en cuanto estemos disponibles. ¡Gracias por tu paciencia!',
    )
    if reason:
        msg = msg.replace(
            'fuera de nuestro horario de atención.',
            f'fuera de nuestro horario de atención por: {reason}.',
        )
    return msg


def get_outside_hours_reason() -> str | None:
    """Return the label of the break/closure block we're currently inside.
    
    Checks date overrides first, then recurring schedules. Returns the label
    if the current time falls within an ``is_closed`` block that has a label.
    Returns ``None`` for simple gaps between working blocks (no explicit break).
    """
    from zoneinfo import ZoneInfo
    from api.models import BotSchedule

    bogota = ZoneInfo('America/Bogota')
    now_bog = timezone.now().astimezone(bogota)
    today = now_bog.date()
    current_time = now_bog.time()
    end_of_day = datetime.time(23, 59, 59)

    def _find_break_label(entries) -> str | None:
        for e in entries:
            if e.is_closed and e.label:
                close = e.close_time or end_of_day
                if e.open_time <= current_time <= close:
                    return e.label
        return None

    label = _find_break_label(
        BotSchedule.objects.filter(date=today, is_active=True)
    )
    if label:
        return label

    return _find_break_label(
        BotSchedule.objects.filter(day_of_week=today.weekday(), is_active=True)
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


def get_send_delay_seconds():
    return int(get_config('send_delay_seconds', 10))


def get_max_user_message_length():
    return int(get_config('max_user_message_length', settings.BOT_MAX_USER_MESSAGE_LENGTH))


def get_allowed_url_domains():
    domains = get_config('allowed_url_domains', [])
    if not isinstance(domains, list):
        domains = []
    return list(domains) + list(settings.BOT_ALLOWED_OUTPUT_URL_DOMAINS)


def is_within_operating_hours() -> bool:
    """Check if current time (UTC-05) falls within BotSchedule.
    
    Each block is evaluated individually. ``is_closed`` blocks are non-working
    (breaks/closures). Gaps between blocks are also outside hours.
    Date overrides take priority over recurring schedules.
    """
    from zoneinfo import ZoneInfo
    from api.models import BotSchedule

    bogota = ZoneInfo('America/Bogota')
    now_bog = timezone.now().astimezone(bogota)
    today = now_bog.date()
    current_time = now_bog.time()
    end_of_day = datetime.time(23, 59, 59)

    def _check(entries) -> bool | None:
        """Return True (working), False (outside), or None if no entry covers."""
        for e in entries:
            close = e.close_time or end_of_day
            if e.open_time <= current_time <= close:
                return not e.is_closed  # False if inside a break block
        return None  # no entry covers current time

    # Check date overrides; supplement recurring (don't replace it)
    overrides = list(BotSchedule.objects.filter(date=today, is_active=True))
    if overrides:
        result = _check(overrides)
        if result is not None:
            return result

    # Recurring day-of-week schedule
    schedules = list(BotSchedule.objects.filter(
        day_of_week=today.weekday(), is_active=True,
    ))
    if schedules:
        result = _check(schedules)
        if result is not None:
            return result
        return False

    # No schedule configured — assume open
    return True


def _bogota_now():
    from zoneinfo import ZoneInfo
    return timezone.now().astimezone(ZoneInfo('America/Bogota'))


def get_grouped_hours_text() -> str:
    """Return operating hours as a grouped human-readable string.

    Days sharing the same working blocks and break blocks are grouped together.
    Consecutive ranges use "a" ("Lunes a Viernes"); non-consecutive
    lists use commas and "y" ("Lunes, Miércoles y Viernes").
    Future date overrides are listed at the end.
    """
    from api.models import BotSchedule

    now_bog = _bogota_now()
    today = now_bog.date()
    end_of_day = datetime.time(23, 59, 59)

    DAYS = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo y Festivos']

    def _fmt(t):
        return t.strftime('%-I:%M %p')

    def _close_text(e):
        if e.close_time:
            return _fmt(e.close_time)
        return 'medianoche'

    # Build per-day blocks: working_blocks list + break_blocks list
    entries = list(BotSchedule.objects.filter(
        is_active=True, day_of_week__isnull=False,
    ))

    # day -> {working: [(open, close)], breaks: [(open, close, label)]}
    day_data: dict[int, dict] = {}
    for e in entries:
        d = e.day_of_week
        if d not in day_data:
            day_data[d] = {'working': [], 'breaks': []}
        if e.is_closed:
            day_data[d]['breaks'].append((e.open_time, e.close_time or end_of_day, e.label or ''))
        else:
            day_data[d]['working'].append((e.open_time, e.close_time or end_of_day))

    if not day_data:
        return ""

    # Sort blocks within each day
    for data in day_data.values():
        data['working'].sort(key=lambda x: x[0])
        data['breaks'].sort(key=lambda x: x[0])

    # Build signature: tuple of ((open, close), ...) for working + ((open, close, label), ...) for breaks
    # Group days with identical signatures
    signature_map: dict[tuple, list[int]] = {}
    for d in sorted(day_data):
        data = day_data[d]
        # Full-days closed: no working blocks, all breaks → grouped via signature
        sig = (
            tuple(data['working']),
            tuple(data['breaks']),
        )
        signature_map.setdefault(sig, []).append(d)

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
    for sig, days in sorted(signature_map.items(), key=lambda kv: kv[1][0]):
        working_blocks, break_blocks = sig
        label = _day_label(days)

        if not working_blocks and break_blocks:
            # Full-day closure
            closure_label = break_blocks[0][2] or ''
            if closure_label:
                lines.append(f"{label}: Cerrado todo el día ({closure_label})")
            else:
                lines.append(f"{label}: Cerrado todo el día")
            continue

        # Working blocks
        working_parts = []
        for o, c in working_blocks:
            if c == end_of_day:
                working_parts.append(f"desde {_fmt(o)}")
            else:
                working_parts.append(f"{_fmt(o)} a {_fmt(c)}")
        lines.append(f"{label}: {', '.join(working_parts)}")

        # Break blocks as sub-lines
        for o, c, lbl in break_blocks:
            time_part = f"{_fmt(o)} a {_fmt(c)}" if c != end_of_day else f"desde {_fmt(o)}"
            if lbl:
                lines.append(f"  Cerrado {time_part}: {lbl}")
            else:
                lines.append(f"  Cerrado {time_part}")

    # Future date overrides
    overrides = BotSchedule.objects.filter(
        is_active=True, date__isnull=False, date__gte=today,
    ).order_by('date')

    for o in overrides:
        date_label = o.date.strftime('%-d/%-m/%Y')
        tag = f" ({o.label})" if o.label else ""
        if o.is_closed:
            if o.close_time:
                lines.append(f"{date_label}{tag}: Cerrado {_fmt(o.open_time)} a {_fmt(o.close_time)}")
            else:
                lines.append(f"{date_label}{tag}: Cerrado todo el día")
        elif o.close_time:
            lines.append(f"{date_label}{tag}: {_fmt(o.open_time)} a {_fmt(o.close_time)}")
        else:
            lines.append(f"{date_label}{tag}: desde {_fmt(o.open_time)}")

    return "\n".join(lines)


def _clear_cache():
    global _CONFIG_CACHE
    _CONFIG_CACHE = {}
