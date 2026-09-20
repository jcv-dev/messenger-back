"""Synchronous client for the Domi Calculator API (agent-facing proxies, §4.3).

The bot uses the async client in ``api/bot/calculator.py``; the agent endpoints
are regular DRF views, so this module mirrors that client with a sync
``httpx.Client``, the same retry policy and the ``X-API-Key`` header.
"""

import logging
import time

import httpx
from django.conf import settings

logger = logging.getLogger('api')

MAX_RETRIES = 2
RETRYABLE_CODES = frozenset({429, 500, 502, 503, 504})
DEFAULT_TIMEOUT = 15.0


class CalculatorAPIError(Exception):
    """The calculator responded with an error or could not be reached."""

    def __init__(self, message, status_code=None, payload=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload


class CalculatorNotConfigured(CalculatorAPIError):
    """``DOMII_CALCULATOR_URL`` is missing."""


def _base_url() -> str:
    return (getattr(settings, 'DOMII_CALCULATOR_URL', '') or '').rstrip('/')


def _api_key() -> str:
    return getattr(settings, 'DOMII_CALCULATOR_API_KEY', '') or ''


def is_configured() -> bool:
    return bool(_base_url())


def _headers() -> dict:
    headers = {'Accept': 'application/json'}
    if _api_key():
        headers['X-API-Key'] = _api_key()
    return headers


def _request(method: str, path: str, *, params=None, json_body=None, timeout=None):
    """Perform a request against the calculator with retries.

    Returns the decoded JSON body. Raises ``CalculatorNotConfigured`` /
    ``CalculatorAPIError`` on failure.
    """
    base = _base_url()
    if not base:
        raise CalculatorNotConfigured('Calculadora no configurada (DOMII_CALCULATOR_URL).')

    url = f'{base}{path}'
    timeout = timeout or DEFAULT_TIMEOUT
    last_error = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                resp = client.request(
                    method, url, params=params, json=json_body, headers=_headers(),
                )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(0.5 * (2 ** attempt))
                continue
            logger.warning('Calculator request failed: %s %s — %s', method, path, exc)
            raise CalculatorAPIError(f'No se pudo conectar con la calculadora: {exc}') from exc

        if resp.status_code in RETRYABLE_CODES and attempt < MAX_RETRIES:
            time.sleep(0.5 * (2 ** attempt))
            last_error = CalculatorAPIError(
                f'Calculadora respondió {resp.status_code}', status_code=resp.status_code,
            )
            continue

        try:
            payload = resp.json()
        except ValueError:
            payload = None

        if resp.status_code >= 400:
            detail = ''
            if isinstance(payload, dict):
                detail = payload.get('detail') or payload.get('error') or ''
            message = f'Calculadora respondió {resp.status_code}' + (f': {detail}' if detail else '')
            raise CalculatorAPIError(message, status_code=resp.status_code, payload=payload)

        return payload

    raise last_error  # pragma: no cover


def geocode_search(query: str):
    """GET /api/geocode/search — list of ``{display_name, place_id}``."""
    return _request('GET', '/api/geocode/search', params={'q': query}, timeout=10)


def geocode_details(place_id: str):
    """GET /api/geocode/details — ``{display_name, lat, lng}``."""
    return _request('GET', '/api/geocode/details', params={'place_id': place_id}, timeout=10)


def calculate_price(payload: dict):
    """POST /api/calculate-price — multi-segment quote."""
    return _request('POST', '/api/calculate-price', json_body=payload, timeout=20)


def get_tools():
    """GET /api/tools — active tool catalog."""
    return _request('GET', '/api/tools', timeout=10)
