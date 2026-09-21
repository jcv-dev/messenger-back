"""Tests for the link-time order backfill (adoption.sync_active_orders).

Bug fixed (2026-09-21): an ops batch created before the conversation was linked
never matched an event, so it stayed invisible in ``Pedidos activos`` until its
next status change. Linking now adopts the client's active orders, suppressing
the notifications for states that already happened.
"""

import datetime
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from api.integrations import ops
from api.integrations.adoption import _lock_key, sync_active_orders
from api.integrations.couriers import courier_snapshot
from api.models import Conversation, Order, OrderStop
from api.tests import get_or_create_tulua_group

OPS_SETTINGS = dict(OPS_API_URL='https://ops.test', OPS_API_KEY='dmikey_x')
CLIENT_ID = 8473


def client_orders(*rows):
    return {'ok': True, 'orders': list(rows)}


ACTIVE_ROW = {
    'order_number': 39116,
    'batch_id': '68ada7bfd4ed0b80',
    'status': 'asignado',
    'status_label': 'Domiciliario asignado',
    'origin': 'Calle 10 #20-30',
    'total': 8100,
    'created_at': '2026-09-21 15:30:11',
    'stops': [
        {'stop': 1, 'service_type': 'compras', 'address': '',
         'description': 'Canasta', 'status': 'asignado'},
        {'stop': 2, 'service_type': 'domicilio', 'address': 'Cra 5 #12-01',
         'description': '', 'status': 'asignado'},
    ],
}

FINISHED_ROW = {
    'order_number': 38000,
    'batch_id': 'oldbatch',
    'status': 'entregado',
    'status_label': 'Entregado',
    'origin': 'Calle 1',
    'total': 5000,
    'created_at': '2026-09-20 10:00:00',
    'stops': [
        {'stop': 1, 'service_type': 'domicilio', 'address': 'Calle 1',
         'description': '', 'status': 'entregado'},
    ],
}

DETAIL = {
    'ok': True,
    'order_number': 39116,
    'status': 'asignado',
    'status_label': 'Domiciliario asignado',
    'origin': 'Calle 10 #20-30',
    'courier': {'id': 1642, 'name': 'Samuel Niampira', 'code': 'sn42'},
    'created_at': '2026-09-21 15:30:11',
    'stops': [
        {'stop': 1, 'service_type': 'compras', 'address': '',
         'description': 'Canasta', 'observation': '', 'status': 'asignado',
         'price': 8000, 'lat': None, 'lng': None},
        {'stop': 2, 'service_type': 'domicilio', 'address': 'Cra 5 #12-01',
         'description': '', 'observation': 'Timbre azul', 'status': 'asignado',
         'price': 100, 'lat': 4.08, 'lng': -76.19},
    ],
}

#: Payloads pushed before the 2026-09-21 ops fix: the detail endpoint only
#: carried the short code string. The backfill must keep working.
LEGACY_DETAIL = {**DETAIL, 'courier': 'sn42'}


class CourierSnapshotTests(SimpleTestCase):
    """The normalizer accepts the object shape and the legacy code string."""

    def test_object_keeps_id_name_and_code(self):
        self.assertEqual(
            courier_snapshot({'courier': {'id': 34, 'name': 'Luis Camilo Vargas', 'code': 'MV06'}}),
            {'id': 34, 'name': 'Luis Camilo Vargas', 'code': 'MV06'},
        )

    def test_legacy_string_becomes_code_only(self):
        self.assertEqual(courier_snapshot({'courier': 'sn42'}), {'code': 'sn42'})

    def test_top_level_courier_code_is_the_fallback(self):
        self.assertEqual(
            courier_snapshot({'courier': None, 'courier_code': 'MV06'}),
            {'code': 'MV06'},
        )

    def test_no_courier_is_empty(self):
        self.assertEqual(courier_snapshot({}), {})
        self.assertEqual(courier_snapshot({'courier': None}), {})
        self.assertEqual(courier_snapshot(None), {})


@override_settings(**OPS_SETTINGS)
class SyncActiveOrdersTests(TestCase):
    def setUp(self):
        cache.clear()
        self.group = get_or_create_tulua_group()
        self.conversation = Conversation.objects.create(
            whatsapp_id='573001234567', contact_name='Janne',
            contact_phone='573001234567', group=self.group,
            ops_client_user_id=CLIENT_ID,
            ops_client_match_source='manual',
            ops_client_snapshot={'id': CLIENT_ID, 'name': 'Janne'},
        )

    def _patch_ops(self, orders, detail=DETAIL):
        return (
            patch.object(ops, 'get_client_orders', return_value=orders),
            patch.object(ops, 'get_order', return_value=detail),
        )

    def test_adopts_active_and_skips_finished(self):
        orders_patch, detail_patch = self._patch_ops(client_orders(ACTIVE_ROW, FINISHED_ROW))
        with orders_patch, detail_patch:
            adopted = sync_active_orders(self.conversation)

        self.assertEqual(adopted, 1)
        order = Order.objects.get(conversation=self.conversation)
        self.assertEqual(order.ops_batch_id, '68ada7bfd4ed0b80')
        self.assertEqual(order.ops_client_user_id, CLIENT_ID)
        self.assertEqual(order.status, 'asignado')
        self.assertEqual(order.total, 8100)
        self.assertEqual(order.stops.count(), 2)
        self.assertFalse(OrderStop.objects.filter(ops_order_number=38000).exists())

    def test_detail_enriches_stops_and_courier(self):
        orders_patch, detail_patch = self._patch_ops(client_orders(ACTIVE_ROW))
        with orders_patch, detail_patch:
            sync_active_orders(self.conversation)

        order = Order.objects.get(conversation=self.conversation)
        self.assertEqual(order.ops_courier_user_id, 1642)
        self.assertEqual(order.courier_name, 'Samuel Niampira')
        self.assertEqual(order.courier_code, 'sn42')
        stop = order.stops.get(stop_no=2)
        self.assertEqual(stop.price, 100)
        self.assertEqual(stop.lat, 4.08)
        self.assertEqual(stop.lng, -76.19)
        self.assertEqual(stop.observation, 'Timbre azul')

    def test_detail_accepts_legacy_courier_string(self):
        orders_patch, detail_patch = self._patch_ops(
            client_orders(ACTIVE_ROW), detail=LEGACY_DETAIL,
        )
        with orders_patch, detail_patch:
            sync_active_orders(self.conversation)

        order = Order.objects.get(conversation=self.conversation)
        self.assertEqual(order.courier_code, 'sn42')
        self.assertEqual(order.courier_name, '')
        self.assertIsNone(order.ops_courier_user_id)

    def test_detail_without_courier_keeps_mirror_clean(self):
        detail = {**DETAIL, 'courier': None}
        orders_patch, detail_patch = self._patch_ops(client_orders(ACTIVE_ROW), detail=detail)
        with orders_patch, detail_patch:
            sync_active_orders(self.conversation)

        order = Order.objects.get(conversation=self.conversation)
        self.assertEqual(order.courier_code, '')
        self.assertEqual(order.courier_name, '')
        self.assertIsNone(order.ops_courier_user_id)

    def test_created_at_kept_from_ops_bogota_time(self):
        orders_patch, detail_patch = self._patch_ops(client_orders(ACTIVE_ROW))
        with orders_patch, detail_patch:
            sync_active_orders(self.conversation)

        order = Order.objects.get(conversation=self.conversation)
        created = order.created_at.astimezone(datetime.timezone.utc)
        self.assertEqual(
            (created.year, created.month, created.day, created.hour, created.minute),
            (2026, 9, 21, 20, 30),
        )

    def test_suppresses_transition_already_in_effect(self):
        from api.integrations.status_notifications import maybe_notify

        orders_patch, detail_patch = self._patch_ops(client_orders(ACTIVE_ROW))
        with orders_patch, detail_patch:
            sync_active_orders(self.conversation)

        order = Order.objects.get(conversation=self.conversation)
        self.assertEqual(order.notified_statuses, ['asignado'])
        with patch(
            'api.integrations.status_notifications.send_status_notification'
        ) as send:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertIsNone(maybe_notify(order))
        send.assert_not_called()

    def test_next_transition_still_notifies(self):
        from api.integrations.status_notifications import maybe_notify

        orders_patch, detail_patch = self._patch_ops(client_orders(ACTIVE_ROW))
        with orders_patch, detail_patch:
            sync_active_orders(self.conversation)

        order = Order.objects.get(conversation=self.conversation)
        order.stops.update(status='en_ruta')
        with patch(
            'api.integrations.status_notifications.send_status_notification'
        ) as send:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(maybe_notify(order), 'en_ruta')
        send.assert_called_once()

    def test_idempotent(self):
        orders_patch, detail_patch = self._patch_ops(client_orders(ACTIVE_ROW))
        with orders_patch, detail_patch:
            self.assertEqual(sync_active_orders(self.conversation), 1)

        cache.delete(_lock_key(self.conversation.id))
        orders_patch, detail_patch = self._patch_ops(client_orders(ACTIVE_ROW))
        with orders_patch, detail_patch:
            self.assertEqual(sync_active_orders(self.conversation), 0)
        self.assertEqual(Order.objects.count(), 1)

    def test_detail_failure_falls_back_to_client_orders_row(self):
        with patch.object(ops, 'get_client_orders', return_value=client_orders(ACTIVE_ROW)):
            with patch.object(ops, 'get_order', side_effect=ops.OpsAPIError('down')):
                self.assertEqual(sync_active_orders(self.conversation), 1)

        order = Order.objects.get(conversation=self.conversation)
        self.assertEqual(order.total, 8100)
        stop = order.stops.get(stop_no=2)
        self.assertEqual(stop.dest_address, 'Cra 5 #12-01')
        self.assertEqual(stop.price, 0)

    def test_ops_failure_is_silent(self):
        with patch.object(ops, 'get_client_orders', side_effect=ops.OpsAPIError('down')):
            self.assertEqual(sync_active_orders(self.conversation), 0)
        self.assertFalse(Order.objects.exists())

    def test_skips_unlinked_conversation(self):
        self.conversation.ops_client_user_id = None
        self.conversation.save(update_fields=['ops_client_user_id'])

        with patch.object(ops, 'get_client_orders') as lookup:
            self.assertEqual(sync_active_orders(self.conversation), 0)
        lookup.assert_not_called()

    @override_settings(OPS_API_URL='', OPS_API_KEY='')
    def test_skips_when_ops_not_configured(self):
        self.assertEqual(sync_active_orders(self.conversation), 0)
        self.assertFalse(Order.objects.exists())

    def test_concurrent_sync_is_coalesced(self):
        orders_patch, detail_patch = self._patch_ops(client_orders(ACTIVE_ROW))
        with orders_patch, detail_patch:
            self.assertEqual(sync_active_orders(self.conversation), 1)
        # Lock still held: a second sync right after the first is a no-op.
        orders_patch, detail_patch = self._patch_ops(client_orders(ACTIVE_ROW))
        with orders_patch, detail_patch:
            self.assertEqual(sync_active_orders(self.conversation), 0)


@override_settings(**OPS_SETTINGS)
class LinkClientBackfillTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username='agent', password='x')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.group = get_or_create_tulua_group()
        self.conversation = Conversation.objects.create(
            whatsapp_id='573001234567', contact_name='Janne',
            contact_phone='573001234567', group=self.group,
        )

    def _link(self):
        return self.client.post(
            f'/api/conversations/{self.conversation.id}/link-client/',
            {'ops_client_user_id': CLIENT_ID,
             'client': {'name': 'Janne', 'phone': '', 'address': ''}},
            format='json',
        )

    def test_link_endpoint_backfills_orders(self):
        with patch.object(ops, 'get_client_orders', return_value=client_orders(ACTIVE_ROW)):
            with patch.object(ops, 'get_order', return_value=DETAIL):
                with patch('api.views.publish_conversation_update'):
                    with patch('api.integrations.views._publish_order_updated') as publish:
                        with self.captureOnCommitCallbacks(execute=True):
                            response = self._link()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Order.objects.filter(conversation=self.conversation).exists())
        publish.assert_called_once()

    def test_link_endpoint_survives_ops_failure(self):
        with patch.object(ops, 'get_client_orders', side_effect=ops.OpsAPIError('down')):
            with patch('api.views.publish_conversation_update'):
                response = self._link()

        self.assertEqual(response.status_code, 200)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.ops_client_user_id, CLIENT_ID)
        self.assertFalse(Order.objects.exists())

    def test_unlink_does_not_backfill(self):
        self.conversation.ops_client_user_id = CLIENT_ID
        self.conversation.save(update_fields=['ops_client_user_id'])

        with patch.object(ops, 'get_client_orders') as lookup:
            with patch('api.views.publish_conversation_update'):
                response = self.client.post(
                    f'/api/conversations/{self.conversation.id}/link-client/',
                    {}, format='json',
                )
        self.assertEqual(response.status_code, 200)
        lookup.assert_not_called()

    def test_auto_link_backfills_orders(self):
        from api.integrations.clients import auto_link_now

        with patch.object(
            ops, 'get_client_by_phone',
            return_value={'ok': True, 'found': True, 'type': 'cliente',
                          'id': CLIENT_ID, 'name': 'Janne', 'phone': '',
                          'address': ''},
        ):
            with patch.object(ops, 'get_client_orders', return_value=client_orders(ACTIVE_ROW)):
                with patch.object(ops, 'get_order', return_value=DETAIL):
                    with patch('api.views.publish_conversation_update'):
                        linked = auto_link_now(self.conversation.id, '573001234567')

        self.assertTrue(linked)
        self.assertTrue(Order.objects.filter(
            conversation=self.conversation, ops_client_user_id=CLIENT_ID,
        ).exists())


@override_settings(**OPS_SETTINGS)
class SyncActiveOrdersCommandTests(TestCase):
    def setUp(self):
        cache.clear()
        self.group = get_or_create_tulua_group()
        self.linked = Conversation.objects.create(
            whatsapp_id='573001234567', contact_name='Janne',
            contact_phone='573001234567', group=self.group,
            ops_client_user_id=CLIENT_ID,
        )
        self.unlinked = Conversation.objects.create(
            whatsapp_id='573009999999', contact_name='Luis',
            contact_phone='573009999999', group=self.group,
        )

    def test_requires_ops_configuration(self):
        with override_settings(OPS_API_URL='', OPS_API_KEY=''):
            with self.assertRaises(CommandError):
                call_command('sync_active_orders')

    def test_command_adopts_missing_orders(self):
        out = StringIO()
        with patch.object(ops, 'get_client_orders', return_value=client_orders(ACTIVE_ROW)):
            with patch.object(ops, 'get_order', return_value=DETAIL):
                call_command('sync_active_orders', stdout=out)

        self.assertIn('1 conversaciones revisadas, 1 pedido(s) adoptado(s)', out.getvalue())
        self.assertTrue(Order.objects.filter(conversation=self.linked).exists())
        self.assertFalse(Order.objects.filter(conversation=self.unlinked).exists())

    def test_conversation_filter(self):
        out = StringIO()
        with patch.object(ops, 'get_client_orders', return_value=client_orders()) as lookup:
            call_command(
                'sync_active_orders',
                '--conversation', str(self.unlinked.id), stdout=out,
            )
        lookup.assert_not_called()
        self.assertIn('0 pedido(s) adoptado(s)', out.getvalue())
