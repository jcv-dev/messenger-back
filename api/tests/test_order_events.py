"""Tests for POST /api/integrations/orders/events/ (plan §4.2)."""

import json
import time
from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from api.integrations.auth import generate_api_key
from api.integrations.hmac import compute_signature
from api.models import Conversation, IntegrationApiKey, Order, OrderStop

SECRET = 'test-webhook-secret'
URL = '/api/integrations/orders/events/'


@override_settings(INTEGRATION_WEBHOOK_SECRET=SECRET)
class OrderEventsTests(APITestCase):
    def setUp(self):
        cache.clear()
        raw, key_hash, prefix = generate_api_key()
        self.key = raw
        self.api_key = IntegrationApiKey.objects.create(
            name='ops', key_hash=key_hash, prefix=prefix,
            scopes=['orders:write'],
        )
        self.conversation = Conversation.objects.create(
            whatsapp_id='573001234567', contact_name='Ana Pérez',
            contact_phone='573001234567',
        )
        self.order = Order.objects.create(
            conversation=self.conversation,
            ops_client_user_id=88,
            client_name='Ana Pérez',
            origin_address='Calle 10 #20-30',
            status='pending',
            source='agent',
        )

    def post_event(self, payload):
        body = json.dumps(payload, separators=(',', ':'))
        headers = {
            'HTTP_X_API_KEY': self.key,
            'HTTP_X_SIGNATURE': 'sha256=' + compute_signature(SECRET, body),
            'HTTP_X_TIMESTAMP': str(int(time.time())),
        }
        with patch('api.integrations.views._publish_order_updated') as publish:
            # Phase 5 may send client notifications; keep WhatsApp out of tests.
            with patch('api.views._send_pool'):
                with self.captureOnCommitCallbacks(execute=True):
                    res = self.client.post(
                        URL, body, content_type='application/json', **headers,
                    )
        return res, publish

    def event(self, order_number, status, **overrides):
        payload = {
            'event': 'order.status_changed',
            'batch_id': 'a1b2c3d4e5f60718',
            'order_number': order_number,
            'status': status,
            'status_label': status,
            'client': {'id': 88, 'name': 'Ana Pérez', 'phone': '3001234567'},
            'origin': 'Calle 10 #20-30',
            'total': 8000,
            'courier_code': 'sn42',
            'occurred_at': timezone.now().isoformat(),
            'stops': [
                {'stop': 1, 'service_type': 'domicilio', 'address': 'Cra 5 #12-01',
                 'description': 'Paquete', 'status': status},
            ],
        }
        payload.update(overrides)
        return payload

    def make_stops(self, count=2):
        stops = []
        for i in range(1, count + 1):
            stops.append(OrderStop.objects.create(
                order=self.order, stop_no=i,
                ops_order_number=1233 + i,
                service_type='domicilio',
                dest_address=f'Destino {i}',
                price=8000,
                status='pending',
            ))
        return stops

    # ── Locating ────────────────────────────────────────────────────────

    def test_unknown_order_returns_linked_false(self):
        res, _ = self.post_event(self.event(999999, 'asignado'))
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['ok'])
        self.assertFalse(res.json()['linked'])

    def test_updates_stop_by_order_number_and_recomputes_single_stop(self):
        stop = self.make_stops(1)[0]
        res, publish = self.post_event(self.event(1234, 'asignado'))

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['linked'])
        self.assertEqual(res.json()['batch_status'], 'asignado')
        # Phase 5: a single-stop batch notifies the client on the asignado
        # transition (further repeats stay deduped below).
        self.assertTrue(res.json()['notified'])
        self.assertEqual(res.json()['notified_status'], 'asignado')

        stop.refresh_from_db()
        self.assertEqual(stop.status, 'asignado')
        self.assertEqual(stop.payload['courier_code'], 'sn42')
        self.assertIsNotNone(stop.last_synced_at)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'asignado')
        # Phase 8: el código del domi también queda a nivel de pedido para el card.
        self.assertEqual(self.order.courier_code, 'sn42')
        self.assertEqual(self.order.ops_batch_id, 'a1b2c3d4e5f60718')
        self.assertEqual(self.order.ops_client_user_id, 88)
        self.assertIsNotNone(self.order.last_synced_at)

        self.assertEqual(publish.call_count, 1)

    def test_links_stop_by_batch_id_and_assigns_order_number(self):
        stop = self.make_stops(1)[0]
        stop.ops_order_number = None
        stop.save(update_fields=['ops_order_number'])

        res, _ = self.post_event(self.event(5000, 'disponible'))

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['linked'])
        stop.refresh_from_db()
        self.assertEqual(stop.ops_order_number, 5000)
        self.assertEqual(stop.status, 'disponible')

    def test_links_order_by_client_phone(self):
        stop = self.make_stops(1)[0]
        stop.ops_order_number = None
        stop.save(update_fields=['ops_order_number'])

        payload = self.event(6000, 'disponible')
        payload['batch_id'] = None
        res, _ = self.post_event(payload)

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['linked'])
        stop.refresh_from_db()
        self.assertEqual(stop.ops_order_number, 6000)

    def test_comanda_shared_number_updates_only_the_event_stop(self):
        """A comanda shares one order_number: `stop` picks the parada."""
        stops = self.make_stops(2)
        for stop in stops:
            stop.ops_order_number = 1234
            stop.save(update_fields=['ops_order_number'])

        payload = self.event(
            1234, 'entregado', stop=2,
            stops=[
                {'stop': 1, 'service_type': 'domicilio', 'address': 'Destino 1',
                 'description': '', 'status': 'disponible'},
                {'stop': 2, 'service_type': 'domicilio', 'address': 'Destino 2',
                 'description': '', 'status': 'entregado'},
            ],
        )
        res, _ = self.post_event(payload)

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['linked'])
        stops[0].refresh_from_db()
        stops[1].refresh_from_db()
        self.assertEqual(stops[0].status, 'pending')
        self.assertEqual(stops[1].status, 'entregado')
        self.assertEqual(stops[1].dest_address, 'Destino 2')

    def test_comanda_same_status_same_second_is_deduped_per_stop(self):
        stops = self.make_stops(2)
        for stop in stops:
            stop.ops_order_number = 1234
            stop.save(update_fields=['ops_order_number'])

        occurred = timezone.now().isoformat()
        first, _ = self.post_event(self.event(1234, 'asignado', stop=1, occurred_at=occurred))
        second, _ = self.post_event(self.event(1234, 'asignado', stop=2, occurred_at=occurred))

        self.assertFalse(first.json().get('duplicate', False))
        self.assertFalse(second.json().get('duplicate', False))
        stops[0].refresh_from_db()
        stops[1].refresh_from_db()
        self.assertEqual(stops[0].status, 'asignado')
        self.assertEqual(stops[1].status, 'asignado')

    def test_created_event_stamps_shared_number_and_status_on_all_comanda_stops(self):
        """`order.created` for a comanda mirrors every stop at once."""
        stops = self.make_stops(2)
        for stop in stops:
            stop.ops_order_number = None
            stop.save(update_fields=['ops_order_number'])

        payload = self.event(
            7000, 'disponible', event='order.created',
            stops=[
                {'stop': 1, 'service_type': 'domicilio', 'address': 'Destino 1',
                 'description': '', 'status': 'disponible'},
                {'stop': 2, 'service_type': 'domicilio', 'address': 'Destino 2',
                 'description': '', 'status': 'disponible'},
            ],
        )
        res, _ = self.post_event(payload)

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['linked'])
        self.assertEqual(res.json()['batch_status'], 'disponible')
        for stop in stops:
            stop.refresh_from_db()
            self.assertEqual(stop.ops_order_number, 7000)
            self.assertEqual(stop.status, 'disponible')
        self.assertEqual(stops[1].dest_address, 'Destino 2')
        self.order.refresh_from_db()
        self.assertEqual(self.order.ops_batch_id, 'a1b2c3d4e5f60718')

    # ── Dedupe ──────────────────────────────────────────────────────────

    def test_duplicate_event_is_ignored(self):
        stop = self.make_stops(1)[0]
        payload = self.event(1234, 'asignado')

        first, _ = self.post_event(payload)
        self.assertFalse(first.json().get('duplicate', False))

        # Un cambio manual posterior no debe ser pisado por el evento repetido.
        stop.status = 'confirmado'
        stop.save(update_fields=['status'])

        second, _ = self.post_event(payload)

        self.assertTrue(second.json()['duplicate'])
        stop.refresh_from_db()
        self.assertEqual(stop.status, 'confirmado')

    # ── Aggregation ─────────────────────────────────────────────────────

    def test_all_stops_canceled_sets_order_canceled(self):
        stops = self.make_stops(2)
        self.order.status = 'asignado'
        self.order.save(update_fields=['status'])
        for i, stop in enumerate(stops, start=1):
            stop.status = 'asignado'
            stop.save(update_fields=['status'])

        res1, _ = self.post_event(self.event(1234, 'cancelado'))
        res2, _ = self.post_event(self.event(1235, 'cancelado'))

        self.order.refresh_from_db()
        self.assertEqual(res1.json()['batch_status'], 'asignado')
        self.assertEqual(res2.json()['batch_status'], 'cancelado')
        self.assertEqual(self.order.status, 'cancelado')
        for stop in stops:
            stop.refresh_from_db()
            self.assertEqual(stop.status, 'cancelado')
            self.assertIsNotNone(stop.canceled_at)

    def test_all_stops_delivered_sets_order_delivered(self):
        stops = self.make_stops(2)
        for stop in stops:
            stop.status = 'en_ruta'
            stop.save(update_fields=['status'])

        self.post_event(self.event(1234, 'entregado'))
        res, _ = self.post_event(self.event(1235, 'entregado'))

        self.order.refresh_from_db()
        self.assertEqual(res.json()['batch_status'], 'entregado')
        self.assertEqual(self.order.status, 'entregado')

    def test_first_en_ruta_sets_order_en_ruta(self):
        self.make_stops(2)
        res, _ = self.post_event(self.event(1234, 'en_ruta'))
        self.order.refresh_from_db()
        self.assertEqual(res.json()['batch_status'], 'en_ruta')
        self.assertEqual(self.order.status, 'en_ruta')

    # ── Validation ──────────────────────────────────────────────────────

    def test_invalid_status_returns_400(self):
        res, _ = self.post_event(self.event(1234, 'inventado'))
        self.assertEqual(res.status_code, 400)

    def test_invalid_event_returns_400(self):
        res, _ = self.post_event(self.event(1234, 'asignado', event='order.deleted'))
        self.assertEqual(res.status_code, 400)
