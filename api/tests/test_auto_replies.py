"""Auto-reply guard: classification, webhook integration and review API."""

import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from api.auto_replies import DEFAULT_CONFIG, classify_message, normalize_text
from api.models import BotConfig, Conversation, Message, WhatsAppTemplate
from api.tests import get_or_create_tulua_group

PHONE = '573001112233'
MARKETING_TEXT = '¡Hola! Gracias por escribirnos, en breve te atenderemos.'

LOCMEM_CACHES = {
    'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'},
    'throttle': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'},
}


def _clear_config_cache():
    from api.bot.config import _clear_cache
    _clear_cache()


@override_settings(CACHES=LOCMEM_CACHES, WHATSAPP_APP_SECRET='')
class AutoReplyGuardTests(APITestCase):
    def setUp(self):
        cache.clear()
        _clear_config_cache()
        self.group = get_or_create_tulua_group()
        self.conversation = Conversation.objects.create(
            whatsapp_id=PHONE,
            contact_name='Negocio Demo',
            contact_phone=PHONE,
            group=self.group,
        )

    def _set_config(self, **overrides):
        value = {**DEFAULT_CONFIG, **overrides}
        BotConfig.objects.update_or_create(
            key='auto_reply_guard', defaults={'value': value},
        )
        _clear_config_cache()

    def _marketing_template(self, at=None, category='MARKETING'):
        message = Message.objects.create(
            conversation=self.conversation,
            direction='outbound',
            message_type='template',
            content=json.dumps({'name': 'promo_demo', 'language': {'code': 'es'}}),
            metadata={
                'template_name': 'promo_demo',
                'template_category': category,
            },
        )
        if at:
            Message.objects.filter(pk=message.pk).update(created_at=at)
        return message

    def _inbound(self, content, at=None, automated=False, read=False):
        message = Message.objects.create(
            conversation=self.conversation,
            direction='inbound',
            message_type='text',
            content=content,
            is_automated=automated,
            is_read=read,
        )
        if at:
            Message.objects.filter(pk=message.pk).update(created_at=at)
        return message

    def _post(self, value):
        payload = {'entry': [{'changes': [{'value': value}]}]}
        return self.client.post(
            '/webhook/', data=json.dumps(payload), content_type='application/json',
        )

    def _webhook_value(self, body, msg_id='wamid.auto1'):
        return {
            'messaging_product': 'whatsapp',
            'metadata': {'phone_number_id': '123'},
            'contacts': [{'profile': {'name': 'Negocio Demo'}, 'wa_id': PHONE}],
            'messages': [{
                'from': PHONE,
                'id': msg_id,
                'type': 'text',
                'text': {'body': body},
            }],
        }

    # ── Classification ──────────────────────────────────────────────────

    def test_normalize_text_ignores_accents_case_and_punctuation(self):
        self.assertEqual(normalize_text('¡HOLA, gracias!'), 'hola gracias')

    def test_auto_reply_after_marketing_template_is_classified(self):
        self._marketing_template()
        match = classify_message(self.conversation, 'text', MARKETING_TEXT)
        self.assertIsNotNone(match)
        self.assertEqual(match['matched_pattern'], 'gracias por escribirnos')
        self.assertEqual(match['template_name'], 'promo_demo')
        self.assertEqual(match['window_minutes'], 2)

    def test_auto_reply_inside_two_minute_window_is_classified(self):
        self._marketing_template(at=timezone.now() - timedelta(minutes=1))
        match = classify_message(self.conversation, 'text', MARKETING_TEXT)
        self.assertIsNotNone(match)

    def test_auto_reply_five_minutes_later_is_not_classified(self):
        self._marketing_template(at=timezone.now() - timedelta(minutes=5))
        match = classify_message(self.conversation, 'text', MARKETING_TEXT)
        self.assertIsNone(match)

    def test_regular_reply_after_marketing_template_is_not_classified(self):
        self._marketing_template()
        match = classify_message(
            self.conversation, 'text', 'Hola, quiero pedir un domicilio a la 45.',
        )
        self.assertIsNone(match)

    def test_auto_reply_without_marketing_template_is_not_classified(self):
        match = classify_message(self.conversation, 'text', MARKETING_TEXT)
        self.assertIsNone(match)

    def test_auto_reply_after_utility_template_is_not_classified(self):
        self._marketing_template(category='UTILITY')
        match = classify_message(self.conversation, 'text', MARKETING_TEXT)
        self.assertIsNone(match)

    def test_auto_reply_outside_marketing_window_is_not_classified(self):
        self._set_config(marketing_window_minutes=1)
        self._marketing_template(at=timezone.now() - timedelta(minutes=3))
        match = classify_message(self.conversation, 'text', MARKETING_TEXT)
        self.assertIsNone(match)

    def test_disabled_guard_does_not_classify(self):
        self._set_config(enabled=False)
        self._marketing_template()
        match = classify_message(self.conversation, 'text', MARKETING_TEXT)
        self.assertIsNone(match)

    def test_guard_can_run_without_marketing_window(self):
        self._set_config(require_marketing=False)
        match = classify_message(self.conversation, 'text', MARKETING_TEXT)
        self.assertIsNotNone(match)
        self.assertIsNone(match['template_name'])

    def test_custom_patterns_replace_defaults(self):
        self._set_config(patterns=['solo este patron'])
        self._marketing_template()
        self.assertIsNone(classify_message(self.conversation, 'text', MARKETING_TEXT))
        self.assertIsNotNone(
            classify_message(self.conversation, 'text', 'Solo este patrón, señor.'),
        )

    # ── Webhook integration ─────────────────────────────────────────────

    def test_webhook_captures_auto_reply_as_read(self):
        self._marketing_template()
        response = self._post(self._webhook_value(MARKETING_TEXT))
        self.assertEqual(response.status_code, 200)

        message = Message.objects.get(whatsapp_message_id='wamid.auto1')
        self.assertTrue(message.is_automated)
        self.assertTrue(message.is_read)
        self.assertEqual(
            message.metadata['auto_reply_guard']['matched_pattern'],
            'gracias por escribirnos',
        )
        self.assertEqual(
            self.conversation.messages.filter(
                direction='inbound', is_read=False,
            ).count(),
            0,
        )

    def test_webhook_leaves_a_real_reply_unread(self):
        self._marketing_template()
        response = self._post(self._webhook_value('Hola, necesito un domicilio'))
        self.assertEqual(response.status_code, 200)

        message = Message.objects.get(whatsapp_message_id='wamid.auto1')
        self.assertFalse(message.is_automated)
        self.assertFalse(message.is_read)

    def test_webhook_flags_but_keeps_unread_when_mark_read_is_off(self):
        self._set_config(mark_read=False)
        self._marketing_template()
        self._post(self._webhook_value(MARKETING_TEXT))

        message = Message.objects.get(whatsapp_message_id='wamid.auto1')
        self.assertTrue(message.is_automated)
        self.assertFalse(message.is_read)

    def test_send_template_records_marketing_metadata(self):
        template = WhatsAppTemplate.objects.create(
            name='promo_demo',
            language='es',
            category='MARKETING',
            status='APPROVED',
            template_id='999',
            components=[{'type': 'body', 'text': 'Hola'}],
        )
        admin = User.objects.create_user('admin', password='x', is_staff=True)
        self.client.force_authenticate(user=admin)

        with patch('api.views._enqueue_outbound_send'):
            response = self.client.post(
                f'/api/conversations/{self.conversation.id}/send_template/',
                {'template_id': template.id},
                format='json',
            )
        self.assertEqual(response.status_code, 201)
        message = Message.objects.get(
            conversation=self.conversation,
            direction='outbound',
            message_type='template',
        )
        self.assertEqual(message.metadata['template_name'], 'promo_demo')
        self.assertEqual(message.metadata['template_category'], 'MARKETING')

    def test_automated_message_does_not_trigger_push_notification(self):
        self._marketing_template()
        user = User.objects.create_user('agent', password='x')
        Message.objects.create(
            conversation=self.conversation,
            direction='outbound',
            message_type='text',
            content='hola',
        )
        from api.models import ConversationTake
        ConversationTake.create_take(
            conversation=self.conversation, created_by=user, duration_minutes=10,
        )
        with patch('api.models.PushSubscription') as push_model:
            push_model.objects.filter.return_value.exists.return_value = True
            self._post(self._webhook_value(MARKETING_TEXT))
            push_model.objects.filter.assert_not_called()

    # ── Review API ──────────────────────────────────────────────────────

    def test_review_api_lists_captured_replies_with_summary(self):
        self._inbound(MARKETING_TEXT, automated=True, read=True)
        self._inbound('mensaje real')
        admin = User.objects.create_user('admin', password='x', is_staff=True)
        self.client.force_authenticate(user=admin)

        response = self.client.get('/api/auto-replies/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['summary']['total'], 1)
        self.assertEqual(response.data['summary']['unread'], 0)
        row = response.data['results'][0]
        self.assertEqual(row['conversation_name'], 'Negocio Demo')
        self.assertEqual(row['contact_phone'], PHONE)

    def test_review_api_is_admin_only(self):
        user = User.objects.create_user('agent', password='x')
        self.client.force_authenticate(user=user)
        response = self.client.get('/api/auto-replies/')
        self.assertEqual(response.status_code, 403)

    def test_config_endpoint_returns_effective_config_and_defaults(self):
        admin = User.objects.create_user('admin', password='x', is_staff=True)
        self.client.force_authenticate(user=admin)

        response = self.client.get('/api/auto-replies/config/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['key'], 'auto_reply_guard')
        self.assertTrue(response.data['value']['enabled'])
        self.assertEqual(response.data['value']['marketing_window_minutes'], 2)
        self.assertGreater(len(response.data['value']['patterns']), 0)
        self.assertEqual(
            response.data['value']['patterns'],
            response.data['defaults']['patterns'],
        )

    def test_set_value_upserts_the_guard_config(self):
        admin = User.objects.create_user('admin', password='x', is_staff=True)
        self.client.force_authenticate(user=admin)
        value = {
            'enabled': True,
            'mark_read': True,
            'require_marketing': False,
            'marketing_window_minutes': 24,
            'patterns': ['solo esto'],
        }

        response = self.client.post(
            '/api/bot-config/set_value/',
            {'key': 'auto_reply_guard', 'value': value},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        _clear_config_cache()

        saved = BotConfig.objects.get(key='auto_reply_guard')
        self.assertEqual(saved.value['marketing_window_minutes'], 24)
        self.assertEqual(saved.value['patterns'], ['solo esto'])

    def test_backfill_flags_historical_reply_relative_to_its_timestamp(self):
        # Both messages are older than the 2-minute window measured from
        # "now", but the reply landed one minute after the template, so the
        # backfill must evaluate the window against the reply's own timestamp.
        self._marketing_template(at=timezone.now() - timedelta(minutes=10))
        historical = self._inbound(
            MARKETING_TEXT, at=timezone.now() - timedelta(minutes=9),
        )
        admin = User.objects.create_user('admin', password='x', is_staff=True)
        self.client.force_authenticate(user=admin)

        response = self.client.post(
            '/api/auto-replies/backfill/', {'days': 7}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['detected'], 1)
        self.assertEqual(response.data['marked_read'], 1)

        historical.refresh_from_db()
        self.assertTrue(historical.is_automated)
        self.assertTrue(historical.is_read)
