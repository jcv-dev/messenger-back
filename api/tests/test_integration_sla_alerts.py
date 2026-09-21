"""Tests for POST /api/integrations/notifications/sla/ (courier SLA alerts).

Ops' ``domii:sla-alerts`` command calls this endpoint so the courier also gets
the approved ``aviso_sla`` utility template over WhatsApp. Covers the named
template payload, conversation creation/reuse, idempotency, the master switch,
configuration overrides and malformed payloads.
"""

import json
import time
from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from api.bot.config import _clear_cache
from api.integrations.auth import generate_api_key
from api.integrations.hmac import compute_signature
from api.models import (
    BotConfig,
    Conversation,
    ConversationTag,
    IntegrationApiKey,
    Message,
    WhatsAppTemplate,
)

SECRET = 'test-webhook-secret'
URL = '/api/integrations/notifications/sla/'


@override_settings(INTEGRATION_WEBHOOK_SECRET=SECRET)
class SlaAlertTests(APITestCase):
    def setUp(self):
        cache.clear()
        _clear_cache()
        raw, key_hash, prefix = generate_api_key()
        self.key = raw
        IntegrationApiKey.objects.create(
            name='ops', key_hash=key_hash, prefix=prefix, scopes=['orders:write'],
        )
        # Never touch WhatsApp from tests.
        pool_patcher = patch('api.views._send_pool')
        pool_patcher.start()
        self.addCleanup(pool_patcher.stop)
        publish_patcher = patch('api.views.publish_conversation_update')
        publish_patcher.start()
        self.addCleanup(publish_patcher.stop)

    # ── Helpers ──────────────────────────────────────────────────────────

    def alert(self, **overrides):
        payload = {
            'alert_key': 'sla:12345:asignado:5',
            'courier': {'id': 1642, 'name': 'Samuel Niampira', 'phone': '3007654321'},
            'order': {
                'order_number': 900123456,
                'status': 'asignado',
                'minutes': 12,
                'threshold': 5,
            },
        }
        payload.update(overrides)
        return payload

    def post_alert(self, payload=None, key=None):
        body = json.dumps(
            payload if payload is not None else self.alert(),
            separators=(',', ':'),
        )
        headers = {
            'HTTP_X_API_KEY': key or self.key,
            'HTTP_X_SIGNATURE': 'sha256=' + compute_signature(SECRET, body),
            'HTTP_X_TIMESTAMP': str(int(time.time())),
        }
        return self.client.post(URL, body, content_type='application/json', **headers)

    def outbound_templates(self):
        return list(
            Message.objects.filter(direction='outbound', message_type='template')
            .order_by('id')
        )

    # ── Happy path ───────────────────────────────────────────────────────

    def test_sends_named_template_and_creates_tagged_conversation(self):
        res = self.post_alert()

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['ok'])
        self.assertTrue(data['sent'])
        self.assertFalse(data['duplicate'])
        self.assertIsNone(data['skipped'])

        conversation = Conversation.objects.get(contact_phone='573007654321')
        self.assertEqual(conversation.contact_name, 'Samuel Niampira')
        self.assertTrue(
            ConversationTag.objects.filter(
                conversation=conversation, tag_name='Domii',
            ).exists()
        )

        messages = self.outbound_templates()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].id, data['message_id'])
        self.assertEqual(messages[0].conversation_id, conversation.id)

        payload = json.loads(messages[0].content)
        self.assertEqual(payload['name'], 'aviso_sla')
        self.assertEqual(payload['language'], {'code': 'es'})
        self.assertEqual(payload['components'][0]['parameters'], [
            {'type': 'text', 'parameter_name': 'order_code', 'text': '#900123456'},
            {'type': 'text', 'parameter_name': 'time', 'text': '12'},
            {'type': 'text', 'parameter_name': 'order_status', 'text': 'asignado'},
        ])
        self.assertEqual(
            messages[0].metadata['sla_alert']['alert_key'],
            'sla:12345:asignado:5',
        )

    def test_en_ruta_renders_human_label(self):
        res = self.post_alert(self.alert(
            alert_key='sla:12345:en_ruta:15',
            order={
                'order_number': 900123457,
                'status': 'en_ruta',
                'minutes': 20,
                'threshold': 15,
            },
        ))

        self.assertTrue(res.json()['sent'])
        payload = json.loads(self.outbound_templates()[0].content)
        params = {
            p['parameter_name']: p['text']
            for p in payload['components'][0]['parameters']
        }
        self.assertEqual(params['order_status'], 'en ruta')
        self.assertEqual(params['time'], '20')

    def test_reuses_existing_conversation(self):
        conversation = Conversation.objects.create(
            whatsapp_id='573007654321',
            contact_name='Samuel Niampira',
            contact_phone='573007654321',
        )

        res = self.post_alert()

        self.assertTrue(res.json()['sent'])
        self.assertEqual(Conversation.objects.count(), 1)
        self.assertEqual(self.outbound_templates()[0].conversation_id, conversation.id)

    def test_alert_key_is_idempotent(self):
        first = self.post_alert()
        second = self.post_alert()

        self.assertTrue(first.json()['sent'])
        data = second.json()
        self.assertTrue(data['ok'])
        self.assertFalse(data['sent'])
        self.assertTrue(data['duplicate'])
        self.assertEqual(len(self.outbound_templates()), 1)

    def test_send_failure_releases_alert_key_for_retry(self):
        with patch(
            'api.integrations.sla_notifications.create_and_send_outbound',
            side_effect=RuntimeError('boom'),
        ):
            with self.assertRaises(RuntimeError):
                self.post_alert()

        retry = self.post_alert()

        self.assertTrue(retry.json()['sent'])
        self.assertEqual(len(self.outbound_templates()), 1)

    # ── Skips ────────────────────────────────────────────────────────────

    def test_invalid_phone_is_skipped_without_conversation(self):
        res = self.post_alert(self.alert(
            courier={'id': 1642, 'name': 'Samuel', 'phone': '123'},
        ))

        data = res.json()
        self.assertTrue(data['ok'])
        self.assertFalse(data['sent'])
        self.assertEqual(data['skipped'], 'invalid_phone')
        self.assertEqual(Conversation.objects.count(), 0)
        self.assertEqual(self.outbound_templates(), [])

    def test_master_switch_off_skips(self):
        BotConfig.objects.update_or_create(
            key='sla_notifications_enabled', defaults={'value': False},
        )
        _clear_cache()

        data = self.post_alert().json()

        self.assertFalse(data['sent'])
        self.assertEqual(data['skipped'], 'disabled')
        self.assertEqual(self.outbound_templates(), [])

    def test_test_order_is_skipped_without_conversation(self):
        res = self.post_alert(self.alert(order={
            'order_number': 900123456,
            'status': 'asignado',
            'minutes': 12,
            'threshold': 5,
            'is_test': True,
        }))

        data = res.json()
        self.assertTrue(data['ok'])
        self.assertFalse(data['sent'])
        self.assertFalse(data['duplicate'])
        self.assertEqual(data['skipped'], 'test_order')
        self.assertEqual(Conversation.objects.count(), 0)
        self.assertEqual(self.outbound_templates(), [])

    def test_test_order_flag_accepts_string_values(self):
        res = self.post_alert(self.alert(order={
            'order_number': 900123456,
            'status': 'asignado',
            'minutes': 12,
            'threshold': 5,
            'is_test': 'true',
        }))

        data = res.json()
        self.assertFalse(data['sent'])
        self.assertEqual(data['skipped'], 'test_order')
        self.assertEqual(self.outbound_templates(), [])

    def test_real_order_is_not_skipped_by_the_test_guard(self):
        data = self.post_alert(self.alert(order={
            'order_number': 900123456,
            'status': 'asignado',
            'minutes': 12,
            'threshold': 5,
            'is_test': False,
        })).json()

        self.assertTrue(data['sent'])
        self.assertIsNone(data['skipped'])
        self.assertEqual(len(self.outbound_templates()), 1)

    # ── Configuration ────────────────────────────────────────────────────

    def test_config_override_changes_template_and_variables(self):
        BotConfig.objects.update_or_create(
            key='sla_alert_template',
            defaults={'value': {
                'template': 'aviso_sla_v2',
                'language': 'es_CO',
                'params': [
                    {'name': 'code', 'value': 'order_code'},
                    {'name': 'mins', 'value': 'time'},
                    {'name': 'estado', 'value': 'order_status'},
                ],
            }},
        )
        _clear_cache()

        self.assertTrue(self.post_alert().json()['sent'])

        payload = json.loads(self.outbound_templates()[0].content)
        self.assertEqual(payload['name'], 'aviso_sla_v2')
        self.assertEqual(payload['language'], {'code': 'es_CO'})
        self.assertEqual(
            [p['parameter_name'] for p in payload['components'][0]['parameters']],
            ['code', 'mins', 'estado'],
        )

    def test_local_template_row_resolves_named_placeholders(self):
        WhatsAppTemplate.objects.create(
            name='aviso_sla',
            language='es',
            category='UTILITY',
            components=[{
                'type': 'body',
                'text': (
                    'Tu pedido {{order_code}} lleva {{time}} minutos en estado '
                    '{{order_status}}.'
                ),
            }],
        )

        self.assertTrue(self.post_alert().json()['sent'])

        payload = json.loads(self.outbound_templates()[0].content)
        self.assertEqual(payload['components'][0]['parameters'], [
            {'type': 'text', 'parameter_name': 'order_code', 'text': '#900123456'},
            {'type': 'text', 'parameter_name': 'time', 'text': '12'},
            {'type': 'text', 'parameter_name': 'order_status', 'text': 'asignado'},
        ])

    # ── Auth & validation ────────────────────────────────────────────────

    def test_requires_orders_write_scope(self):
        raw, key_hash, prefix = generate_api_key()
        IntegrationApiKey.objects.create(
            name='readonly', key_hash=key_hash, prefix=prefix, scopes=[],
        )

        res = self.post_alert(key=raw)

        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.outbound_templates(), [])

    def test_rejects_malformed_payloads(self):
        cases = [
            self.alert(alert_key=''),
            self.alert(order={'order_number': None, 'status': 'asignado', 'minutes': 1}),
            self.alert(order={'order_number': 9001, 'status': 'entregado', 'minutes': 1}),
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                res = self.post_alert(payload)
                self.assertEqual(res.status_code, 400)
                self.assertFalse(res.json()['ok'])

        self.assertEqual(self.outbound_templates(), [])
