"""Tests for Phase 3 — order creation, proxies, local detail/refresh (plan §4.3)."""

from unittest import mock
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from api.integrations import ops
from api.integrations.orders import (
    DEFAULT_ORDER_CREATED_MESSAGE,
    OrderValidationError,
    build_confirmation_text,
    format_cop,
    order_numbers_text,
    validate_stops,
)
from api.models import BotConfig, CityGroup, Conversation, Order, OrderStop
from api.tests import assign_user_group, get_or_create_tulua_group

OPS_SETTINGS = dict(OPS_API_URL='https://ops.test', OPS_API_KEY='dmikey_x')

CLIENT_SNAPSHOT = {
    'id': 88, 'name': 'Ana Pérez', 'phone': '3001234567', 'address': 'Calle 10 #20-30',
}

OPS_RESPONSE = {
    'ok': True,
    'batch_id': 'a1b2c3d4e5f60718',
    'order_numbers': [1234],
    'order_number': 1234,
    'stops_count': 2,
    'status': 'disponible',
    'client': {'id': 88, 'name': 'Ana Pérez', 'phone': '3001234567'},
    'courier': None,
    'message': 'Pedido creado.',
    'test': True,
}

OPS_RESPONSE_ASSIGNED = {
    **OPS_RESPONSE,
    'status': 'asignado',
    'courier': {'id': 1642, 'name': 'Samuel Niampira', 'code': 'sn42'},
    'message': 'Pedido creado y asignado a sn42.',
}

COURIER_INPUT = {
    'ops_courier_user_id': 1642,
    'name': 'Samuel Niampira',
    'code': 'sn42',
}

PAYLOAD = {
    'origin_address': 'Calle 10 #20-30',
    'payment_method': 'efectivo',
    'profile': 'usuario_final',
    'tools': ['canasta'],
    'acompanante': False,
    'send_confirmation': True,
    'idempotency_key': 'uuid-1',
    'stops': [
        {
            'service_type': 'domicilio', 'dest_address': 'Cra 5 #12-01',
            'description': 'Paquete pequeño', 'observation': 'Timbre azul',
            'price': 8000,
        },
        {
            'service_type': 'compras', 'dest_address': '',
            'description': 'Mercado', 'price': 6500,
        },
    ],
}


def order_stops(order):
    return list(order.stops.order_by('stop_no', 'id'))


class OrderValidationUnitTests(TestCase):
    def test_format_cop(self):
        self.assertEqual(format_cop(14500), '$14.500')
        self.assertEqual(format_cop(0), '$0')
        self.assertEqual(format_cop(None), '$0')

    def test_validate_stops_normalizes_keys(self):
        stops = validate_stops([
            {'service_type': 'Domicilio', 'dest_address': 'Cra 5 #12-01', 'price': 8000},
        ], catalog=[])
        self.assertEqual(stops[0]['service_type'], 'domicilio')
        self.assertEqual(stops[0]['stop_no'], 1)
        self.assertEqual(stops[0]['price'], 8000)

    def test_validate_stops_requires_address_for_domicilio(self):
        with self.assertRaises(OrderValidationError) as ctx:
            validate_stops([
                {'service_type': 'domicilio', 'dest_address': '', 'price': 8000},
            ], catalog=[])
        self.assertIn('dirección', ctx.exception.message)

    def test_validate_stops_allows_addressless_service(self):
        stops = validate_stops([
            {'service_type': 'compras', 'dest_address': '', 'description': '', 'price': 5000},
        ], catalog=[])
        self.assertEqual(stops[0]['dest_address'], '')

    def test_validate_stops_rejects_unknown_service_and_zero_price(self):
        with self.assertRaises(OrderValidationError):
            validate_stops([
                {'service_type': 'teleportacion', 'dest_address': 'X', 'price': 100},
            ], catalog=[])
        with self.assertRaises(OrderValidationError):
            validate_stops([
                {'service_type': 'compras', 'dest_address': '', 'price': 0},
            ], catalog=[])

    def test_validate_stops_uses_catalog_requires_address(self):
        catalog = [{'key': 'compras', 'requires_address': True}]
        with self.assertRaises(OrderValidationError):
            validate_stops([
                {'service_type': 'compras', 'dest_address': '', 'price': 100},
            ], catalog=catalog)

    def test_build_confirmation_text_placeholders(self):
        conversation = Conversation.objects.create(
            whatsapp_id='573001234567', contact_name='Ana',
        )
        order = Order.objects.create(
            conversation=conversation, client_name='Ana Pérez',
            origin_address='Calle 10 #20-30', total=14500, status='disponible',
        )
        OrderStop.objects.create(
            order=order, stop_no=1, service_type='domicilio',
            dest_address='Cra 5 #12-01', price=8000, ops_order_number=1234,
            status='disponible',
        )
        OrderStop.objects.create(
            order=order, stop_no=2, service_type='compras',
            description='Mercado', price=6500, ops_order_number=1235,
            status='disponible',
        )
        text = build_confirmation_text(order)
        self.assertIn('#1234, #1235', text)
        # The default confirmation never shows the price (user feedback).
        self.assertNotIn('$14.500', text)
        self.assertNotIn('{total}', text)
        self.assertIn('Un domiciliario lo aceptará pronto', text)
        self.assertEqual(order_numbers_text(order), '#1234, #1235')

    def test_order_numbers_text_dedupes_a_shared_comanda_number(self):
        """A comanda has one order_number for N stops: never repeat it."""
        conversation = Conversation.objects.create(
            whatsapp_id='5730012345600', contact_name='Ana',
        )
        order = Order.objects.create(
            conversation=conversation, client_name='Ana Pérez',
            origin_address='Calle 10 #20-30', total=10000, status='disponible',
        )
        for stop_no in (1, 2):
            OrderStop.objects.create(
                order=order, stop_no=stop_no, service_type='domicilio',
                dest_address='X', price=5000, ops_order_number=1234,
                status='disponible',
            )
        self.assertEqual(order_numbers_text(order), '#1234')
        self.assertIn('#1234 recibido', build_confirmation_text(order))
        self.assertNotIn('#1234, #1234', build_confirmation_text(order))

    def test_build_confirmation_text_honors_bot_config(self):
        from api.bot.config import _clear_cache

        BotConfig.objects.update_or_create(
            key='order_created_message',
            defaults={'value': 'Pedido {order_numbers} por {total} ({count} paradas)'},
        )
        _clear_cache()
        try:
            conversation = Conversation.objects.create(
                whatsapp_id='5730012345679', contact_name='Ana',
            )
            order = Order.objects.create(
                conversation=conversation, total=9000, status='disponible',
            )
            OrderStop.objects.create(
                order=order, stop_no=1, service_type='domicilio',
                dest_address='X', price=9000, ops_order_number=77, status='disponible',
            )
            self.assertEqual(build_confirmation_text(order), 'Pedido #77 por $9.000 (1 paradas)')
        finally:
            BotConfig.objects.filter(key='order_created_message').delete()
            _clear_cache()

    def test_build_confirmation_text_first_order_number(self):
        from api.bot.config import _clear_cache

        BotConfig.objects.update_or_create(
            key='order_created_message',
            defaults={'value': 'Pedido {order_number} · {count} paradas'},
        )
        _clear_cache()
        try:
            conversation = Conversation.objects.create(
                whatsapp_id='5730012345678', contact_name='Ana',
            )
            order = Order.objects.create(conversation=conversation, status='disponible')
            OrderStop.objects.create(
                order=order, stop_no=1, service_type='domicilio',
                dest_address='A', price=5000, ops_order_number=1234, status='disponible',
            )
            OrderStop.objects.create(
                order=order, stop_no=2, service_type='compras',
                description='M', price=3000, ops_order_number=1235, status='disponible',
            )
            self.assertEqual(build_confirmation_text(order), 'Pedido #1234 · 2 paradas')
        finally:
            BotConfig.objects.filter(key='order_created_message').delete()
            _clear_cache()

    def test_default_message_shape(self):
        self.assertIn('{order_numbers}', DEFAULT_ORDER_CREATED_MESSAGE)
        # ``{total}`` stays supported for custom templates, but the default
        # confirmation does not include the price.
        self.assertNotIn('{total}', DEFAULT_ORDER_CREATED_MESSAGE)
        self.assertNotIn('Valor', DEFAULT_ORDER_CREATED_MESSAGE)


class OrderCreatedMessageConfigTests(APITestCase):
    """Editing the confirmation template applies immediately."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username='configadmin', password='x', is_staff=True)
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def test_update_value_clears_the_botconfig_cache(self):
        from api.bot.config import _clear_cache, get_config

        config, _ = BotConfig.objects.update_or_create(
            key='order_created_message',
            defaults={'value': DEFAULT_ORDER_CREATED_MESSAGE},
        )
        _clear_cache()
        get_config('order_created_message')  # warm the in-process cache

        res = self.client.patch(
            f'/api/bot-config/{config.id}/update_value/',
            {'value': 'Pedido {order_number} · {total}'},
            format='json',
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(get_config('order_created_message'), 'Pedido {order_number} · {total}')

        config.refresh_from_db()
        self.assertEqual(config.value, 'Pedido {order_number} · {total}')


@override_settings(**OPS_SETTINGS)
class CreateOrderEndpointTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.group = get_or_create_tulua_group()
        self.user = User.objects.create_user(username='agent', password='x')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.conversation = Conversation.objects.create(
            whatsapp_id='573001234567', contact_name='Ana Pérez',
            contact_phone='573001234567', group=self.group,
        )
        self.url = f'/api/conversations/{self.conversation.id}/orders/'

    def test_requires_auth(self):
        self.client.credentials()
        res = self.client.post(self.url, PAYLOAD, format='json')
        self.assertEqual(res.status_code, 401)

    def test_create_order_maps_multi_stop_batch(self):
        with patch('api.integrations.orders.ops.create_order',
                   return_value=OPS_RESPONSE) as create, \
                patch('api.integrations.orders.fetch_client_by_phone',
                      return_value=dict(CLIENT_SNAPSHOT)), \
                patch('api.integrations.orders.send_confirmation') as confirm, \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            res = self.client.post(self.url, PAYLOAD, format='json')

        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(res.data['created'])

        order = Order.objects.get()
        self.assertEqual(order.status, 'disponible')
        self.assertEqual(order.ops_batch_id, 'a1b2c3d4e5f60718')
        self.assertEqual(order.ops_client_user_id, 88)
        self.assertEqual(order.client_name, 'Ana Pérez')
        self.assertEqual(int(order.total), 14500)
        self.assertEqual(order.created_by, self.user)
        self.assertEqual(order.payload['ops']['batch_id'], 'a1b2c3d4e5f60718')

        stops = order_stops(order)
        # Comanda: un solo order_number compartido por todas las paradas.
        self.assertEqual([s.ops_order_number for s in stops], [1234, 1234])
        self.assertEqual([s.status for s in stops], ['disponible', 'disponible'])
        self.assertEqual(int(stops[0].price), 8000)

        ops_payload = create.call_args.args[0]
        self.assertEqual(ops_payload['origin_address'], 'Calle 10 #20-30')
        self.assertEqual(ops_payload['client_user_id'], 88)
        self.assertNotIn('client_name', ops_payload)
        self.assertEqual(len(ops_payload['items']), 2)
        self.assertEqual(ops_payload['items'][0]['precio_total'], 8000)
        self.assertEqual(ops_payload['items'][1]['service_type'], 'compras')
        self.assertEqual(create.call_args.kwargs['idempotency_key'], 'uuid-1')

        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.ops_client_user_id, 88)
        self.assertEqual(self.conversation.ops_client_match_source, 'order')

        confirm.assert_called_once()

    def test_records_ai_draft_source_and_metadata(self):
        """Phase 6: the order sheet reports the AI prefill (source + payload)."""
        payload = {
            **PAYLOAD,
            'source': 'llm',
            'draft': {
                'from_message_id': 55,
                'confidence': 0.86,
                'missing': ['precio de la parada 2'],
                'model': 'deepseek-flash',
                'cached': True,
            },
        }
        with patch('api.integrations.orders.ops.create_order',
                   return_value=OPS_RESPONSE), \
                patch('api.integrations.orders.fetch_client_by_phone',
                      return_value=dict(CLIENT_SNAPSHOT)), \
                patch('api.integrations.orders.send_confirmation'), \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            res = self.client.post(self.url, payload, format='json')

        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get()
        self.assertEqual(order.source, 'llm')
        self.assertEqual(order.payload['draft'], {
            'from_message_id': 55,
            'confidence': 0.86,
            'missing': ['precio de la parada 2'],
            'model': 'deepseek-flash',
            'cached': True,
        })

    def test_rejects_an_unknown_source(self):
        res = self.client.post(self.url, {**PAYLOAD, 'source': 'robot'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('source', res.data['details'])

    def test_client_creation_by_explicit_name(self):
        payload = dict(PAYLOAD)
        payload['client'] = {'name': 'Cliente Nuevo'}
        with patch('api.integrations.orders.ops.create_order',
                   return_value={**OPS_RESPONSE, 'client': None}) as create, \
                patch('api.integrations.orders.fetch_client_by_phone', return_value=None), \
                patch('api.integrations.orders.send_confirmation'), \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            res = self.client.post(self.url, payload, format='json')

        self.assertEqual(res.status_code, 201, res.data)
        ops_payload = create.call_args.args[0]
        self.assertNotIn('client_user_id', ops_payload)
        self.assertEqual(ops_payload['client_name'], 'Cliente Nuevo')
        self.assertEqual(ops_payload['client_phone'], '3001234567')

    def test_explicit_client_search_data_links_snapshot(self):
        """Picking a search result carries its name/phone/address into the link."""
        payload = dict(PAYLOAD)
        payload['client'] = {
            'ops_client_user_id': 88,
            'name': 'Ana Pérez',
            'phone': '3001111111',
            'address': 'Calle 10 #20-30',
        }
        with patch('api.integrations.orders.ops.create_order',
                   return_value=OPS_RESPONSE) as create, \
                patch('api.integrations.orders.send_confirmation'), \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            res = self.client.post(self.url, payload, format='json')

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(create.call_args.args[0]['client_user_id'], 88)
        self.assertNotIn('client_name', create.call_args.args[0])

        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.ops_client_user_id, 88)
        self.assertEqual(self.conversation.ops_client_match_source, 'order')
        self.assertEqual(self.conversation.ops_client_snapshot, {
            'id': 88,
            'name': 'Ana Pérez',
            'phone': '3001111111',
            'address': 'Calle 10 #20-30',
        })

    def test_unlinked_client_uses_conversation_name(self):
        """No explicit client: ops creates it from the conversation identity."""
        with patch('api.integrations.orders.ops.create_order',
                   return_value={**OPS_RESPONSE, 'client': None}) as create, \
                patch('api.integrations.orders.fetch_client_by_phone', return_value=None), \
                patch('api.integrations.orders.send_confirmation'), \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            res = self.client.post(self.url, PAYLOAD, format='json')

        self.assertEqual(res.status_code, 201, res.data)
        ops_payload = create.call_args.args[0]
        self.assertNotIn('client_user_id', ops_payload)
        self.assertEqual(ops_payload['client_name'], 'Ana Pérez')
        self.assertEqual(ops_payload['client_phone'], '3001234567')

    def test_unlinked_client_prefers_custom_name(self):
        self.conversation.custom_name = 'Doña Ana'
        self.conversation.save(update_fields=['custom_name'])
        with patch('api.integrations.orders.ops.create_order',
                   return_value={**OPS_RESPONSE, 'client': None}) as create, \
                patch('api.integrations.orders.fetch_client_by_phone', return_value=None), \
                patch('api.integrations.orders.send_confirmation'), \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            res = self.client.post(self.url, PAYLOAD, format='json')

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(create.call_args.args[0]['client_name'], 'Doña Ana')

    def test_idempotency_key_replays_the_same_batch(self):
        with patch('api.integrations.orders.ops.create_order', return_value=OPS_RESPONSE) as create, \
                patch('api.integrations.orders.fetch_client_by_phone', return_value=dict(CLIENT_SNAPSHOT)), \
                patch('api.integrations.orders.send_confirmation'), \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            first = self.client.post(self.url, PAYLOAD, format='json')
            second = self.client.post(self.url, PAYLOAD, format='json')

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.data['created'])
        self.assertEqual(first.data['order']['id'], second.data['order']['id'])
        self.assertEqual(Order.objects.count(), 1)
        create.assert_called_once()

    def test_idempotency_in_progress_conflict(self):
        cache.set(f'order:idem:{self.user.id}:uuid-1', '__pending__', 60)
        with patch('api.integrations.orders.ops.create_order', return_value=OPS_RESPONSE), \
                patch('api.integrations.orders.fetch_client_by_phone', return_value=dict(CLIENT_SNAPSHOT)), \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            res = self.client.post(self.url, PAYLOAD, format='json')
        self.assertEqual(res.status_code, 409)
        self.assertEqual(Order.objects.count(), 0)

    def test_ops_failure_marks_batch_failed_and_allows_retry(self):
        with patch('api.integrations.orders.ops.create_order',
                   side_effect=ops.OpsAPIError('Ops respondió 500')), \
                patch('api.integrations.orders.fetch_client_by_phone', return_value=dict(CLIENT_SNAPSHOT)), \
                patch('api.integrations.orders.send_confirmation'), \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            res = self.client.post(self.url, PAYLOAD, format='json')

        self.assertEqual(res.status_code, 502)
        self.assertTrue(res.data['retry'])
        order = Order.objects.get()
        self.assertEqual(order.status, 'failed')
        self.assertEqual(res.data['order']['status'], 'failed')

        # The failure clears the idempotency marker so the agent can retry.
        with patch('api.integrations.orders.ops.create_order', return_value=OPS_RESPONSE), \
                patch('api.integrations.orders.fetch_client_by_phone', return_value=dict(CLIENT_SNAPSHOT)), \
                patch('api.integrations.orders.send_confirmation'), \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            retry = self.client.post(self.url, PAYLOAD, format='json')
        self.assertEqual(retry.status_code, 201)
        self.assertEqual(Order.objects.count(), 2)

    def test_validation_error_returns_field(self):
        payload = dict(PAYLOAD)
        payload['stops'] = [{'service_type': 'domicilio', 'dest_address': '', 'price': 8000}]
        res = self.client.post(self.url, payload, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['field'], 'stops')
        self.assertEqual(Order.objects.count(), 0)

    def test_serializer_errors_are_rejected(self):
        res = self.client.post(self.url, {'origin_address': '', 'stops': []}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('details', res.data)

    # ------------------------------------------------------------------
    #  Phase 8 — selector de domiciliario (asignación manual)
    # ------------------------------------------------------------------

    def test_create_order_with_courier_sends_manual_assignment(self):
        payload = {
            **PAYLOAD,
            'assignment': 'manual',
            'courier': dict(COURIER_INPUT),
        }
        with patch('api.integrations.orders.ops.create_order',
                   return_value=dict(OPS_RESPONSE_ASSIGNED)) as create, \
                patch('api.integrations.orders.fetch_client_by_phone',
                      return_value=dict(CLIENT_SNAPSHOT)), \
                patch('api.integrations.orders.send_confirmation') as confirm, \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            res = self.client.post(self.url, payload, format='json')

        self.assertEqual(res.status_code, 201, res.data)

        ops_payload = create.call_args.args[0]
        self.assertEqual(ops_payload['courier_user_id'], 1642)
        self.assertEqual(ops_payload['mode'], 'manual')

        order = Order.objects.get()
        self.assertEqual(order.status, 'asignado')
        self.assertEqual(order.ops_courier_user_id, 1642)
        self.assertEqual(order.courier_name, 'Samuel Niampira')
        self.assertEqual(order.courier_code, 'sn42')
        self.assertEqual(order.payload['request']['assignment'], 'manual')
        self.assertEqual(
            order.payload['request']['courier']['ops_courier_user_id'], 1642,
        )

        # El serializer expone el domi para el card y la hoja de detalle.
        self.assertEqual(res.data['order']['ops_courier_user_id'], 1642)
        self.assertEqual(res.data['order']['courier_name'], 'Samuel Niampira')
        self.assertEqual(res.data['order']['courier_code'], 'sn42')

        # La confirmación sigue saliendo (el aviso de asignado lo manda el
        # evento `order.created` de ops, Phase 5).
        confirm.assert_called_once()

    def test_libre_order_sends_no_courier(self):
        with patch('api.integrations.orders.ops.create_order',
                   return_value=dict(OPS_RESPONSE)) as create, \
                patch('api.integrations.orders.fetch_client_by_phone',
                      return_value=dict(CLIENT_SNAPSHOT)), \
                patch('api.integrations.orders.send_confirmation'), \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            res = self.client.post(self.url, PAYLOAD, format='json')

        self.assertEqual(res.status_code, 201, res.data)
        ops_payload = create.call_args.args[0]
        self.assertEqual(ops_payload['mode'], 'libre')
        self.assertNotIn('courier_user_id', ops_payload)
        order = Order.objects.get()
        self.assertIsNone(order.ops_courier_user_id)
        self.assertEqual(order.courier_code, '')

    def test_libre_assignment_ignores_a_stray_courier(self):
        """``assignment=libre`` never reaches ops as a manual assignment."""
        payload = {
            **PAYLOAD,
            'assignment': 'libre',
            'courier': dict(COURIER_INPUT),
        }
        with patch('api.integrations.orders.ops.create_order',
                   return_value=dict(OPS_RESPONSE)) as create, \
                patch('api.integrations.orders.fetch_client_by_phone',
                      return_value=dict(CLIENT_SNAPSHOT)), \
                patch('api.integrations.orders.send_confirmation'), \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            res = self.client.post(self.url, payload, format='json')

        self.assertEqual(res.status_code, 201, res.data)
        ops_payload = create.call_args.args[0]
        self.assertEqual(ops_payload['mode'], 'libre')
        self.assertNotIn('courier_user_id', ops_payload)
        order = Order.objects.get()
        self.assertIsNone(order.ops_courier_user_id)
        self.assertEqual(order.courier_code, '')
        self.assertIsNone(order.payload['request']['courier'])

    def test_manual_assignment_requires_a_courier(self):
        payload = {**PAYLOAD, 'assignment': 'manual'}
        res = self.client.post(self.url, payload, format='json')

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['field'], 'courier')
        self.assertEqual(Order.objects.count(), 0)

    def test_ops_validation_error_discards_the_draft_and_allows_retry(self):
        """Un 422 de ops (deuda del domi) es un dato corregible, no un fallido."""
        payload = {
            **PAYLOAD,
            'assignment': 'manual',
            'courier': dict(COURIER_INPUT),
        }
        validation_error = ops.OpsAPIError(
            'Ops respondió 422',
            status_code=422,
            payload={
                'ok': False,
                'error': 'Datos inválidos.',
                'details': {
                    'courier_user_id': ['No se puede asignar. Samuel tiene deuda de días anteriores.'],
                },
            },
        )
        with patch('api.integrations.orders.ops.create_order', side_effect=validation_error), \
                patch('api.integrations.orders.fetch_client_by_phone',
                      return_value=dict(CLIENT_SNAPSHOT)):
            res = self.client.post(self.url, payload, format='json')

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data['field'], 'courier')
        self.assertIn('deuda', res.data['error'])
        # Sin batch fallido local y con la llave liberada para reintentar.
        self.assertEqual(Order.objects.count(), 0)

        with patch('api.integrations.orders.ops.create_order',
                   return_value=dict(OPS_RESPONSE_ASSIGNED)), \
                patch('api.integrations.orders.fetch_client_by_phone',
                      return_value=dict(CLIENT_SNAPSHOT)), \
                patch('api.integrations.orders.send_confirmation'), \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            retry = self.client.post(self.url, payload, format='json')

        self.assertEqual(retry.status_code, 201, retry.data)
        self.assertEqual(Order.objects.count(), 1)


@override_settings(**OPS_SETTINGS)
class ConversationOrderListTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.group = get_or_create_tulua_group()
        self.user = User.objects.create_user(username='agent', password='x')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.conversation = Conversation.objects.create(
            whatsapp_id='573001234567', contact_name='Ana', group=self.group,
        )
        self.active = Order.objects.create(
            conversation=self.conversation, status='disponible', total=8000,
        )
        OrderStop.objects.create(
            order=self.active, stop_no=1, service_type='domicilio',
            dest_address='X', price=8000, status='disponible', ops_order_number=1,
        )
        self.finished = Order.objects.create(
            conversation=self.conversation, status='entregado', total=5000,
        )

    def test_list_defaults_to_active_orders(self):
        res = self.client.get(f'/api/conversations/{self.conversation.id}/orders/')
        self.assertEqual(res.status_code, 200)
        ids = [row['id'] for row in res.data['orders']]
        self.assertEqual(ids, [self.active.id])

    def test_list_include_finished(self):
        res = self.client.get(
            f'/api/conversations/{self.conversation.id}/orders/?include_finished=1',
        )
        self.assertEqual(res.status_code, 200)
        ids = {row['id'] for row in res.data['orders']}
        self.assertEqual(ids, {self.active.id, self.finished.id})

    def test_other_group_is_hidden(self):
        other_group = CityGroup.objects.create(name='Otra', slug='otra')
        other = User.objects.create_user(username='other', password='x')
        assign_user_group(other, other_group)
        token = Token.objects.create(user=other)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

        res = self.client.get(f'/api/conversations/{self.conversation.id}/orders/')
        self.assertEqual(res.status_code, 404)


@override_settings(**OPS_SETTINGS)
class OrderDetailRefreshTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.group = get_or_create_tulua_group()
        self.user = User.objects.create_user(username='agent', password='x')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.conversation = Conversation.objects.create(
            whatsapp_id='573001234567', contact_name='Ana', group=self.group,
        )
        self.order = Order.objects.create(
            conversation=self.conversation, status='disponible', total=8000,
        )
        self.stop = OrderStop.objects.create(
            order=self.order, stop_no=1, service_type='domicilio',
            dest_address='Cra 5 #12-01', price=8000, status='disponible',
            ops_order_number=1234,
        )

    def test_detail_returns_local_order(self):
        res = self.client.get(f'/api/orders/{self.order.id}/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['order']['id'], self.order.id)
        self.assertEqual(len(res.data['order']['stops']), 1)

    def test_refresh_pulls_ops_status(self):
        ops_order = {
            'ok': True, 'order_number': 1234, 'status': 'asignado',
            'status_label': 'Domiciliario asignado', 'courier': 'sn42',
            'stops': [{'stop': 1, 'service_type': 'domicilio', 'address': 'Cra 5 #12-01',
                       'description': '', 'status': 'asignado'}],
        }
        with patch('api.integrations.orders.ops.get_order', return_value=ops_order) as get, \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            res = self.client.post(f'/api/orders/{self.order.id}/refresh/')

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['order']['status'], 'asignado')
        self.assertEqual(res.data['order']['courier_code'], 'sn42')
        get.assert_called_once_with(1234)
        self.stop.refresh_from_db()
        self.assertEqual(self.stop.status, 'asignado')
        self.order.refresh_from_db()
        self.assertEqual(self.order.courier_code, 'sn42')

    def test_refresh_comanda_reads_per_stop_status_once(self):
        stop2 = OrderStop.objects.create(
            order=self.order, stop_no=2, service_type='compras',
            dest_address='', description='Mercado', price=3000,
            status='disponible', ops_order_number=1234,
        )
        ops_order = {
            'ok': True, 'order_number': 1234, 'status': 'en_ruta',
            'status_label': 'En camino',
            'stops': [
                {'stop': 1, 'service_type': 'domicilio', 'address': 'Cra 5 #12-01',
                 'description': '', 'status': 'entregado'},
                {'stop': 2, 'service_type': 'compras', 'address': '',
                 'description': 'Mercado', 'status': 'en_ruta'},
            ],
        }
        with patch('api.integrations.orders.ops.get_order', return_value=ops_order) as get, \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            res = self.client.post(f'/api/orders/{self.order.id}/refresh/')

        self.assertEqual(res.status_code, 200, res.data)
        # Una sola llamada a ops aunque las dos paradas compartan el número.
        get.assert_called_once_with(1234)
        self.stop.refresh_from_db()
        stop2.refresh_from_db()
        self.assertEqual(self.stop.status, 'entregado')
        self.assertEqual(stop2.status, 'en_ruta')
        self.assertEqual(res.data['order']['status'], 'en_ruta')

    def test_refresh_ops_error_returns_502(self):
        with patch('api.integrations.orders.ops.get_order',
                   side_effect=ops.OpsAPIError('Ops respondió 500')):
            res = self.client.post(f'/api/orders/{self.order.id}/refresh/')
        self.assertEqual(res.status_code, 502)

    def test_detail_hidden_for_other_group(self):
        other_group = CityGroup.objects.create(name='Otra', slug='otra2')
        other = User.objects.create_user(username='other', password='x')
        assign_user_group(other, other_group)
        token = Token.objects.create(user=other)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        res = self.client.get(f'/api/orders/{self.order.id}/')
        self.assertEqual(res.status_code, 404)


@override_settings(**OPS_SETTINGS)
class OrderProxyTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username='agent', password='x')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def test_services_catalog_is_cached(self):
        payload = {
            'ok': True,
            'services': [
                {'key': 'domicilio', 'name': 'Domicilio', 'requires_address': True},
                {'key': 'compras', 'name': 'Compras', 'requires_address': False},
            ],
        }
        with patch('api.order_views.ops.get_services', return_value=payload) as get:
            first = self.client.get('/api/orders/services/')
            second = self.client.get('/api/orders/services/')

        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(first.data['services']), 2)
        self.assertEqual(second.data['services'], first.data['services'])
        get.assert_called_once()
        self.assertIsNotNone(cache.get('ops:services:catalog'))

    def test_services_error_is_502_and_503(self):
        with patch('api.order_views.ops.get_services',
                   side_effect=ops.OpsAPIError('boom')):
            self.assertEqual(self.client.get('/api/orders/services/').status_code, 502)
        with patch('api.order_views.ops.get_services',
                   side_effect=ops.OpsNotConfigured('missing')):
            self.assertEqual(self.client.get('/api/orders/services/').status_code, 503)

    def test_tools_catalog_is_cached(self):
        tools = [{'key': 'canasta', 'label': 'Canasta'}]
        with patch('api.order_views.calculator.get_tools', return_value=tools) as get:
            first = self.client.get('/api/orders/tools/')
            second = self.client.get('/api/orders/tools/')

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data['tools'], tools)
        self.assertEqual(second.data['tools'], tools)
        get.assert_called_once()

    def test_geocode_search_proxies_and_skips_short_queries(self):
        results = [{'display_name': 'Cra 5 #12-01', 'place_id': 'abc'}]
        with patch('api.order_views.calculator.geocode_search', return_value=results) as search:
            res = self.client.post('/api/orders/geocode/search/', {'q': 'Cra 5'}, format='json')
            short = self.client.post('/api/orders/geocode/search/', {'q': 'C'}, format='json')

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['results'], results)
        self.assertEqual(short.data['results'], [])
        search.assert_called_once_with('Cra 5')

    def test_geocode_details(self):
        place = {'display_name': 'Cra 5 #12-01', 'lat': 4.08, 'lng': -76.19}
        with patch('api.order_views.calculator.geocode_details', return_value=place) as details:
            res = self.client.post('/api/orders/geocode/details/', {'place_id': 'abc'}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['place']['lat'], 4.08)
        details.assert_called_once_with('abc')

        missing = self.client.post('/api/orders/geocode/details/', {}, format='json')
        self.assertEqual(missing.status_code, 400)

    def test_quote_maps_service_types_and_shared_origin(self):
        quote = {'breakdown': {'total': 14500, 'segments': []}}
        with patch('api.order_views.calculator.calculate_price', return_value=quote) as calc:
            res = self.client.post('/api/orders/quote/', {
                'origin_address': 'Calle 10 #20-30',
                'origin_lat': 4.08, 'origin_lng': -76.19,
                'payment_method': 'efectivo',
                'profile': 'usuario_final',
                'tools': ['canasta'],
                'acompanante': False,
                'stops': [
                    {'service_type': 'domicilio', 'dest_address': 'Cra 5 #12-01'},
                    {'service_type': 'compras', 'dest_address': ''},
                ],
            }, format='json')

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['quote'], quote)
        payload = calc.call_args.args[0]
        self.assertEqual(payload['segments'][0]['service_type'], 'domicilios')
        self.assertEqual(payload['segments'][1]['service_type'], 'purchases')
        self.assertEqual(payload['segments'][0]['origin'], payload['segments'][1]['origin'])
        self.assertEqual(payload['segments'][0]['origin']['lat'], 4.08)
        self.assertEqual(payload['tools'], ['canasta'])

    def test_quote_rejects_unknown_service(self):
        res = self.client.post('/api/orders/quote/', {
            'origin_address': 'X',
            'stops': [{'service_type': 'teleportacion', 'dest_address': 'Y'}],
        }, format='json')
        self.assertEqual(res.status_code, 400)

    def test_quote_calculator_error(self):
        from api.integrations.calculator import CalculatorAPIError

        with patch('api.order_views.calculator.calculate_price',
                   side_effect=CalculatorAPIError('boom')):
            res = self.client.post('/api/orders/quote/', {
                'origin_address': 'X',
                'stops': [{'service_type': 'domicilio', 'dest_address': 'Y'}],
            }, format='json')
        self.assertEqual(res.status_code, 502)

    def test_couriers_proxy_passes_query_and_rows(self):
        rows = [
            {
                'id': 1642, 'name': 'Samuel Niampira', 'phone': '3001234567',
                'code': 'sn42', 'active': True, 'is_working': True,
                'is_paused': False, 'shift_order': 1, 'shift_suspended': False,
                'active_order': None, 'active_status': None,
                'status_label': 'Disponible',
            },
        ]
        with patch('api.order_views.ops.list_couriers', return_value={'ok': True, 'couriers': rows}) as get:
            res = self.client.get('/api/orders/couriers/?q=sam')
            all_rows = self.client.get('/api/orders/couriers/')

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['couriers'], rows)
        get.assert_any_call(query='sam')
        self.assertEqual(get.call_args_list[-1], mock.call(query=None))
        self.assertEqual(all_rows.status_code, 200)
        self.assertEqual(all_rows.data['couriers'], rows)

    def test_couriers_proxy_error_is_502(self):
        with patch('api.order_views.ops.list_couriers',
                   side_effect=ops.OpsAPIError('boom')):
            self.assertEqual(self.client.get('/api/orders/couriers/').status_code, 502)
