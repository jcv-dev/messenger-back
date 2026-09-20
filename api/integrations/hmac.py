"""Webhook signature verification for ops → Messager pushes.

Headers (plan §4.2):

- ``X-Api-Key``:   integration key (verified by ``IntegrationKeyAuthentication``).
- ``X-Signature``: ``sha256=<hmac_hex>`` — HMAC-SHA256 of the raw body with
  ``INTEGRATION_WEBHOOK_SECRET``.
- ``X-Timestamp``: unix seconds; skew above ``MAX_SKEW_SECONDS`` is rejected
  (replay protection).
"""

import hashlib
import hmac
import time

from django.conf import settings
from rest_framework import exceptions

SIGNATURE_HEADER = 'X-Signature'
TIMESTAMP_HEADER = 'X-Timestamp'
MAX_SKEW_SECONDS = 300


def compute_signature(secret: str, body) -> str:
    """Return the HMAC-SHA256 hex digest of ``body`` (str or bytes)."""
    if isinstance(body, str):
        body = body.encode('utf-8')
    return hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()


def _normalize_signature(raw: str) -> str:
    raw = (raw or '').strip()
    if raw.startswith('sha256='):
        raw = raw[len('sha256='):]
    return raw.lower()


def verify_webhook_request(request, secret: str | None = None, *, max_skew: int = MAX_SKEW_SECONDS):
    """Validate ``X-Signature`` / ``X-Timestamp`` for ``request``.

    Raises ``rest_framework.exceptions.AuthenticationFailed`` (401) when the
    request must be rejected. Returns the accepted timestamp on success.
    """
    secret = secret if secret is not None else getattr(settings, 'INTEGRATION_WEBHOOK_SECRET', '')
    if not secret:
        raise exceptions.AuthenticationFailed('INTEGRATION_WEBHOOK_SECRET no configurado.')

    raw_signature = request.headers.get(SIGNATURE_HEADER, '')
    if not raw_signature:
        raise exceptions.AuthenticationFailed(f'Falta el header {SIGNATURE_HEADER}.')

    raw_timestamp = request.headers.get(TIMESTAMP_HEADER, '')
    if not raw_timestamp:
        raise exceptions.AuthenticationFailed(f'Falta el header {TIMESTAMP_HEADER}.')

    try:
        timestamp = int(raw_timestamp)
    except (TypeError, ValueError):
        raise exceptions.AuthenticationFailed('Timestamp inválido.')

    now = int(time.time())
    if abs(now - timestamp) > max_skew:
        raise exceptions.AuthenticationFailed('Timestamp fuera de rango (posible replay).')

    expected = compute_signature(secret, request.body)
    provided = _normalize_signature(raw_signature)
    if not hmac.compare_digest(expected, provided):
        raise exceptions.AuthenticationFailed('Firma HMAC inválida.')

    return timestamp
