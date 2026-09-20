"""Tests for Phase 4 — order cancellation (plan §4.3, §6.3)."""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from api.integrations import ops
from api.integrations.orders import OrderCancelError, validate_cancel_reason
from api.models import AuditLog, CityGroup, Conversation, ConversationNote, Order, OrderStop
from api.tests import assign_user_group, get_or_create_tulua_group

OPS_SETTINGS = dict(OPS_API_URL='https://ops.test', OPS_API_KEY='dmikey_x')

REASON = 'El cliente canceló por WhatsApp'


class CancelReasonUnitTests(TestCase):
    def test_validate_cancel_reason_trims_and_caps(self):
        self.assertEqual(validate_cancel_reason('  corto motivo  '), 'corto motivo')
        self.assertEqual(len(validate_cancel_reason('x' * 900)), 500)

    def test_validate_cancel_reason_rejects_empty_and_short(self):
        for value in ('', '  ', 'no', None):
            with self.assertRaises(OrderCancelError):
                validate_cancel_reason(value)


@override_settings(**OPS_SETTINGS)
class OrderCancelEndpointTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.group = get_or_create_tulua_group()
        self.user = User.objects.create_user(username='agent', password='x')
        assign_user_group(self.user, self.group)
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.conversation = Conversation.objects.create(
            whatsapp_id='573001234567', contact_name='Ana Pérez',
            contact_phone='573001234567', group=self.group,
        )

        self.order = Order.objects.create(
            conversation=self.conversation,
            ops_batch_id='a1b2c3d4e5f60718',
            ops_client_user_id=88,
            client_name='Ana Pérez',
            origin_address='Calle 10 #20-30',
            total=14500,
            status='disponible',
            created_by=self.user,
            payload={'request': {'idempotency_key': 'uuid-1'}},
        )
        self.stop1 = OrderStop.objects.create(
            order=self.order, stop_no=1, ops_order_number=1234,
            service_type='domicilio', dest_address='Cra 5 #12-01',
            price=8000, status='disponible',
        )
        self.stop2 = OrderStop.objects.create(
            order=self.order, stop_no=2, ops_order_number=1234,
            service_type='compras', dest_address='Calle 20 #3-10',
            price=6500, status='asignado',
        )
        self.url = f'/api/orders/{self.order.id}/cancel/'

    def _post(self, url=None, payload=None):
        return self.client.post(url or self.url, payload or {'reason': REASON}, format='json')

    def test_cancel_whole_order_calls_ops_once_and_cancels_all_stops(self):
        with patch('api.integrations.orders.ops.cancel_order',
                   return_value={'ok': True, 'order_number': 1234, 'canceled': 2,
                                 'status': 'cancelado'}) as cancel, \
                patch('api.integrations.views._publish_order_updated') as publish_order, \
                patch('api.views.publish_conversation_update') as publish_conv:
            res = self._post()

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data['ok'])
        self.assertEqual(res.data['canceled'], 2)

        cancel.assert_called_once_with(1234, REASON)

        self.stop1.refresh_from_db()
        self.stop2.refresh_from_db()
        self.assertEqual(self.stop1.status, 'cancelado')
        self.assertEqual(self.stop2.status, 'cancelado')
        self.assertIsNotNone(self.stop1.canceled_at)
        self.assertEqual(self.stop1.cancel_reason, REASON)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'cancelado')
        self.assertEqual(int(self.order.total), 14500)
        self.assertIn(f'[Pedido] Cancelado: {REASON}', self.order.payload['cancel'])

        # Response carries the fresh stops.
        statuses = {stop['stop_no']: stop['status'] for stop in res.data['order']['stops']}
        self.assertEqual(statuses, {1: 'cancelado', 2: 'cancelado'})

        # Audit log only: cancellation never creates a conversation note.
        self.assertFalse(ConversationNote.objects.exists())

        audit = AuditLog.objects.get(action='cancel_order')
        self.assertEqual(audit.actor, self.user)
        self.assertEqual(audit.conversation, self.conversation)
        self.assertIn(REASON, audit.detail)

        publish_order.assert_called_once()
        publish_conv.assert_called_once()

    def test_cancel_order_is_idempotent_when_no_active_stops(self):
        self.stop1.status = 'cancelado'
        self.stop1.save(update_fields=['status'])
        self.stop2.status = 'entregado'
        self.stop2.save(update_fields=['status'])

        with patch('api.integrations.orders.ops.cancel_order') as cancel, \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            res = self._post()

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['canceled'], 0)
        cancel.assert_not_called()
        self.assertFalse(ConversationNote.objects.exists())
        self.assertFalse(AuditLog.objects.exists())

    def test_cancel_failed_order_without_ops_number_cancels_locally(self):
        self.order.ops_batch_id = None
        self.order.status = 'failed'
        self.order.save(update_fields=['ops_batch_id', 'status'])
        OrderStop.objects.filter(order=self.order).update(ops_order_number=None, status='pending')

        with patch('api.integrations.orders.ops.cancel_order') as cancel, \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            res = self._post()

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['canceled'], 2)
        cancel.assert_not_called()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'cancelado')
        self.assertEqual(
            OrderStop.objects.filter(order=self.order, status='cancelado').count(), 2,
        )

    def test_cancel_order_ops_failure_returns_502_and_keeps_state(self):
        with patch('api.integrations.orders.ops.cancel_order',
                   side_effect=ops.OpsAPIError('Ops respondió 500')) as cancel, \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            res = self._post()

        self.assertEqual(res.status_code, 502, res.data)
        self.assertFalse(res.data['ok'])
        cancel.assert_called_once_with(1234, REASON)

        self.stop1.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.stop1.status, 'disponible')
        self.assertEqual(self.order.status, 'disponible')
        self.assertFalse(ConversationNote.objects.exists())
        self.assertFalse(AuditLog.objects.exists())

    def test_cancel_order_validates_reason(self):
        with patch('api.integrations.orders.ops.cancel_order') as cancel:
            missing = self.client.post(self.url, {}, format='json')
            short = self.client.post(self.url, {'reason': 'no'}, format='json')

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.data['field'], 'reason')
        self.assertEqual(short.status_code, 400)
        cancel.assert_not_called()

    def test_cancel_order_requires_auth(self):
        self.client.credentials()
        res = self.client.post(self.url, {'reason': REASON}, format='json')
        self.assertEqual(res.status_code, 401)

    def test_cancel_order_hidden_for_other_group(self):
        other_group = CityGroup.objects.create(name='Other', slug='other')
        other_user = User.objects.create_user(username='other', password='x')
        assign_user_group(other_user, other_group)
        token = Token.objects.create(user=other_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

        with patch('api.integrations.orders.ops.cancel_order') as cancel:
            res = self._post()

        self.assertEqual(res.status_code, 404)
        cancel.assert_not_called()

    # ------------------------------------------------------------------
    #  Per-stop cancellation
    # ------------------------------------------------------------------

    def test_cancel_stop_calls_ops_stop_endpoint_and_recomputes(self):
        url = f'/api/orders/{self.order.id}/stops/{self.stop2.id}/cancel/'
        with patch('api.integrations.orders.ops.cancel_order_stop',
                   return_value={'ok': True, 'order_number': 1234, 'stop': 2,
                                 'canceled': 1, 'status': 'cancelado'}) as cancel, \
                patch('api.integrations.views._publish_order_updated') as publish_order, \
                patch('api.views.publish_conversation_update'):
            res = self._post(url)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['canceled'], 1)
        cancel.assert_called_once_with(1234, 2, REASON)

        self.stop1.refresh_from_db()
        self.stop2.refresh_from_db()
        self.assertEqual(self.stop1.status, 'disponible')
        self.assertEqual(self.stop2.status, 'cancelado')
        self.assertEqual(self.stop2.cancel_reason, REASON)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'disponible')
        self.assertIn(f'[Pedido] Parada 2 cancelada: {REASON}', self.order.payload['cancel'])

        self.assertFalse(ConversationNote.objects.exists())
        audit = AuditLog.objects.get(action='cancel_order')
        self.assertIn('parada 2', audit.detail)
        publish_order.assert_called_once()

    def test_cancel_stop_all_canceled_marks_order_canceled(self):
        self.stop1.status = 'cancelado'
        self.stop1.save(update_fields=['status'])

        url = f'/api/orders/{self.order.id}/stops/{self.stop2.id}/cancel/'
        with patch('api.integrations.orders.ops.cancel_order_stop',
                   return_value={'ok': True}), \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            res = self._post(url)

        self.assertEqual(res.status_code, 200, res.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'cancelado')

    def test_cancel_stop_is_idempotent_for_terminal_stop(self):
        self.stop2.status = 'cancelado'
        self.stop2.save(update_fields=['status'])

        url = f'/api/orders/{self.order.id}/stops/{self.stop2.id}/cancel/'
        with patch('api.integrations.orders.ops.cancel_order_stop') as cancel, \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            res = self._post(url)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['canceled'], 0)
        cancel.assert_not_called()
        self.assertFalse(ConversationNote.objects.exists())

    def test_cancel_stop_uses_its_own_legacy_number(self):
        self.stop2.ops_order_number = 1235
        self.stop2.save(update_fields=['ops_order_number'])

        url = f'/api/orders/{self.order.id}/stops/{self.stop2.id}/cancel/'
        with patch('api.integrations.orders.ops.cancel_order_stop',
                   return_value={'ok': True}) as cancel, \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            res = self._post(url)

        self.assertEqual(res.status_code, 200, res.data)
        cancel.assert_called_once_with(1235, 2, REASON)

    def test_cancel_stop_ops_failure_returns_502(self):
        url = f'/api/orders/{self.order.id}/stops/{self.stop2.id}/cancel/'
        with patch('api.integrations.orders.ops.cancel_order_stop',
                   side_effect=ops.OpsAPIError('Ops respondió 500')), \
                patch('api.integrations.views._publish_order_updated'), \
                patch('api.views.publish_conversation_update'):
            res = self._post(url)

        self.assertEqual(res.status_code, 502, res.data)
        self.stop2.refresh_from_db()
        self.assertEqual(self.stop2.status, 'asignado')
        self.assertFalse(ConversationNote.objects.exists())

    def test_cancel_stop_unknown_stop_returns_404(self):
        url = f'/api/orders/{self.order.id}/stops/99999/cancel/'
        res = self._post(url)
        self.assertEqual(res.status_code, 404)
