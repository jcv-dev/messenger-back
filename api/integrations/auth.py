"""Authentication, scopes and throttling for the integration endpoints.

Ops authenticates with the ``X-Api-Key`` header. Only the SHA-256 hash of the
key is stored (``IntegrationApiKey.key_hash``); ``last_used_at`` is updated
with a 5-minute debounce to avoid a write on every request.
"""

import hashlib
import secrets

from django.core.cache import cache
from django.utils import timezone
from rest_framework import authentication, exceptions
from rest_framework.permissions import BasePermission
from rest_framework.throttling import SimpleRateThrottle

from api.models import IntegrationApiKey

KEY_PREFIX = 'mk_live_'

SCOPE_EXEMPTIONS_WRITE = 'exemptions:write'
SCOPE_ORDERS_WRITE = 'orders:write'
ALL_SCOPES = (SCOPE_EXEMPTIONS_WRITE, SCOPE_ORDERS_WRITE)

LAST_USED_DEBOUNCE_SECONDS = 300
RATE_LIMIT = '120/min'
RATE_CACHE_FORMAT = 'integration:throttle:%(scope)s:%(ident)s'


def hash_api_key(raw: str) -> str:
    """SHA-256 hex digest of the raw key."""
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Return ``(raw_key, key_hash, prefix)``.

    The raw key is returned once; only the hash should be persisted.
    """
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    return raw, hash_api_key(raw), raw[:12]


class IntegrationKeyAuthentication(authentication.BaseAuthentication):
    """DRF authenticator for ``X-Api-Key`` integration keys."""

    keyword = 'X-Api-Key'

    def authenticate(self, request):
        raw = request.META.get('HTTP_X_API_KEY') or ''
        raw = raw.strip()
        if not raw:
            return None

        key = IntegrationApiKey.objects.filter(
            key_hash=hash_api_key(raw),
            is_active=True,
        ).first()

        if key is None:
            raise exceptions.AuthenticationFailed('API key inválida o inactiva.')

        cache_key = f'integration:key:{key.id}:used'
        if cache.add(cache_key, 1, LAST_USED_DEBOUNCE_SECONDS):
            IntegrationApiKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())

        # (user, auth): integrations are not end users, the key travels in auth.
        return (None, key)

    def authenticate_header(self, request):
        return self.keyword


class HasIntegrationScope(BasePermission):
    """Require the scope declared on the view (``integration_scope``)."""

    message = 'Scope de integración insuficiente.'

    def has_permission(self, request, view):
        key = request.auth
        if not isinstance(key, IntegrationApiKey):
            return False
        if not key.is_active:
            return False
        scope = getattr(view, 'integration_scope', None)
        if not scope:
            return True
        return scope in (key.scopes or [])


class IntegrationRateThrottle(SimpleRateThrottle):
    """120 requests/minute per integration key (per plan §4.2)."""

    scope = 'integration'

    def get_rate(self):
        return RATE_LIMIT

    def get_cache_key(self, request, view):
        key = request.auth
        if isinstance(key, IntegrationApiKey):
            ident = f'key:{key.id}'
        else:
            ident = f'ip:{self.get_ident(request)}'
        return RATE_CACHE_FORMAT % {'scope': self.scope, 'ident': ident}
