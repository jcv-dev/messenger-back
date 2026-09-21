"""HTTP client for the Domiitulua public API v1 (ops).

Server-to-server only: the key never reaches the browser. Retries mirror
``api/bot/calculator.py`` (2 retries, 429/5xx, exponential backoff).
"""

import logging
import time

import httpx
from django.conf import settings

logger = logging.getLogger('api')

MAX_RETRIES = 2
RETRYABLE_CODES = frozenset({429, 500, 502, 503, 504})
DEFAULT_TIMEOUT = 15.0


class OpsAPIError(Exception):
    """Ops responded with an error or could not be reached."""

    def __init__(self, message, status_code=None, payload=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload


class OpsNotConfigured(OpsAPIError):
    """``OPS_API_URL`` / ``OPS_API_KEY`` are missing."""


def _base_url() -> str:
    return (getattr(settings, 'OPS_API_URL', '') or '').rstrip('/')


def _api_key() -> str:
    return getattr(settings, 'OPS_API_KEY', '') or ''


def is_configured() -> bool:
    return bool(_base_url() and _api_key())


def _headers() -> dict:
    return {
        'Authorization': f'Bearer {_api_key()}',
        'Accept': 'application/json',
    }


def _request(method: str, path: str, *, params=None, json_body=None,
             timeout=None, extra_headers=None):
    """Perform a request against the ops API with retries.

    Returns the decoded JSON body. Raises ``OpsNotConfigured`` /
    ``OpsAPIError`` on failure.
    """
    base = _base_url()
    if not base or not _api_key():
        raise OpsNotConfigured('Ops API no configurada (OPS_API_URL/OPS_API_KEY).')

    headers = _headers()
    if extra_headers:
        headers.update(extra_headers)

    url = f'{base}{path}'
    last_error = None
    timeout = timeout or getattr(settings, 'OPS_TIMEOUT', DEFAULT_TIMEOUT)

    for attempt in range(MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.request(method, url, params=params, json=json_body, headers=headers)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(0.5 * (2 ** attempt))
                continue
            logger.warning('OPS request failed: %s %s — %s', method, path, exc)
            raise OpsAPIError(f'No se pudo conectar con ops: {exc}') from exc

        if resp.status_code in RETRYABLE_CODES and attempt < MAX_RETRIES:
            time.sleep(0.5 * (2 ** attempt))
            last_error = OpsAPIError(
                f'Ops respondió {resp.status_code}', status_code=resp.status_code
            )
            continue

        try:
            payload = resp.json()
        except ValueError:
            payload = None

        if resp.status_code >= 400:
            detail = ''
            if isinstance(payload, dict):
                detail = payload.get('error') or payload.get('message') or ''
            message = f'Ops respondió {resp.status_code}' + (f': {detail}' if detail else '')
            raise OpsAPIError(message, status_code=resp.status_code, payload=payload)

        return payload

    raise last_error  # pragma: no cover


# ---------------------------------------------------------------------------
#  Public API v1 (plan §4.1)
# ---------------------------------------------------------------------------


def get_services():
    """GET /api/v1/services — active ops service catalog."""
    return _request('GET', '/api/v1/services')


def get_client_by_phone(phone: str, timeout: float | None = None):
    """GET /api/v1/clients/{phone} — ``found=false`` / ``type=no_cliente`` when unknown."""
    return _request('GET', f'/api/v1/clients/{phone}', timeout=timeout)


def search_clients(query: str, limit: int = 10):
    """GET /api/v1/clients?q= — returns ``{ok, rows: [...]}``."""
    return _request('GET', '/api/v1/clients', params={'q': query, 'limit': limit})


def get_client_addresses(client_id: int):
    """GET /api/v1/clients/{id}/addresses — returns ``{ok, rows: [...]}``."""
    return _request('GET', f'/api/v1/clients/{client_id}/addresses')


def set_client_default_address(client_id: int, address: str, lat=None, lng=None):
    """POST /api/v1/clients/{id}/addresses/default.

    Adds (or updates) the address in the client's history and marks it as the
    default one, replacing the previous. Returns ``{ok, row}``.
    """
    body = {'address': address}
    if lat is not None:
        body['lat'] = lat
    if lng is not None:
        body['lng'] = lng
    return _request('POST', f'/api/v1/clients/{client_id}/addresses/default', json_body=body)


def get_client_orders(client_id: int, limit: int = 5, timeout: float | None = None):
    """GET /api/v1/clients/{id}/orders — returns ``{ok, orders: [...]}``."""
    return _request(
        'GET', f'/api/v1/clients/{client_id}/orders',
        params={'limit': limit}, timeout=timeout,
    )


def create_order(payload: dict, idempotency_key: str | None = None):
    """POST /api/v1/orders with an optional ``X-Idempotency-Key``."""
    headers = {}
    if idempotency_key:
        headers['X-Idempotency-Key'] = idempotency_key
    return _request('POST', '/api/v1/orders', json_body=payload, extra_headers=headers)


def cancel_order(order_number: int, reason: str):
    """POST /api/v1/orders/{n}/cancel with a mandatory reason."""
    return _request(
        'POST',
        f'/api/v1/orders/{order_number}/cancel',
        json_body={'reason': reason},
    )


def cancel_order_stop(order_number: int, stop_no: int, reason: str):
    """POST /api/v1/orders/{n}/stops/{stop}/cancel (one parada of a comanda)."""
    return _request(
        'POST',
        f'/api/v1/orders/{order_number}/stops/{stop_no}/cancel',
        json_body={'reason': reason},
    )


def get_order(order_number: int, timeout: float | None = None):
    """GET /api/v1/orders/{n}."""
    return _request('GET', f'/api/v1/orders/{order_number}', timeout=timeout)


def list_couriers(query: str | None = None):
    """GET /api/v1/couriers — snapshot with turn/availability state."""
    params = {'q': query} if query else None
    return _request('GET', '/api/v1/couriers', params=params)
