"""Phone normalization between WhatsApp and Domiitulua (ops) formats.

Rules (plan §5):

- WhatsApp: digits only, ``57`` + 10 digits (12 total).
- Ops: digits only, 10 digits, no country code (``users.phone``).
"""

import re

COUNTRY_CODE = '57'
_OPS_RE = re.compile(r'^3\d{9}$')
_WA_RE = re.compile(r'^573\d{9}$')


def digits_only(value) -> str:
    """Return only the digits of ``value`` (empty string for None)."""
    if value is None:
        return ''
    return re.sub(r'\D+', '', str(value))


def to_wa(value) -> str:
    """Normalize to the WhatsApp format (``573001234567``).

    Returns an empty string when the value cannot be normalized.
    """
    digits = digits_only(value)
    if not digits:
        return ''
    if _WA_RE.match(digits):
        return digits
    if len(digits) == 10:
        return COUNTRY_CODE + digits
    if digits.startswith(COUNTRY_CODE) and len(digits) > 12:
        # e.g. 5713001234567 (extra digit) — keep the last 10
        tail = digits[-10:]
        return COUNTRY_CODE + tail if len(tail) == 10 else ''
    if len(digits) == 12 and digits.startswith(COUNTRY_CODE):
        return digits
    return ''


def to_ops(value) -> str:
    """Normalize to the ops format (``3001234567``).

    Returns an empty string when the value cannot be normalized.
    """
    digits = digits_only(value)
    if not digits:
        return ''
    if len(digits) == 12 and digits.startswith(COUNTRY_CODE):
        digits = digits[2:]
    if len(digits) == 10:
        return digits
    if len(digits) > 10 and digits.startswith(COUNTRY_CODE):
        return digits[-10:] if len(digits[-10:]) == 10 else ''
    if len(digits) == 11 and digits.startswith(COUNTRY_CODE):
        return digits[1:]
    return ''


def is_wa(value) -> bool:
    """True when ``value`` is a valid WhatsApp phone (573 + 9 digits)."""
    return bool(_WA_RE.match(digits_only(value)))


def is_ops_10(value) -> bool:
    """True when ``value`` is a valid ops phone (10 digits)."""
    return bool(re.fullmatch(r'\d{10}', digits_only(value)))
