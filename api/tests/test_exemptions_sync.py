"""Tests for POST /api/integrations/exemptions/sync/ (plan §4.2)."""

import json
import time
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from api.integrations.auth import generate_api_key
from api.integrations.hmac import compute_signature
from api.models import BotExemptContact, Conversation, ConversationTag, IntegrationApiKey

SECRET = 'test-webhook-secret'
URL = '/api/integrations/exemptions/sync/'


@override_settings(INTEGRATION_WEBHOOK_SECRET=SECRET)
class ExemptionsSyncTests(APITestCase):
    def setUp(self):
        raw, key_hash, prefix = generate_api_key()
        self.key = raw
        self.api_key = IntegrationApiKey.objects.create(
            name='ops', key_hash=key_hash, prefix=prefix,
            scopes=['exemptions:write'],
        )

    def post_snapshot(self, payload):
        body = json.dumps(payload, separators=(',', ':'))
        headers = {
            'HTTP_X_API_KEY': self.key,
            'HTTP_X_SIGNATURE': 'sha256=' + compute_signature(SECRET, body),
            'HTTP_X_TIMESTAMP': str(int(time.time())),
        }
        with patch('api.integrations.views.publish_conversation_update') as publish:
            with self.captureOnCommitCallbacks(execute=True):
                res = self.client.post(
                    URL, body, content_type='application/json', **headers,
                )
        return res, publish

    def snapshot(self, couriers):
        return {
            'generated_at': timezone.now().isoformat(),
            'kind': 'riders',
            'couriers': couriers,
        }

    @staticmethod
    def courier(courier_id, phone, name='Rider', active=True):
        return {'id': courier_id, 'name': name, 'phone': phone, 'code': f'r{courier_id}', 'active': active}

    # ── Happy path ──────────────────────────────────────────────────────

    def test_full_replace_upserts_contacts_and_manages_tags(self):
        conv1 = Conversation.objects.create(whatsapp_id='1', contact_name='Rider One', contact_phone='573001111111')
        conv2 = Conversation.objects.create(whatsapp_id='2', contact_name='Rider Two', contact_phone='573002222222')
        conv4 = Conversation.objects.create(whatsapp_id='4', contact_name='Unrelated', contact_phone='573004444444')

        # Estado previo: dos couriers sincronizados, un manual y un tag viejo.
        BotExemptContact.objects.create(contact_phone='573001111111', contact_name='Viejo Uno', source='ops_sync', ops_courier_id=1)
        BotExemptContact.objects.create(contact_phone='573002222222', contact_name='Rider Dos', source='ops_sync', ops_courier_id=2)
        BotExemptContact.objects.create(contact_phone='573009999999', contact_name='Manual', source='manual')
        ConversationTag.create_tag(conv2, 'Domii', expiry_type='never', tag_color='gray')
        ConversationTag.create_tag(conv4, 'Domii', expiry_type='never', tag_color='gray')

        res, publish = self.post_snapshot(self.snapshot([
            self.courier(1, '3001111111', 'Rider Uno'),
            self.courier(3, '3003333333', 'Rider Tres'),
        ]))

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['applied'], 2)
        self.assertEqual(data['removed'], 1)
        self.assertEqual(data['tags_added'], 1)   # conv1; conv3 no tiene conversación
        self.assertEqual(data['tags_removed'], 1)  # conv2

        # Contacts: upsert + delete; el manual sobrevive.
        updated = BotExemptContact.objects.get(contact_phone='573001111111')
        self.assertEqual(updated.contact_name, 'Rider Uno')
        self.assertEqual(updated.source, 'ops_sync')
        self.assertEqual(updated.ops_courier_id, 1)
        self.assertTrue(BotExemptContact.objects.filter(contact_phone='573003333333', source='ops_sync').exists())
        self.assertFalse(BotExemptContact.objects.filter(contact_phone='573002222222').exists())
        self.assertTrue(BotExemptContact.objects.filter(contact_phone='573009999999', source='manual').exists())

        # Tags: agregado a conv1, quitado de conv2, el no relacionado intacto.
        conv1.refresh_from_db()
        tag1 = ConversationTag.objects.filter(conversation=conv1, tag_name='Domii').first()
        self.assertIsNotNone(tag1)
        self.assertEqual(tag1.expiry_type, 'never')
        self.assertIsNone(tag1.expires_at)
        self.assertFalse(ConversationTag.objects.filter(conversation=conv2, tag_name='Domii').exists())
        self.assertTrue(ConversationTag.objects.filter(conversation=conv4, tag_name='Domii').exists())

        # SSE solo por las conversaciones afectadas.
        published_names = sorted(call.args[0].contact_name for call in publish.call_args_list)
        self.assertEqual(published_names, ['Rider One', 'Rider Two'])

    def test_second_identical_snapshot_is_idempotent(self):
        Conversation.objects.create(whatsapp_id='1', contact_name='Rider One', contact_phone='573001111111')
        payload = self.snapshot([self.courier(1, '3001111111')])

        first, _ = self.post_snapshot(payload)
        second, _ = self.post_snapshot(payload)

        self.assertEqual(first.json()['tags_added'], 1)
        self.assertEqual(second.json()['tags_added'], 0)
        self.assertEqual(second.json()['applied'], 1)
        self.assertEqual(ConversationTag.objects.filter(tag_name='Domii').count(), 1)

    def test_inactive_courier_in_request_removes_contact_and_tag(self):
        conv = Conversation.objects.create(whatsapp_id='1', contact_name='Rider One', contact_phone='573001111111')
        BotExemptContact.objects.create(contact_phone='573001111111', source='ops_sync', ops_courier_id=1)
        ConversationTag.create_tag(conv, 'Domii', expiry_type='never', tag_color='gray')

        res, _ = self.post_snapshot(self.snapshot([
            self.courier(1, '3001111111', active=False),
        ]))

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['removed'], 1)
        self.assertEqual(res.json()['tags_removed'], 1)
        self.assertFalse(BotExemptContact.objects.exists())
        self.assertFalse(ConversationTag.objects.filter(tag_name='Domii').exists())

    def test_phone_normalization_to_wa(self):
        res, _ = self.post_snapshot(self.snapshot([
            self.courier(7, '3001234567'),
        ]))

        self.assertEqual(res.status_code, 200)
        self.assertTrue(BotExemptContact.objects.filter(contact_phone='573001234567').exists())
        self.assertFalse(BotExemptContact.objects.filter(contact_phone='3001234567').exists())

    def test_manual_contacts_are_never_deleted(self):
        BotExemptContact.objects.create(contact_phone='573009999999', source='manual', contact_name='Manual')
        res, _ = self.post_snapshot(self.snapshot([]))

        self.assertEqual(res.status_code, 200)
        self.assertTrue(BotExemptContact.objects.filter(contact_phone='573009999999').exists())

    def test_unsupported_kind_returns_400(self):
        res, _ = self.post_snapshot({'kind': 'operators', 'couriers': []})
        self.assertEqual(res.status_code, 400)

    def test_couriers_must_be_a_list(self):
        res, _ = self.post_snapshot({'kind': 'riders', 'couriers': 'nope'})
        self.assertEqual(res.status_code, 400)
