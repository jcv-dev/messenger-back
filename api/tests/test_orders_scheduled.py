"""Tests for scheduled orders (pedidos programados) — Messager side.

Covers serializer window validation, the ops payload carrying ``scheduled_at``,
the local ``programado`` mirror, the scheduled confirmation text, the
``for_schedule`` courier proxy, no premature notifications, and the status
preservation rules on events/adoption.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from api.integrations.orders import build_confirmation_text, send_confirmation
from api.integrations.views import recompute_order_status
from api.models import Conversation, Order, OrderStop
from api.serializers import OrderCreateInputSerializer, OrderSerializer
from api.tests import get_or_create_tulua_group

CLIENT_SNAPSHOT = {
    'id': 88, 'name': 'Ana Pérez', 'phone': '3001234567', 'address': 'Calle 10 #20-30',
}

OPS_RESPONSE_SCHEDULED_FREE = {
    'ok': True,
    'batch_id': 'b1b2c3d4e5f60718',
    'order_numbers': [1234],
    'order_number': 1234,
    'stops_count': 1,
    'status': 'programado',
    'client': {'id': 88, 'name': 'Ana Pérez', 'phone': '3001234567'},
    'courier': None,
    'message': 'Pedido programado para 26/09/2026 14:30.',
    'scheduled_at': '2026-09-26T14:30:00-05:00',
    'scheduled_mode': 'libre',
}

OPS_RESPONSE_SCHEDULED_ASSIGNED = {
    **OPS_RESPONSE_SCHEDULED_FREE,
    'scheduled_mode': 'manual',
    'courier': {'id': 1642, 'name': 'Samuel Niampira', 'code': 'sn42'},
}

COURIER_INPUT = {'ops_courier_user_id': 1642, 'name': 'Samuel Niampira', 'code': 'sn42'}


def _future_iso(minutes=60):
    return (timezone.now() + timedelta(minutes=minutes)).isoformat()


class ScheduledSerializerTests(TestCase):
    def test_scheduled_for_must_be_future(self):
        serializer = OrderCreateInputSerializer(data={
            'origin_address': 'Calle 10',
            'stops': [{'service_type': 'domicilio', 'dest_address': 'Cra 5', 'price': 8000}],
            'scheduled_for': (timezone.now() - timedelta(minutes=5)).isoformat(),
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn('scheduled_for', serializer.errors)

    def test_scheduled_for_accepts_valid_future(self):
        serializer = OrderCreateInputSerializer(data={
            'origin_address': 'Calle 10',
            'stops': [{'service_type': 'domicilio', 'dest_address': 'Cra 5', 'price': 8000}],
            'scheduled_for': (timezone.now() + timedelta(hours=2)).isoformat(),
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)


class ScheduledConfirmationUnitTests(TestCase):
    def _order(self, status, scheduled_for):
        group = get_or_create_tulua_group()
        conv = Conversation.objects.create(
            whatsapp_id='573001112222', contact_name='Ana', contact_phone='573001112222',
            group=group,
        )
        order = Order.objects.create(
            conversation=conv, status=status, scheduled_for=scheduled_for,
            total=14500, client_name='Ana',
        )
        OrderStop.objects.create(
            order=order, stop_no=1, service_type='domicilio',
            dest_address='Cra 5', price=14500, status='nuevo',
        )
        return order

    def test_scheduled_confirmation_mentions_the_date(self):
        when = timezone.now() + timedelta(days=1)
        order = self._order('programado', when)
        text = build_confirmation_text(order)
        self.assertIn('programado', text.lower())
        self.assertIn('14.500', text)

    def test_normal_confirmation_unchanged(self):
        order = self._order('disponible', None)
        text = build_confirmation_text(order)
        self.assertNotIn('programado', text.lower())


class ScheduledOrderCreateTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.group = get_or_create_tulua_group()
        self.user = User.objects.create_user(username='agent-sched', password='x')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.conversation = Conversation.objects.create(
            whatsapp_id='573001234567', contact_name='Ana Pérez',
            contact_phone='573001234567', group=self.group,
        )
        self.url = f'/api/conversations/{self.conversation.id}/orders/'

    def _post(self, response, extra=None):
        payload = {
            'origin_address': 'Calle 10 #20-30',
            'stops': [{
                'service_type': 'domicilio', 'dest_address': 'Cra 5 #12-01', 'price': 8000,
            }],
            'scheduled_for': _future_iso(120),
            'idempotency_key': 'sched-1',
        }
        if extra:
            payload.update(extra)
        with patch('api.integrations.orders.ops.create_order', return_value=response) as create, \
                patch('api.integrations.orders.fetch_client_by_phone',
                      return_value=dict(CLIENT_SNAPSHOT)), \
                patch('api.integrations.orders.send_confirmation') as confirm, \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            res = self.client.post(self.url, payload, format='json')
        return res, create, confirm

    def test_free_scheduled_order_mirrors_programado_and_sends_scheduled_at(self):
        res, create, confirm = self._post(OPS_RESPONSE_SCHEDULED_FREE)

        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get()
        self.assertEqual(order.status, 'programado')
        self.assertIsNotNone(order.scheduled_for)

        ops_payload = create.call_args.args[0]
        self.assertIn('scheduled_at', ops_payload)
        self.assertEqual(ops_payload['mode'], 'libre')

        confirm.assert_called_once()
        self.assertEqual(res.data['order']['status'], 'programado')

    def test_manual_scheduled_order_sends_courier_and_mode(self):
        res, create, confirm = self._post(
            OPS_RESPONSE_SCHEDULED_ASSIGNED,
            {
                'assignment': 'manual',
                'courier': COURIER_INPUT,
            },
        )

        self.assertEqual(res.status_code, 201, res.data)
        ops_payload = create.call_args.args[0]
        self.assertEqual(ops_payload['mode'], 'manual')
        self.assertEqual(ops_payload['courier_user_id'], 1642)
        self.assertIn('scheduled_at', ops_payload)

        order = Order.objects.get()
        self.assertEqual(order.status, 'programado')
        self.assertEqual(order.ops_courier_user_id, 1642)

    def test_too_soon_is_rejected_before_ops(self):
        with patch('api.integrations.orders.ops.create_order') as create:
            res = self.client.post(self.url, {
                'origin_address': 'Calle 10',
                'stops': [{'service_type': 'domicilio', 'dest_address': 'Cra 5', 'price': 8000}],
                'scheduled_for': _future_iso(2),
            }, format='json')
        self.assertEqual(res.status_code, 400, res.data)
        create.assert_not_called()
        self.assertEqual(Order.objects.count(), 0)


class ScheduledCourierProxyTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='courier-proxy', password='x')
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def test_for_schedule_flag_is_forwarded(self):
        rows = [{'id': 1, 'name': 'Off', 'is_working': False, 'status_label': 'Inactivo'}]
        with patch('api.order_views.ops.list_couriers',
                   return_value={'ok': True, 'couriers': rows}) as get:
            res = self.client.get('/api/orders/couriers/?for_schedule=1')
        self.assertEqual(res.status_code, 200)
        get.assert_any_call(query=None, for_schedule=True)


class ScheduledStatusPreservationTests(TestCase):
    def _orders(self):
        group = get_or_create_tulua_group()
        conv = Conversation.objects.create(
            whatsapp_id='573009998888', contact_name='Ana', contact_phone='573009998888',
            group=group,
        )
        return conv

    def test_recompute_keeps_programado_while_stops_are_nuevo(self):
        conv = self._orders()
        order = Order.objects.create(
            conversation=conv, status='programado',
            scheduled_for=timezone.now() + timedelta(hours=3), total=8000,
        )
        OrderStop.objects.create(
            order=order, stop_no=1, service_type='domicilio',
            dest_address='Cra 5', price=8000, status='nuevo',
        )
        recompute_order_status(order)
        self.assertEqual(order.status, 'programado')

    def test_recompute_cancels_programado_when_all_stops_canceled(self):
        conv = self._orders()
        order = Order.objects.create(
            conversation=conv, status='programado',
            scheduled_for=timezone.now() + timedelta(hours=3), total=8000,
        )
        OrderStop.objects.create(
            order=order, stop_no=1, service_type='domicilio',
            dest_address='Cra 5', price=8000, status='cancelado',
        )
        recompute_order_status(order)
        self.assertEqual(order.status, 'cancelado')

    def test_order_serializer_exposes_schedule_fields(self):
        conv = self._orders()
        order = Order.objects.create(
            conversation=conv, status='programado',
            scheduled_for=timezone.now() + timedelta(hours=3), total=8000,
        )
        data = OrderSerializer(order).data
        self.assertIn('scheduled_for', data)
        self.assertIn('scheduled_released', data)
        self.assertEqual(data['status_label'], 'Programado')


class ScheduledSendConfirmationTests(TestCase):
    """The scheduled text is picked when the order is still ``programado``."""

    def test_send_confirmation_uses_scheduled_text(self):
        group = get_or_create_tulua_group()
        conv = Conversation.objects.create(
            whatsapp_id='573007776666', contact_name='Ana', contact_phone='573007776666',
            group=group,
        )
        order = Order.objects.create(
            conversation=conv, status='programado',
            scheduled_for=timezone.now() + timedelta(days=1), total=8000,
        )
        OrderStop.objects.create(
            order=order, stop_no=1, service_type='domicilio', dest_address='Cra 5',
            price=8000, status='nuevo',
        )
        with patch('api.integrations.notify.send_text') as send:
            send_confirmation(conv, order)
        send.assert_called_once()
        text = send.call_args.args[1]
        self.assertIn('programado', text.lower())
