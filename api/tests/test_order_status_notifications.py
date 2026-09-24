"""Phase 5 — aggregate order status notifications (plan §1, §4.2).

Covers the batch transitions (``asignado``, ``en_ruta``, ``entregado``,
``cancelado``), the ``notified_statuses`` dedupe, the master switch and the
service-window template fallback.
"""

import json
import time
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from api.integrations import notify as notify_module
from api.integrations.auth import generate_api_key
from api.integrations.hmac import compute_signature
from api.integrations.notify import (
    build_fallback_template_payload,
    send_service_window_fallback,
    send_text,
)
from api.integrations.orders import send_confirmation
from api.integrations.status_notifications import (
    DEFAULT_ORDER_STATUS_MESSAGES,
    build_fallback_template,
    maybe_notify,
    render_status_text,
    resolve_transition,
    status_values,
)
from api.models import (
    BotConfig,
    Conversation,
    IntegrationApiKey,
    Message,
    Order,
    OrderStop,
    WhatsAppTemplate,
)

SECRET = 'test-webhook-secret'
URL = '/api/integrations/orders/events/'


@override_settings(INTEGRATION_WEBHOOK_SECRET=SECRET)
class OrderStatusNotificationWebhookTests(APITestCase):
    """A signed ops event drives at most one client message per transition."""

    def setUp(self):
        cache.clear()
        from api.bot.config import _clear_cache

        _clear_cache()
        raw, key_hash, prefix = generate_api_key()
        self.key = raw
        IntegrationApiKey.objects.create(
            name='ops', key_hash=key_hash, prefix=prefix, scopes=['orders:write'],
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
        # Never touch WhatsApp from tests.
        pool_patcher = patch('api.views._send_pool')
        self.pool = pool_patcher.start()
        self.addCleanup(pool_patcher.stop)
        publish_patcher = patch('api.integrations.views._publish_order_updated')
        publish_patcher.start()
        self.addCleanup(publish_patcher.stop)

    # ── Helpers ──────────────────────────────────────────────────────────

    def post_event(self, payload):
        body = json.dumps(payload, separators=(',', ':'))
        headers = {
            'HTTP_X_API_KEY': self.key,
            'HTTP_X_SIGNATURE': 'sha256=' + compute_signature(SECRET, body),
            'HTTP_X_TIMESTAMP': str(int(time.time())),
        }
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                URL, body, content_type='application/json', **headers,
            )

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

    def make_stops(self, count=2, number=1234, status='pending'):
        stops = []
        for index in range(1, count + 1):
            stops.append(OrderStop.objects.create(
                order=self.order, stop_no=index, ops_order_number=number,
                service_type='domicilio', dest_address=f'Destino {index}',
                price=8000, status=status,
            ))
        return stops

    def messages(self, message_type='text'):
        return list(Message.objects.filter(
            conversation=self.conversation, direction='outbound',
            message_type=message_type,
        ).order_by('id'))

    def refresh_order(self):
        self.order.refresh_from_db()
        return self.order

    # ── Transition aggregation ───────────────────────────────────────────

    def test_single_stop_en_ruta_notifies_with_configured_text(self):
        self.make_stops(1, number=1240)

        res = self.post_event(self.event(1240, 'en_ruta'))

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['notified'])
        self.assertEqual(res.json()['notified_status'], 'en_ruta')
        messages = self.messages()
        self.assertEqual(len(messages), 1)
        self.assertIn('#1240', messages[0].content)
        self.assertNotIn('Ana', messages[0].content)
        self.assertEqual(
            messages[0].metadata['fallback_template']['name'], 'aviso_en_ruta',
        )
        self.assertEqual(self.refresh_order().notified_statuses, ['en_ruta'])

    def test_same_transition_is_never_sent_twice(self):
        self.make_stops(1, number=1240)
        self.post_event(self.event(1240, 'en_ruta'))

        again = self.post_event(self.event(
            1240, 'en_ruta', occurred_at=timezone.now().isoformat(),
        ))

        self.assertFalse(again.json()['notified'])
        self.assertEqual(len(self.messages()), 1)

    def test_two_stops_wait_for_both_assignments(self):
        self.make_stops(2, number=1234)

        first = self.post_event(self.event(1234, 'asignado', stop=1))
        self.assertFalse(first.json()['notified'])
        self.assertEqual(len(self.messages()), 0)

        second = self.post_event(self.event(1234, 'asignado', stop=2))
        self.assertTrue(second.json()['notified'])
        self.assertEqual(second.json()['notified_status'], 'asignado')
        self.assertEqual(len(self.messages()), 1)

    def test_first_en_ruta_notifies_only_once(self):
        self.make_stops(2, number=1234)
        self.post_event(self.event(1234, 'en_ruta', stop=1))

        second = self.post_event(self.event(1234, 'en_ruta', stop=2))

        self.assertFalse(second.json()['notified'])
        self.assertEqual(len(self.messages()), 1)

    def test_entregado_fires_when_every_stop_is_delivered(self):
        stops = self.make_stops(2, number=1234, status='en_ruta')
        self.order.notified_statuses = ['en_ruta']
        self.order.save(update_fields=['notified_statuses'])

        first = self.post_event(self.event(1234, 'entregado', stop=1))
        self.assertFalse(first.json()['notified'])
        self.assertEqual(len(self.messages()), 0)

        second = self.post_event(self.event(1234, 'entregado', stop=2))
        self.assertTrue(second.json()['notified'])
        self.assertEqual(second.json()['notified_status'], 'entregado')
        self.assertEqual(len(self.messages()), 1)
        for stop in stops:
            stop.refresh_from_db()
            self.assertEqual(stop.status, 'entregado')

    def test_cancelado_fires_only_when_every_stop_is_canceled(self):
        self.make_stops(2, number=1234, status='asignado')
        self.order.notified_statuses = ['asignado']
        self.order.save(update_fields=['notified_statuses'])

        partial = self.post_event(self.event(1234, 'cancelado', stop=1))
        self.assertFalse(partial.json()['notified'])
        self.assertEqual(len(self.messages()), 0)

        final = self.post_event(self.event(1234, 'cancelado', stop=2))
        self.assertTrue(final.json()['notified'])
        self.assertEqual(final.json()['notified_status'], 'cancelado')
        self.assertEqual(len(self.messages()), 1)
        self.assertIn('cancelado', self.messages()[0].content)

    def test_order_created_does_not_notify(self):
        self.make_stops(2, number=1234)
        res = self.post_event(self.event(
            1234, 'disponible', event='order.created',
            stops=[
                {'stop': 1, 'service_type': 'domicilio', 'address': 'Destino 1',
                 'description': '', 'status': 'disponible'},
                {'stop': 2, 'service_type': 'domicilio', 'address': 'Destino 2',
                 'description': '', 'status': 'disponible'},
            ],
        ))

        self.assertFalse(res.json()['notified'])
        self.assertEqual(len(self.messages()), 0)

    def test_lower_transition_is_skipped_after_a_higher_one(self):
        stops = self.make_stops(2, number=1234, status='asignado')
        stops[0].status = 'entregado'
        stops[0].save(update_fields=['status'])
        self.order.notified_statuses = ['en_ruta']
        self.order.save(update_fields=['notified_statuses'])

        res = self.post_event(self.event(1234, 'asignado', stop=2))

        # The batch satisfies asignado again, but en_ruta already went out.
        self.assertFalse(res.json()['notified'])
        self.assertEqual(len(self.messages()), 0)
        self.assertEqual(self.refresh_order().notified_statuses, ['en_ruta'])

    def test_master_switch_disables_messages_but_keeps_the_mirror(self):
        BotConfig.objects.update_or_create(
            key='order_status_notifications_enabled', defaults={'value': False},
        )
        from api.bot.config import _clear_cache

        _clear_cache()
        try:
            self.make_stops(1, number=1240)
            res = self.post_event(self.event(1240, 'en_ruta'))

            self.assertFalse(res.json()['notified'])
            self.assertEqual(len(self.messages()), 0)
            self.assertEqual(self.refresh_order().status, 'en_ruta')
        finally:
            _clear_cache()

    def test_configured_status_text_is_used(self):
        BotConfig.objects.update_or_create(
            key='order_status_messages',
            defaults={'value': {'en_ruta': 'Tu pedido {order_number} va en camino'}},
        )
        from api.bot.config import _clear_cache

        _clear_cache()
        try:
            self.make_stops(1, number=1240)
            self.post_event(self.event(1240, 'en_ruta'))
            self.assertEqual(
                self.messages()[0].content, 'Tu pedido #1240 va en camino',
            )
        finally:
            _clear_cache()


class StatusNotificationUnitTests(TestCase):
    """Pure helpers: transition resolution, values and rendering."""

    def setUp(self):
        self.conversation = Conversation.objects.create(
            whatsapp_id='573009999999', contact_name='Cliente',
            contact_phone='573009999999',
        )
        self.order = Order.objects.create(
            conversation=self.conversation, client_name='Cliente Oculto',
            origin_address='Calle 1 #2-3', status='pending',
        )

    def stop(self, number, status):
        return OrderStop.objects.create(
            order=self.order, stop_no=number, ops_order_number=777,
            service_type='domicilio', dest_address=f'D {number}',
            price=5000, status=status,
        )

    def test_resolve_transition_prefers_the_highest_state(self):
        self.stop(1, 'entregado')
        self.stop(2, 'en_ruta')
        self.assertEqual(resolve_transition(self.order), 'en_ruta')

    def test_resolve_transition_mixed_terminal_is_silent(self):
        self.stop(1, 'entregado')
        self.stop(2, 'cancelado')
        self.assertIsNone(resolve_transition(self.order))

    def test_resolve_transition_confirmado_counts_as_assigned(self):
        self.stop(1, 'confirmado')
        self.stop(2, 'asignado')
        self.assertEqual(resolve_transition(self.order), 'asignado')

    def test_values_exclude_the_client_name(self):
        self.order.ops_order_number = None
        stop = self.stop(1, 'asignado')
        stop.ops_order_number = 4321
        stop.save(update_fields=['ops_order_number'])

        values = status_values(self.order, 'asignado')
        self.assertNotIn('client_name', values)
        self.assertEqual(values['order_number'], '#4321')
        self.assertNotIn('Cliente Oculto', json.dumps(values))

    def test_default_message_has_no_client_name_placeholder(self):
        for text in DEFAULT_ORDER_STATUS_MESSAGES.values():
            self.assertNotIn('{client_name}', text)

    def test_render_status_text_replaces_placeholders(self):
        stop = self.stop(1, 'entregado')
        stop.ops_order_number = 4321
        stop.save(update_fields=['ops_order_number'])
        text = render_status_text(self.order, 'entregado')
        self.assertIn('#4321', text)
        self.assertNotIn('{#', text)

    def test_build_fallback_template_carries_values(self):
        stop = self.stop(1, 'en_ruta')
        stop.ops_order_number = 4321
        stop.save(update_fields=['ops_order_number'])
        fallback = build_fallback_template(self.order, 'en_ruta')
        self.assertEqual(fallback['name'], 'aviso_en_ruta')
        self.assertEqual(fallback['language'], 'es')
        self.assertEqual(
            fallback['params'],
            [{'name': 'order_code', 'value': 'order_number'}],
        )
        self.assertEqual(fallback['values']['order_number'], '#4321')


class ServiceWindowFallbackTests(TestCase):
    """The text notification is swapped for the approved template."""

    def setUp(self):
        from api.bot.config import _clear_cache

        _clear_cache()
        self.conversation = Conversation.objects.create(
            whatsapp_id='573001112233', contact_name='Ana',
            contact_phone='573001112233',
        )
        pool_patcher = patch('api.views._send_pool')
        self.pool = pool_patcher.start()
        self.addCleanup(pool_patcher.stop)

    def make_message(self, fallback=None):
        with self.captureOnCommitCallbacks(execute=True):
            return send_text(
                self.conversation,
                '🛵 Pedido #1234 en camino.',
                fallback_template=fallback,
                preview='🛵 Pedido #1234 en camino.',
            )

    def mark_failed(self, message, code=131047, text='(#131047) Re-engagement message'):
        metadata = dict(message.metadata or {})
        metadata['send_error_code'] = code
        metadata['send_error'] = text
        message.metadata = metadata
        message.save(update_fields=['metadata'])

    @staticmethod
    def fallback():
        return {
            'status': 'en_ruta',
            'name': 'aviso_en_ruta',
            'language': 'es',
            'params': ['order_number'],
            'values': {'order_number': '#1234', 'status': 'en_ruta'},
        }

    def test_service_window_error_detection(self):
        self.assertTrue(notify_module._is_service_window_error(
            {'send_error_code': 131047},
        ))
        self.assertTrue(notify_module._is_service_window_error(
            {'send_error': '(#131047) Re-engagement message'},
        ))
        self.assertTrue(notify_module._is_service_window_error({'send_error_code': '470'}))
        self.assertFalse(notify_module._is_service_window_error(
            {'send_error_code': 131000, 'send_error': 'Something else'},
        ))
        self.assertFalse(notify_module._is_service_window_error({}))

    def test_fallback_replaces_the_failed_text(self):
        message = self.make_message(self.fallback())
        self.mark_failed(message)

        with self.captureOnCommitCallbacks(execute=True):
            template = send_service_window_fallback(message.id, self.fallback())

        self.assertIsNotNone(template)
        message.refresh_from_db()
        self.assertEqual(message.metadata['status'], 'cancelled')
        self.assertTrue(message.metadata['fallback_sent'])
        self.assertEqual(message.metadata['fallback_message_id'], template.id)

        self.assertEqual(template.message_type, 'template')
        payload = json.loads(template.content)
        self.assertEqual(payload['name'], 'aviso_en_ruta')
        self.assertEqual(payload['language'], {'code': 'es'})
        self.assertEqual(payload['components'][0]['parameters'], [
            {'type': 'text', 'text': '#1234'},
        ])

        # The readable text stays in the conversation list preview.
        self.conversation.refresh_from_db()
        self.assertIn('en camino', self.conversation.last_message)

        # Idempotent: no second template message.
        self.assertIsNone(send_service_window_fallback(message.id, self.fallback()))
        self.assertEqual(
            Message.objects.filter(message_type='template').count(), 1,
        )

    def test_other_failures_are_not_replaced(self):
        message = self.make_message(self.fallback())
        self.mark_failed(message, code=131000, text='Message undeliverable')

        self.assertIsNone(send_service_window_fallback(message.id, self.fallback()))
        message.refresh_from_db()
        # Queued (pending) but never replaced by a template.
        self.assertNotEqual(message.metadata.get('status'), 'cancelled')
        self.assertFalse(message.metadata.get('fallback_sent'))
        self.assertEqual(Message.objects.filter(message_type='template').count(), 0)

    def test_message_without_fallback_is_untouched(self):
        message = self.make_message()
        self.mark_failed(message)
        self.assertIsNone(send_service_window_fallback(message.id, {
            'name': '', 'language': 'es', 'params': [], 'values': {},
        }))

    def test_local_template_row_is_used(self):
        WhatsAppTemplate.objects.create(
            name='aviso_en_ruta', language='es', category='UTILITY',
            status='APPROVED',
            components=[{'type': 'body', 'text': 'Hola {{pedido}}, tu pedido va en camino'}],
        )
        message = self.make_message(self.fallback())
        self.mark_failed(message)

        with self.captureOnCommitCallbacks(execute=True):
            template = send_service_window_fallback(message.id, self.fallback())

        payload = json.loads(template.content)
        # The approved template names its parameter ``pedido``; the first
        # configured value fills it positionally.
        self.assertEqual(payload['components'][0]['parameters'], [
            {'type': 'text', 'parameter_name': 'pedido', 'text': '#1234'},
        ])

    def test_deliver_outbound_triggers_the_fallback(self):
        message = self.make_message(self.fallback())

        def fake_send(*args, **kwargs):
            self.mark_failed(message, code=131047)

        with patch('api.views.send_whatsapp_outbound', side_effect=fake_send):
            with self.captureOnCommitCallbacks(execute=True):
                notify_module._deliver_outbound(
                    message.id, self.conversation.id, self.fallback(),
                )

        message.refresh_from_db()
        self.assertEqual(message.metadata['status'], 'cancelled')
        self.assertEqual(Message.objects.filter(message_type='template').count(), 1)


class FallbackPayloadTests(TestCase):
    """Payload building across named/positional/unknown templates."""

    def descriptor(self, **overrides):
        fallback = {
            'status': 'entregado',
            'name': 'aviso_entregado',
            'language': 'es',
            'params': ['order_number', 'total'],
            'values': {'order_number': '#1234', 'total': '$8.000'},
        }
        fallback.update(overrides)
        return fallback

    def test_generic_payload_uses_ordered_values(self):
        payload = build_fallback_template_payload(self.descriptor())
        self.assertEqual(payload['name'], 'aviso_entregado')
        self.assertEqual(payload['components'][0]['parameters'], [
            {'type': 'text', 'text': '#1234'},
            {'type': 'text', 'text': '$8.000'},
        ])

    def test_named_template_parameters_resolve_by_name(self):
        WhatsAppTemplate.objects.create(
            name='aviso_entregado', language='es', category='UTILITY',
            status='APPROVED',
            components=[{'type': 'body', 'text': 'Pedido {{order_number}}: {{total}}'}],
        )
        payload = build_fallback_template_payload(self.descriptor())
        self.assertEqual(payload['components'][0]['parameters'], [
            {'type': 'text', 'parameter_name': 'order_number', 'text': '#1234'},
            {'type': 'text', 'parameter_name': 'total', 'text': '$8.000'},
        ])

    def test_declared_names_fall_back_to_position(self):
        WhatsAppTemplate.objects.create(
            name='aviso_entregado', language='es', category='UTILITY',
            status='APPROVED',
            components=[{
                'type': 'body',
                'text': 'Hola {{cliente}}',
                'parameters': [{'name': 'cliente'}],
            }],
        )
        payload = build_fallback_template_payload(self.descriptor())
        self.assertEqual(payload['components'][0]['parameters'], [
            {'type': 'text', 'parameter_name': 'cliente', 'text': '#1234'},
        ])

    def test_missing_name_returns_none(self):
        self.assertIsNone(build_fallback_template_payload(self.descriptor(name='')))

    def test_named_mapping_without_local_row(self):
        """The approved templates use a named ``{{order_code}}`` parameter."""
        payload = build_fallback_template_payload(self.descriptor(
            params=[{'name': 'order_code', 'value': 'order_number'}],
        ))
        self.assertEqual(payload['components'][0]['parameters'], [
            {'type': 'text', 'parameter_name': 'order_code', 'text': '#1234'},
        ])

    def test_named_mapping_with_local_named_template(self):
        WhatsAppTemplate.objects.create(
            name='aviso_entregado', language='es', category='UTILITY',
            status='APPROVED',
            components=[{'type': 'body', 'text': 'Tu pedido {{order_code}} fue entregado'}],
        )
        payload = build_fallback_template_payload(self.descriptor(
            params=[{'name': 'order_code', 'value': 'order_number'}],
        ))
        self.assertEqual(payload['components'][0]['parameters'], [
            {'type': 'text', 'parameter_name': 'order_code', 'text': '#1234'},
        ])

    def test_named_mapping_with_local_positional_template(self):
        WhatsAppTemplate.objects.create(
            name='aviso_entregado', language='es', category='UTILITY',
            status='APPROVED',
            components=[{'type': 'body', 'text': 'Tu pedido {{1}} fue entregado'}],
        )
        payload = build_fallback_template_payload(self.descriptor(
            params=[{'name': 'order_code', 'value': 'order_number'}],
        ))
        self.assertEqual(payload['components'][0]['parameters'], [
            {'type': 'text', 'text': '#1234'},
        ])

    def test_plain_string_params_stay_positional(self):
        payload = build_fallback_template_payload(self.descriptor(
            params=['order_number'],
        ))
        self.assertEqual(payload['components'][0]['parameters'], [
            {'type': 'text', 'text': '#1234'},
        ])

    def test_positional_template_renders_in_the_thread(self):
        from api.serializers import MessageSerializer

        message = Message.objects.create(
            conversation=Conversation.objects.create(
                whatsapp_id='573001110000', contact_name='Ana',
                contact_phone='573001110000',
            ),
            direction='outbound',
            message_type='template',
            content=json.dumps({
                'name': 'aviso_en_ruta',
                'language': {'code': 'es'},
                'components': [{
                    'type': 'body',
                    'parameters': [{'type': 'text', 'text': '#1234'}],
                }],
            }),
        )
        self.assertEqual(MessageSerializer(message).data['content_display'], '#1234')

    def test_named_template_renders_stored_body(self):
        from api.serializers import MessageSerializer

        WhatsAppTemplate.objects.create(
            name='aviso_entregado', language='es', category='UTILITY',
            status='APPROVED',
            components=[{'type': 'body', 'text': 'Pedido {{order_number}} entregado'}],
        )
        message = Message.objects.create(
            conversation=Conversation.objects.create(
                whatsapp_id='573001110001', contact_name='Ana',
                contact_phone='573001110001',
            ),
            direction='outbound',
            message_type='template',
            content=json.dumps({
                'name': 'aviso_entregado',
                'language': {'code': 'es'},
                'components': [{
                    'type': 'body',
                    'parameters': [
                        {'type': 'text', 'parameter_name': 'order_number', 'text': '#1234'},
                    ],
                }],
            }),
        )
        self.assertEqual(
            MessageSerializer(message).data['content_display'],
            'Pedido #1234 entregado',
        )


class UsernameOnlyOrderNotificationTests(TestCase):
    """Orders for WhatsApp username-only contacts still reach the client.

    Regression: a conversation with an empty ``contact_phone`` (username-only
    contact, addressed by the BSUID in ``whatsapp_id``) was silently skipped by
    both the aggregate status notification and the creation confirmation.
    """

    BSUID = 'CO.3271735316332842'

    def setUp(self):
        from api.bot.config import _clear_cache

        _clear_cache()
        self.conversation = Conversation.objects.create(
            whatsapp_id=self.BSUID, contact_name='Juan Camilo', contact_phone='',
        )
        self.order = Order.objects.create(
            conversation=self.conversation,
            ops_client_user_id=8596,
            client_name='Juan Camilo',
            origin_address='Calle 9A #13-40',
            status='pending',
            source='agent',
        )
        self.stop = OrderStop.objects.create(
            order=self.order, stop_no=1, ops_order_number=39511,
            service_type='domicilio', dest_address='Kra 36 #33-41',
            price=4500, status='en_ruta',
        )
        # Never touch WhatsApp from tests.
        pool_patcher = patch('api.views._send_pool')
        self.pool = pool_patcher.start()
        self.addCleanup(pool_patcher.stop)

    def outbound(self, message_type='text'):
        return list(Message.objects.filter(
            conversation=self.conversation, direction='outbound',
            message_type=message_type,
        ).order_by('id'))

    def test_has_delivery_target_accepts_username_only(self):
        from api.integrations.status_notifications import has_delivery_target

        self.assertTrue(has_delivery_target(self.conversation))
        self.assertFalse(has_delivery_target(None))

    def test_username_only_conversation_gets_a_status_notification(self):
        with self.captureOnCommitCallbacks(execute=True):
            notified = maybe_notify(self.order)

        self.assertEqual(notified, 'en_ruta')
        texts = self.outbound()
        self.assertEqual(len(texts), 1)
        self.assertIn('#39511', texts[0].content)
        # The approved template fallback travels with the text (BSUID-capable).
        self.assertEqual(
            texts[0].metadata['fallback_template']['name'], 'aviso_en_ruta',
        )
        self.order.refresh_from_db()
        self.assertIn('en_ruta', self.order.notified_statuses)

    def test_username_only_conversation_gets_the_confirmation(self):
        with self.captureOnCommitCallbacks(execute=True):
            send_confirmation(self.conversation, self.order)

        texts = self.outbound()
        self.assertEqual(len(texts), 1)
        self.assertIn('#39511', texts[0].content)

    def test_missing_identity_does_not_claim_the_transition(self):
        Conversation.objects.filter(pk=self.conversation.pk).update(
            whatsapp_id='', contact_phone='',
        )
        self.conversation.refresh_from_db()

        with self.captureOnCommitCallbacks(execute=True):
            notified = maybe_notify(self.order)

        self.assertIsNone(notified)
        self.order.refresh_from_db()
        self.assertEqual(self.order.notified_statuses, [])
        self.assertEqual(self.outbound(), [])
