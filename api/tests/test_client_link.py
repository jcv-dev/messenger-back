"""Tests for Phase 2 — client coupling (link endpoint, auto-link, backfill)
and the agent-facing client proxies (plan §4.3/§5)."""

import json
from io import StringIO
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from api.integrations import ops
from api.integrations.clients import (
    CLIENT_CACHE_NEGATIVE, CLIENT_CACHE_NEGATIVE_TTL, CLIENT_CACHE_POSITIVE_TTL,
    auto_link_now, cache_client, cache_key, fetch_client_by_phone,
    get_cached_client, normalize_client, schedule_auto_link,
)
from api.models import AuditLog, Conversation
from api.tests import get_or_create_tulua_group

OPS_SETTINGS = dict(OPS_API_URL='https://ops.test', OPS_API_KEY='dmikey_x')

CLIENT_PAYLOAD = {
    'ok': True,
    'found': True,
    'type': 'cliente',
    'id': 88,
    'name': 'Ana Pérez',
    'phone': '3001234567',
    'address': 'Calle 10 #20-30',
}

UNKNOWN_PAYLOAD = {
    'ok': True, 'found': False, 'type': 'no_cliente', 'phone': '3001234567',
}


class NormalizeClientTests(TestCase):
    def test_normalizes_client(self):
        snapshot = normalize_client(CLIENT_PAYLOAD)
        self.assertEqual(snapshot, {
            'id': 88, 'name': 'Ana Pérez', 'phone': '3001234567',
            'address': 'Calle 10 #20-30',
        })

    def test_rejects_unknown_and_riders(self):
        self.assertIsNone(normalize_client(UNKNOWN_PAYLOAD))
        rider = {**CLIENT_PAYLOAD, 'type': 'domiciliario'}
        self.assertIsNone(normalize_client(rider))

    def test_rejects_missing_id(self):
        self.assertIsNone(normalize_client({**CLIENT_PAYLOAD, 'id': None}))


@override_settings(**OPS_SETTINGS)
class FetchClientByPhoneTests(TestCase):
    def setUp(self):
        cache.delete(cache_key('3001234567'))

    @patch('api.integrations.clients.ops.get_client_by_phone')
    def test_positive_lookup_is_cached(self, lookup):
        lookup.return_value = CLIENT_PAYLOAD

        first = fetch_client_by_phone('573001234567')
        second = fetch_client_by_phone('3001234567')

        self.assertEqual(first, second)
        self.assertEqual(first['id'], 88)
        lookup.assert_called_once_with('3001234567', timeout=None)
        self.assertEqual(cache.get(cache_key('3001234567')), first)

    @patch('api.integrations.clients.ops.get_client_by_phone')
    def test_unknown_phone_uses_negative_cache(self, lookup):
        lookup.return_value = UNKNOWN_PAYLOAD

        self.assertIsNone(fetch_client_by_phone('3001234567'))
        self.assertIsNone(fetch_client_by_phone('3001234567'))

        lookup.assert_called_once()
        self.assertEqual(
            cache.get(cache_key('3001234567')), CLIENT_CACHE_NEGATIVE,
        )

    @patch('api.integrations.clients.ops.get_client_by_phone')
    def test_network_error_is_not_cached(self, lookup):
        lookup.side_effect = ops.OpsAPIError('boom')

        self.assertIsNone(fetch_client_by_phone('3001234567'))
        self.assertIsNone(cache.get(cache_key('3001234567')))

    @patch('api.integrations.clients.ops.get_client_by_phone')
    def test_invalid_phone_skips_network(self, lookup):
        self.assertIsNone(fetch_client_by_phone('12345'))
        lookup.assert_not_called()

    def test_cache_ttls(self):
        cache_client('3009999999', {'id': 1})
        cache_client('3008888888', None)

        raw = cache._cache.get_client(write=True)
        self.assertAlmostEqual(
            raw.ttl(cache.make_key(cache_key('3009999999'))),
            CLIENT_CACHE_POSITIVE_TTL, delta=5,
        )
        self.assertAlmostEqual(
            raw.ttl(cache.make_key(cache_key('3008888888'))),
            CLIENT_CACHE_NEGATIVE_TTL, delta=5,
        )
        hit, payload = get_cached_client('3009999999')
        self.assertTrue(hit)
        self.assertEqual(payload, {'id': 1})
        hit, payload = get_cached_client('3008888888')
        self.assertTrue(hit)
        self.assertIsNone(payload)
        hit, _ = get_cached_client('3007777777')
        self.assertFalse(hit)

        cache.delete(cache_key('3009999999'))
        cache.delete(cache_key('3008888888'))


@override_settings(**OPS_SETTINGS)
class AutoLinkTests(TestCase):
    def setUp(self):
        self.group = get_or_create_tulua_group()
        self.conversation = Conversation.objects.create(
            whatsapp_id='573001234567', contact_name='Ana',
            contact_phone='573001234567', group=self.group,
        )
        cache.delete(cache_key('3001234567'))

    @patch('api.views.publish_conversation_update')
    @patch('api.integrations.clients.ops.get_client_by_phone')
    def test_auto_link_now_links_and_publishes(self, lookup, publish):
        lookup.return_value = CLIENT_PAYLOAD

        linked = auto_link_now(self.conversation.id, '573001234567')

        self.assertTrue(linked)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.ops_client_user_id, 88)
        self.assertEqual(self.conversation.ops_client_match_source, 'auto_phone')
        self.assertEqual(self.conversation.ops_client_snapshot['name'], 'Ana Pérez')
        self.assertIsNotNone(self.conversation.ops_client_linked_at)
        publish.assert_called_once()

    @patch('api.views.publish_conversation_update')
    @patch('api.integrations.clients.ops.get_client_by_phone')
    def test_auto_link_skips_linked_conversation(self, lookup, publish):
        self.conversation.ops_client_user_id = 42
        self.conversation.save(update_fields=['ops_client_user_id'])

        self.assertFalse(auto_link_now(self.conversation.id, '573001234567'))
        lookup.assert_not_called()
        publish.assert_not_called()

    @patch('api.views.publish_conversation_update')
    @patch('api.integrations.clients.ops.get_client_by_phone')
    def test_auto_link_unknown_phone_is_noop(self, lookup, publish):
        lookup.return_value = UNKNOWN_PAYLOAD

        self.assertFalse(auto_link_now(self.conversation.id, '573001234567'))
        self.conversation.refresh_from_db()
        self.assertIsNone(self.conversation.ops_client_user_id)
        publish.assert_not_called()

    @patch('api.integrations.clients.threading.Thread')
    def test_schedule_starts_daemon_thread(self, thread_cls):
        started = schedule_auto_link(self.conversation.id, '573001234567')

        self.assertTrue(started)
        thread_cls.assert_called_once()
        self.assertTrue(thread_cls.call_args.kwargs['daemon'])
        thread_cls.return_value.start.assert_called_once()

    @override_settings(OPS_API_URL='', OPS_API_KEY='')
    @patch('api.integrations.clients.threading.Thread')
    def test_schedule_skips_when_ops_not_configured(self, thread_cls):
        self.assertFalse(schedule_auto_link(self.conversation.id, '573001234567'))
        thread_cls.assert_not_called()

    @patch('api.integrations.clients.threading.Thread')
    def test_schedule_skips_invalid_phone(self, thread_cls):
        self.assertFalse(schedule_auto_link(self.conversation.id, 'not-a-phone'))
        thread_cls.assert_not_called()


@override_settings(**OPS_SETTINGS)
class LinkClientEndpointTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='agent', password='x')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.group = get_or_create_tulua_group()
        self.conversation = Conversation.objects.create(
            whatsapp_id='573001234567', contact_name='Ana',
            contact_phone='573001234567', group=self.group,
        )
        cache.delete(cache_key('3001234567'))

    def test_requires_auth(self):
        self.client.credentials()
        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/link-client/', {},
            format='json',
        )
        self.assertEqual(response.status_code, 401)

    @patch('api.views.publish_conversation_update')
    def test_link_with_snapshot(self, publish):
        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/link-client/',
            {
                'ops_client_user_id': 88,
                'client': {'name': 'Ana Pérez', 'phone': '3001234567',
                           'address': 'Calle 10 #20-30'},
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['ops_client_user_id'], 88)
        self.assertEqual(response.data['ops_client_match_source'], 'manual')
        self.assertEqual(response.data['ops_client_snapshot']['name'], 'Ana Pérez')
        publish.assert_called_once()
        self.assertTrue(AuditLog.objects.filter(
            action='link_client', conversation=self.conversation,
        ).exists())

    @patch('api.views.publish_conversation_update')
    def test_unlink_clears_link(self, publish):
        self.conversation.ops_client_user_id = 88
        self.conversation.ops_client_match_source = 'manual'
        self.conversation.ops_client_snapshot = {'name': 'Ana'}
        self.conversation.save()

        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/link-client/', {},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data['ops_client_user_id'])
        self.assertEqual(response.data['ops_client_match_source'], '')
        self.assertEqual(response.data['ops_client_snapshot'], {})
        publish.assert_called_once()
        self.assertTrue(AuditLog.objects.filter(
            action='unlink_client', conversation=self.conversation,
        ).exists())

    def test_invalid_id_returns_400(self):
        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/link-client/',
            {'ops_client_user_id': 'abc'}, format='json',
        )
        self.assertEqual(response.status_code, 400)

    @patch('api.views.publish_conversation_update')
    @patch('api.integrations.clients.ops.get_client_by_phone')
    def test_link_without_snapshot_enriches_from_phone(self, lookup, publish):
        lookup.return_value = CLIENT_PAYLOAD

        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/link-client/',
            {'ops_client_user_id': 88}, format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['ops_client_snapshot']['name'], 'Ana Pérez')
        lookup.assert_called_once_with('3001234567', timeout=5)

    @patch('api.views.publish_conversation_update')
    @patch('api.integrations.clients.ops.get_client_by_phone')
    def test_link_without_snapshot_ignores_mismatched_phone(self, lookup, publish):
        lookup.return_value = {**CLIENT_PAYLOAD, 'id': 99}

        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/link-client/',
            {'ops_client_user_id': 88}, format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['ops_client_snapshot'], {})


@override_settings(**OPS_SETTINGS)
class WebhookAutoLinkTests(APITestCase):
    def setUp(self):
        get_or_create_tulua_group()

    def _payload(self, wa_id='573001234567'):
        return {
            'entry': [{'changes': [{'value': {
                'messages': [{
                    'from': wa_id, 'id': 'wamid.autolink1',
                    'type': 'text', 'text': {'body': 'Hola'},
                }],
                'contacts': [{'profile': {'name': 'Ana'}, 'wa_id': wa_id}],
            }}]}],
        }

    @patch('api.views.schedule_auto_link')
    def test_new_conversation_schedules_auto_link(self, schedule):
        with self.settings(WHATSAPP_APP_SECRET=''):
            response = self.client.post(
                '/webhook/', data=json.dumps(self._payload()),
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 200)
        schedule.assert_called_once()
        conv_id, phone = schedule.call_args.args
        self.assertEqual(phone, '573001234567')
        self.assertEqual(Conversation.objects.get(id=conv_id).contact_phone, '573001234567')

    @patch('api.views.schedule_auto_link')
    def test_linked_conversation_skips_auto_link(self, schedule):
        Conversation.objects.create(
            whatsapp_id='573001234567', contact_name='Ana',
            contact_phone='573001234567', ops_client_user_id=88,
        )

        with self.settings(WHATSAPP_APP_SECRET=''):
            self.client.post(
                '/webhook/', data=json.dumps(self._payload()),
                content_type='application/json',
            )

        schedule.assert_not_called()


@override_settings(**OPS_SETTINGS)
class LinkOpsClientsCommandTests(TestCase):
    def setUp(self):
        self.group = get_or_create_tulua_group()
        self.conversation = Conversation.objects.create(
            whatsapp_id='573001234567', contact_name='Ana',
            contact_phone='573001234567', group=self.group,
        )
        cache.delete(cache_key('3001234567'))

    @patch('api.management.commands.link_ops_clients.fetch_client_by_phone')
    def test_dry_run_does_not_write(self, lookup):
        lookup.return_value = {'id': 88, 'name': 'Ana Pérez', 'phone': '3001234567'}

        out = StringIO()
        call_command('link_ops_clients', '--dry-run', stdout=out)

        self.conversation.refresh_from_db()
        self.assertIsNone(self.conversation.ops_client_user_id)
        self.assertIn('Simulación', out.getvalue())
        self.assertIn('1 conversaciones revisadas, 1 vinculadas', out.getvalue())

    @patch('api.management.commands.link_ops_clients.fetch_client_by_phone')
    def test_backfill_links_conversations(self, lookup):
        lookup.return_value = {'id': 88, 'name': 'Ana Pérez', 'phone': '3001234567'}

        out = StringIO()
        call_command('link_ops_clients', '--batch-size', '1', stdout=out)

        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.ops_client_user_id, 88)
        self.assertEqual(self.conversation.ops_client_match_source, 'auto_phone')

    @patch('api.management.commands.link_ops_clients.fetch_client_by_phone')
    def test_limit_stops_early(self, lookup):
        Conversation.objects.create(
            whatsapp_id='573009999999', contact_name='Luis',
            contact_phone='573009999999', group=self.group,
        )
        lookup.return_value = {'id': 88, 'name': 'Ana Pérez', 'phone': '3001234567'}

        call_command('link_ops_clients', '--limit', '1')

        self.assertEqual(lookup.call_count, 1)

    @override_settings(OPS_API_URL='', OPS_API_KEY='')
    def test_requires_ops_configuration(self):
        with self.assertRaises(CommandError):
            call_command('link_ops_clients')


@override_settings(**OPS_SETTINGS)
class OrderClientProxyTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='agent', password='x')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def test_search_requires_auth(self):
        self.client.credentials()
        response = self.client.get('/api/orders/clients/?q=Ana')
        self.assertEqual(response.status_code, 401)

    def test_short_query_returns_empty_without_ops(self):
        with patch('api.order_views.ops.search_clients') as search:
            response = self.client.get('/api/orders/clients/?q=A')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'ok': True, 'rows': []})
        search.assert_not_called()

    @patch('api.order_views.ops.search_clients')
    def test_search_proxies_rows(self, search):
        search.return_value = {'ok': True, 'rows': [{'id': 88, 'name': 'Ana'}]}

        response = self.client.get('/api/orders/clients/?q=Ana')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['rows'], [{'id': 88, 'name': 'Ana'}])
        search.assert_called_once_with('Ana')

    @patch('api.order_views.ops.search_clients')
    def test_search_returns_502_on_ops_error(self, search):
        search.side_effect = ops.OpsAPIError('Ops respondió 500')

        response = self.client.get('/api/orders/clients/?q=Ana')

        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.data['ok'])

    @override_settings(OPS_API_URL='', OPS_API_KEY='')
    def test_search_returns_503_when_not_configured(self):
        response = self.client.get('/api/orders/clients/?q=Ana')
        self.assertEqual(response.status_code, 503)

    @patch('api.order_views.ops.get_client_addresses')
    def test_addresses_proxy(self, addresses):
        addresses.return_value = {'ok': True, 'rows': [{'id': 5, 'address': 'Cra 5'}]}

        response = self.client.get('/api/orders/clients/88/addresses/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['rows'][0]['address'], 'Cra 5')
        addresses.assert_called_once_with(88)

    @patch('api.order_views.ops.get_client_orders')
    def test_orders_proxy_clamps_limit(self, orders):
        orders.return_value = {'ok': True, 'orders': []}

        response = self.client.get('/api/orders/clients/88/orders/?limit=99')

        self.assertEqual(response.status_code, 200)
        orders.assert_called_once_with(88, limit=20)

    @patch('api.order_views.ops.get_client_orders')
    def test_orders_proxy_defaults_to_five(self, orders):
        orders.return_value = {'ok': True, 'orders': []}

        self.client.get('/api/orders/clients/88/orders/')

        orders.assert_called_once_with(88, limit=5)

    @patch('api.order_views.ops.set_client_default_address')
    def test_set_default_address_proxies_payload(self, set_default):
        set_default.return_value = {
            'ok': True,
            'row': {'id': 7, 'address': 'Calle 20 3 10', 'is_default': 1},
        }

        response = self.client.post(
            '/api/orders/clients/88/addresses/default/',
            {'address': 'Calle 20 #3-10', 'lat': 4.085, 'lng': -76.195},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['ok'])
        self.assertEqual(response.data['row']['id'], 7)
        set_default.assert_called_once_with(88, 'Calle 20 #3-10', lat=4.085, lng=-76.195)

    @patch('api.order_views.ops.set_client_default_address')
    def test_set_default_address_without_coords(self, set_default):
        set_default.return_value = {'ok': True, 'row': {'id': 7}}

        self.client.post(
            '/api/orders/clients/88/addresses/default/',
            {'address': 'Calle 20 #3-10'},
            format='json',
        )

        set_default.assert_called_once_with(88, 'Calle 20 #3-10', lat=None, lng=None)

    @patch('api.order_views.ops.set_client_default_address')
    def test_set_default_address_validates_input(self, set_default):
        for payload in (
            {},
            {'address': 'ab'},
            {'address': 'Calle 20 #3-10', 'lat': 120},
        ):
            response = self.client.post(
                '/api/orders/clients/88/addresses/default/', payload, format='json',
            )
            self.assertEqual(response.status_code, 400)

        set_default.assert_not_called()

    @patch('api.order_views.ops.set_client_default_address')
    def test_set_default_address_returns_502_on_ops_error(self, set_default):
        set_default.side_effect = ops.OpsAPIError('Ops respondió 403: Sin permiso')

        response = self.client.post(
            '/api/orders/clients/88/addresses/default/',
            {'address': 'Calle 20 #3-10'},
            format='json',
        )

        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.data['ok'])

    @override_settings(OPS_API_URL='', OPS_API_KEY='')
    def test_set_default_address_returns_503_when_not_configured(self):
        response = self.client.post(
            '/api/orders/clients/88/addresses/default/',
            {'address': 'Calle 20 #3-10'},
            format='json',
        )

        self.assertEqual(response.status_code, 503)
