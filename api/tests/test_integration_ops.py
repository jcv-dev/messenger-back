"""Tests for the ops HTTP client (plan §4.1/§9) using a fake httpx.Client."""

from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from api.integrations import ops


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class FakeClient:
    """Minimal httpx.Client stand-in that records calls."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def request(self, method, url, params=None, json=None, headers=None):
        self.calls.append({
            'method': method, 'url': url, 'params': params,
            'json': json, 'headers': headers or {},
        })
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


@override_settings(OPS_API_URL='https://ops.test', OPS_API_KEY='dmikey_x')
class OpsClientTests(SimpleTestCase):
    def fake(self, responses):
        client = FakeClient(responses)

        class _Factory:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return client

            def __exit__(self, *args):
                return False

        return client, _Factory

    def test_is_configured(self):
        self.assertTrue(ops.is_configured())

    @override_settings(OPS_API_URL='', OPS_API_KEY='')
    def test_not_configured_raises(self):
        self.assertFalse(ops.is_configured())
        with self.assertRaises(ops.OpsNotConfigured):
            ops.get_order(123)

    def test_get_client_by_phone_uses_bearer_and_path(self):
        client, factory = self.fake([FakeResponse(200, {'ok': True, 'found': True, 'type': 'cliente'})])

        with patch('api.integrations.ops.httpx.Client', factory):
            result = ops.get_client_by_phone('3001234567')

        self.assertTrue(result['found'])
        call = client.calls[0]
        self.assertEqual(call['method'], 'GET')
        self.assertEqual(call['url'], 'https://ops.test/api/v1/clients/3001234567')
        self.assertEqual(call['headers']['Authorization'], 'Bearer dmikey_x')

    def test_search_clients_passes_query(self):
        client, factory = self.fake([FakeResponse(200, {'ok': True, 'rows': []})])

        with patch('api.integrations.ops.httpx.Client', factory):
            ops.search_clients('Ana')

        self.assertEqual(client.calls[0]['params'], {'q': 'Ana', 'limit': 10})

    def test_create_order_sends_idempotency_header(self):
        client, factory = self.fake([FakeResponse(201, {'ok': True})])

        with patch('api.integrations.ops.httpx.Client', factory):
            ops.create_order({'origin_address': 'Calle 1'}, idempotency_key='abc-123')

        call = client.calls[0]
        self.assertEqual(call['method'], 'POST')
        self.assertEqual(call['url'], 'https://ops.test/api/v1/orders')
        self.assertEqual(call['headers']['X-Idempotency-Key'], 'abc-123')

    def test_retries_on_5xx_then_succeeds(self):
        client, factory = self.fake([
            FakeResponse(500, {'error': 'boom'}),
            FakeResponse(200, {'ok': True}),
        ])

        with patch('api.integrations.ops.httpx.Client', factory):
            with patch('api.integrations.ops.time.sleep') as sleep:
                result = ops.get_order(1234)

        self.assertTrue(result['ok'])
        self.assertEqual(len(client.calls), 2)
        sleep.assert_called_once()

    def test_4xx_raises_ops_api_error_with_status(self):
        _, factory = self.fake([FakeResponse(404, {'error': 'no existe'})])

        with patch('api.integrations.ops.httpx.Client', factory):
            with self.assertRaises(ops.OpsAPIError) as ctx:
                ops.get_order(999)

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn('no existe', str(ctx.exception))
