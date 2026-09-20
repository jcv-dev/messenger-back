"""Tests for integration key auth, scopes, HMAC and throttle (plan §4.2/§9)."""

import json
import time

from django.test import override_settings
from rest_framework.test import APITestCase

from api.integrations.auth import generate_api_key, hash_api_key
from api.integrations.hmac import compute_signature
from api.models import IntegrationApiKey

SECRET = 'test-webhook-secret'


def signed(body: str, key: str, *, secret: str = SECRET, timestamp=None):
    ts = int(timestamp if timestamp is not None else time.time())
    return {
        'HTTP_X_API_KEY': key,
        'HTTP_X_SIGNATURE': 'sha256=' + compute_signature(secret, body),
        'HTTP_X_TIMESTAMP': str(ts),
    }


@override_settings(INTEGRATION_WEBHOOK_SECRET=SECRET)
class IntegrationAuthTests(APITestCase):
    def make_key(self, scopes, active=True):
        raw, key_hash, prefix = generate_api_key()
        IntegrationApiKey.objects.create(
            name='ops', key_hash=key_hash, prefix=prefix,
            scopes=scopes, is_active=active,
        )
        return raw

    # ── Key format ──────────────────────────────────────────────────────

    def test_generate_key_shape(self):
        raw, key_hash, prefix = generate_api_key()
        self.assertTrue(raw.startswith('mk_live_'))
        self.assertEqual(len(key_hash), 64)
        self.assertEqual(key_hash, hash_api_key(raw))
        self.assertEqual(prefix, raw[:12])

    def test_raw_key_is_never_stored(self):
        raw = self.make_key([])
        stored = IntegrationApiKey.objects.get()
        self.assertNotEqual(stored.key_hash, raw)

    # ── Authentication ──────────────────────────────────────────────────

    def test_missing_key_returns_401(self):
        body = json.dumps({'ping': True})
        res = self.client.post('/api/integrations/ping/', body, content_type='application/json')
        self.assertEqual(res.status_code, 401)

    def test_invalid_key_returns_401(self):
        body = json.dumps({'ping': True})
        res = self.client.post(
            '/api/integrations/ping/', body, content_type='application/json',
            **signed(body, 'mk_live_does_not_exist'),
        )
        self.assertEqual(res.status_code, 401)

    def test_inactive_key_returns_401(self):
        raw = self.make_key([], active=False)
        body = json.dumps({'ping': True})
        res = self.client.post(
            '/api/integrations/ping/', body, content_type='application/json',
            **signed(body, raw),
        )
        self.assertEqual(res.status_code, 401)

    def test_valid_key_and_signature_returns_key_info(self):
        raw = self.make_key(['orders:write', 'exemptions:write'])
        body = json.dumps({'ping': True})
        res = self.client.post(
            '/api/integrations/ping/', body, content_type='application/json',
            **signed(body, raw),
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['ok'])
        self.assertEqual(res.json()['key_name'], 'ops')
        self.assertEqual(res.json()['scopes'], ['orders:write', 'exemptions:write'])

    def test_last_used_at_is_recorded(self):
        raw = self.make_key([])
        body = json.dumps({'ping': True})
        self.client.post(
            '/api/integrations/ping/', body, content_type='application/json',
            **signed(body, raw),
        )
        key = IntegrationApiKey.objects.get()
        self.assertIsNotNone(key.last_used_at)

    # ── Scopes ──────────────────────────────────────────────────────────

    def test_wrong_scope_returns_403(self):
        raw = self.make_key(['exemptions:write'])
        body = json.dumps({'order_number': 1, 'status': 'asignado'})
        res = self.client.post(
            '/api/integrations/orders/events/', body, content_type='application/json',
            **signed(body, raw),
        )
        self.assertEqual(res.status_code, 403)

    # ── HMAC ────────────────────────────────────────────────────────────

    def test_bad_signature_returns_401(self):
        raw = self.make_key([])
        body = json.dumps({'ping': True})
        headers = signed(body, raw)
        headers['HTTP_X_SIGNATURE'] = 'sha256=' + '0' * 64
        res = self.client.post(
            '/api/integrations/ping/', body, content_type='application/json', **headers,
        )
        self.assertEqual(res.status_code, 401)

    def test_missing_signature_returns_401(self):
        raw = self.make_key([])
        body = json.dumps({'ping': True})
        res = self.client.post(
            '/api/integrations/ping/', body, content_type='application/json',
            HTTP_X_API_KEY=raw, HTTP_X_TIMESTAMP=str(int(time.time())),
        )
        self.assertEqual(res.status_code, 401)

    def test_missing_timestamp_returns_401(self):
        raw = self.make_key([])
        body = json.dumps({'ping': True})
        res = self.client.post(
            '/api/integrations/ping/', body, content_type='application/json',
            HTTP_X_API_KEY=raw, HTTP_X_SIGNATURE='sha256=' + compute_signature(SECRET, body),
        )
        self.assertEqual(res.status_code, 401)

    def test_expired_timestamp_returns_401(self):
        raw = self.make_key([])
        body = json.dumps({'ping': True})
        res = self.client.post(
            '/api/integrations/ping/', body, content_type='application/json',
            **signed(body, raw, timestamp=time.time() - 400),
        )
        self.assertEqual(res.status_code, 401)

    def test_future_timestamp_returns_401(self):
        raw = self.make_key([])
        body = json.dumps({'ping': True})
        res = self.client.post(
            '/api/integrations/ping/', body, content_type='application/json',
            **signed(body, raw, timestamp=time.time() + 400),
        )
        self.assertEqual(res.status_code, 401)

    def test_tampered_body_returns_401(self):
        raw = self.make_key([])
        original = json.dumps({'ping': True})
        tampered = json.dumps({'ping': False, 'extra': 'x'})
        res = self.client.post(
            '/api/integrations/ping/', tampered, content_type='application/json',
            **signed(original, raw),
        )
        self.assertEqual(res.status_code, 401)

    def test_signature_without_sha256_prefix_is_accepted(self):
        raw = self.make_key([])
        body = json.dumps({'ping': True})
        headers = signed(body, raw)
        headers['HTTP_X_SIGNATURE'] = headers['HTTP_X_SIGNATURE'][len('sha256='):]
        res = self.client.post(
            '/api/integrations/ping/', body, content_type='application/json', **headers,
        )
        self.assertEqual(res.status_code, 200)

    @override_settings(INTEGRATION_WEBHOOK_SECRET='')
    def test_missing_secret_configuration_rejects(self):
        raw = self.make_key([])
        body = json.dumps({'ping': True})
        res = self.client.post(
            '/api/integrations/ping/', body, content_type='application/json',
            **signed(body, raw),
        )
        self.assertEqual(res.status_code, 401)

    # ── Throttle ────────────────────────────────────────────────────────

    def test_throttle_cache_key_is_per_key(self):
        from unittest.mock import patch

        from api.integrations.auth import IntegrationRateThrottle

        throttle = IntegrationRateThrottle()
        self.assertEqual(throttle.get_rate(), '120/min')

        key = IntegrationApiKey.objects.create(
            name='k', key_hash='a' * 64, prefix='mk_live_aaaa', scopes=[],
        )

        request = type('Req', (), {})()
        request.auth = key
        with patch.object(throttle, 'get_ident', return_value='1.2.3.4'):
            cache_key = throttle.get_cache_key(request, None)
        self.assertEqual(cache_key, f'integration:throttle:integration:key:{key.id}')
