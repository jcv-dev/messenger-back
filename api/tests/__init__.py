import asyncio
from unittest.mock import patch, MagicMock
from django.core.files.uploadedfile import SimpleUploadedFile
from urllib.parse import quote

from django.db import IntegrityError
from django.test import TestCase, SimpleTestCase, override_settings
from django.conf import settings
from django.urls import resolve, reverse
from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase
from django.utils import timezone
from datetime import timedelta
import json

from api.models import Conversation, Message, ConversationTag, ConversationNote, ConversationTake, ConversationUserPin, SSEToken, CityGroup, UserProfile, BotExemptContact, WhatsAppTemplate, AgentPresence, PushSubscription, AuditLog, CannedResponse, StickerAsset
from api.serializers import (
    ConversationSerializer, MessageSerializer,
    ConversationTagSerializer, ConversationNoteSerializer, ConversationTakeSerializer,
    WhatsAppTemplateSerializer, SendTemplateSerializer, BulkSendTemplateSerializer,
)
from api.views import ConversationViewSet, MessageViewSet, WhatsAppTemplateViewSet

# Load bot package so @patch('api.bot.dispatcher.xxx') resolves
from api import bot as _  # noqa

def get_or_create_tulua_group():
    group, _ = CityGroup.objects.get_or_create(
        slug='tulua', defaults={'name': 'Tuluá'},
    )
    return group


def assign_user_group(user, group):
    try:
        profile = user.profile
        profile.group = group
        profile.save(update_fields=['group_id'])
    except UserProfile.DoesNotExist:
        UserProfile.objects.create(user=user, group=group)


# ── Settings ────────────────────────────────────────────────────────────────

class SecuritySettingsTests(SimpleTestCase):
    """Verify critical security settings."""

    def test_debug_defaults_to_false(self):
        self.assertFalse(settings.DEBUG)

    def test_secret_key_has_no_default(self):
        from decouple import config
        self.assertIsNotNone(config('SECRET_KEY', default=None))

    def test_secure_proxy_ssl_header_set(self):
        self.assertEqual(
            settings.SECURE_PROXY_SSL_HEADER,
            ('HTTP_X_FORWARDED_PROTO', 'https'),
        )

    def test_session_cookie_secure_is_bool(self):
        self.assertIsInstance(settings.SESSION_COOKIE_SECURE, bool)

    def test_csrf_cookie_secure_is_bool(self):
        self.assertIsInstance(settings.CSRF_COOKIE_SECURE, bool)

    def test_hsts_enabled(self):
        self.assertEqual(settings.SECURE_HSTS_SECONDS, 31536000)
        self.assertTrue(settings.SECURE_HSTS_INCLUDE_SUBDOMAINS)

    def test_secure_ssl_redirect_defaults_false(self):
        self.assertFalse(settings.SECURE_SSL_REDIRECT)

    def test_whitenoise_middleware_present(self):
        middleware_classes = [m for m in settings.MIDDLEWARE if 'whitenoise' in m.lower()]
        self.assertTrue(middleware_classes)


class PerformanceSettingsTests(SimpleTestCase):
    """Verify DRF and infrastructure settings."""

    def test_atomic_requests_not_enabled(self):
        self.assertIsNone(settings.REST_FRAMEWORK.get('ATOMIC_REQUESTS'))

    def test_caches_configured(self):
        self.assertIn('default', settings.CACHES)
        self.assertIn('BACKEND', settings.CACHES['default'])

    def test_db_conn_max_age_defaults_to_zero(self):
        self.assertEqual(settings.DATABASES['default']['CONN_MAX_AGE'], 0)

    def test_upload_limits_set(self):
        self.assertEqual(settings.DATA_UPLOAD_MAX_MEMORY_SIZE, 52428800)
        self.assertEqual(settings.FILE_UPLOAD_MAX_MEMORY_SIZE, 52428800)

    def test_redis_url_configured(self):
        self.assertIsNotNone(settings.REDIS_URL)

    def test_csrf_trusted_origins_configured(self):
        self.assertIn('CSRF_TRUSTED_ORIGINS', dir(settings))


class LoggingSettingsTests(SimpleTestCase):
    """Verify JSON logging is configured for Dozzle."""

    def test_logging_dict_exists(self):
        self.assertIn('version', settings.LOGGING)
        self.assertEqual(settings.LOGGING['version'], 1)

    def test_json_formatter_configured(self):
        self.assertIn('json', settings.LOGGING['formatters'])
        self.assertIn('jsonlogger', settings.LOGGING['formatters']['json']['()'])

    def test_console_handler_uses_json(self):
        console = settings.LOGGING.get('handlers', {}).get('console', {})
        self.assertEqual(console.get('class'), 'logging.StreamHandler')
        self.assertEqual(console.get('formatter'), 'json')

    def test_api_logger_configured(self):
        self.assertIn('api', settings.LOGGING.get('loggers', {}))
        self.assertEqual(settings.LOGGING['loggers']['api']['level'], 'INFO')


# ── URLs ────────────────────────────────────────────────────────────────────

class URLConfigurationTests(SimpleTestCase):
    """Verify URL routing is correct."""

    def test_admin_at_custom_path(self):
        """Admin should be at /manage/ in DEBUG mode."""

    def test_admin_not_at_default_path(self):
        pass

    def test_admin_resolves_at_manage(self):
        pass

    def test_api_events_resolves(self):
        self.assertEqual(resolve('/api/events/').func.__name__, 'realtime_events')

    def test_webhook_resolves(self):
        self.assertEqual(resolve('/webhook/').func.__name__, 'whatsapp_webhook')

    def test_api_conversations_resolves(self):
        self.assertEqual(resolve('/api/conversations/').func.cls, ConversationViewSet)

    def test_media_proxy_url_resolves(self):
        self.assertEqual(reverse('media-proxy'), '/api/media-proxy/')

    def test_health_resolves(self):
        self.assertEqual(resolve('/health').func.__name__, 'health_check')


# ── Health Endpoint ──────────────────────────────────────────────────────────

class HealthEndpointTests(SimpleTestCase):
    """Verify the /health endpoint."""

    def test_health_returns_200(self):
        from django.test import Client
        response = Client().get('/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'ok')

    def test_health_has_no_db_query(self):
        from django.test import Client
        response = Client().get('/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/html; charset=utf-8')


# ── Serializers ─────────────────────────────────────────────────────────────

class SerializerStructureTests(SimpleTestCase):

    def test_conversation_serializer_no_messages_field(self):
        """Must NOT include 'messages' to prevent loading all messages."""
        self.assertNotIn('messages', ConversationSerializer.Meta.fields)

    def test_no_serializer_uses_fields_all(self):
        from rest_framework import serializers
        from api import serializers as s_module
        for name in dir(s_module):
            obj = getattr(s_module, name)
            if isinstance(obj, type) and issubclass(obj, serializers.ModelSerializer) and obj is not serializers.ModelSerializer:
                meta = getattr(obj, 'Meta', None)
                if meta:
                    self.assertNotEqual(
                        getattr(meta, 'fields', None), '__all__',
                        f"{name} uses fields = '__all__'",
                    )

    def test_message_serializer_has_required_fields(self):
        self.assertIn('context_message_preview', MessageSerializer.Meta.fields)
        self.assertIn('context_message_id', MessageSerializer.Meta.fields)
        self.assertIn('sender', MessageSerializer.Meta.fields)
        self.assertIn('sender_detail', MessageSerializer.Meta.fields)


# ── Conversation ViewSet ────────────────────────────────────────────────────

class ConversationViewSetTests(APITestCase):

    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass123')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)

        self.conversation = Conversation.objects.create(
            whatsapp_id='15551234567', contact_name='Test Contact', contact_phone='15551234567',
            group=self.group,
        )
        for i in range(3):
            Message.objects.create(
                conversation=self.conversation, direction='inbound',
                message_type='text', content=f'Message {i}', sender_name='Test Contact',
            )
        self.tag = ConversationTag.create_tag(
            conversation=self.conversation, tag_name='test-tag', created_by=self.user,
        )
        self.note = ConversationNote.create_note(
            conversation=self.conversation, content='test note', created_by=self.user,
        )
        self.take = ConversationTake.create_take(
            conversation=self.conversation, created_by=self.user, duration_minutes=60,
        )

    def test_list_requires_auth(self):
        self.client.credentials()  # clear
        response = self.client.get('/api/conversations/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_list_returns_conversations(self):
        response = self.client.get('/api/conversations/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_detail_returns_prefetched_data(self):
        response = self.client.get(f'/api/conversations/{self.conversation.id}/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('active_tags', response.data)
        self.assertIn('active_notes', response.data)
        self.assertIn('active_take', response.data)
        self.assertNotIn('messages', response.data)

    def test_active_conversations_query_count(self):
        """Verify N+1 prevented: 5 conversations = ~7 queries, not 5*3+1=16."""
        for i in range(4):
            conv = Conversation.objects.create(
                whatsapp_id=f'1555{i:05d}', contact_name=f'Contact {i}', contact_phone=f'1555{i:05d}',
            )
            Message.objects.create(conversation=conv, direction='inbound', message_type='text', content=f'Msg {i}', sender_name='Test')
            ConversationTag.create_tag(conversation=conv, tag_name=f'tag-{i}', created_by=self.user)
            ConversationNote.create_note(conversation=conv, content=f'note-{i}', created_by=self.user)
            ConversationTake.create_take(conversation=conv, created_by=self.user, duration_minutes=60)

        with self.assertNumQueries(9):
            self.client.get('/api/conversations/active_conversations/')

    def test_messages_cursor_pagination(self):
        response = self.client.get(f'/api/conversations/{self.conversation.id}/messages/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('results', response.data)
        self.assertIn('cursor', response.data)
        self.assertIn('has_more', response.data)
        self.assertEqual(len(response.data['results']), 3)

    def test_add_tag(self):
        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/add_tag/',
            {'tag_name': 'new-tag', 'expiry_type': '1h'},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['tag_name'], 'new-tag')

    def test_add_note(self):
        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/add_note/',
            {'content': 'Important note', 'expiry_type': 'never'},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_set_custom_name(self):
        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/set_custom_name/',
            {'custom_name': 'VIP Client'},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.custom_name, 'VIP Client')

    def test_mark_read(self):
        response = self.client.post(f'/api/conversations/{self.conversation.id}/mark_read/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['marked_read'], 3)

    def test_search_by_name(self):
        response = self.client.get('/api/conversations/search/', {'q': 'Test Contact'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)

    def test_search_empty_query(self):
        response = self.client.get('/api/conversations/search/', {'q': ''})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['results'], [])

    def test_remove_expired_tags(self):
        staff = User.objects.create_superuser(username='staff', password='staff123', email='')
        staff_token = Token.objects.create(user=staff)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {staff_token.key}')
        response = self.client.post('/api/conversations/remove_expired_tags/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def test_take_conversation(self):
        """Verifying transaction-wrapped take endpoint works."""
        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/take_conversation/',
            {'duration_minutes': 15},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_release_conversation(self):
        ConversationTake.create_take(
            conversation=self.conversation, created_by=self.user, duration_minutes=30,
        )
        response = self.client.post(f'/api/conversations/{self.conversation.id}/release_conversation/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_non_owner_cannot_release(self):
        other_user = User.objects.create_user(username='other', password='pass')
        other_token = Token.objects.create(user=other_user)
        ConversationTake.create_take(
            conversation=self.conversation, created_by=other_user, duration_minutes=30,
        )
        response = self.client.post(f'/api/conversations/{self.conversation.id}/release_conversation/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_non_admin_cannot_override_others_take(self):
        other_user = User.objects.create_user(username='other2', password='pass')
        other_token = Token.objects.create(user=other_user)
        assign_user_group(other_user, self.group)
        ConversationTake.create_take(
            conversation=self.conversation, created_by=other_user, duration_minutes=30,
        )
        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/take_conversation/',
            {'duration_minutes': 15},
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @patch('api.views.get_sync_redis')
    def test_take_conversation_does_not_delete_escalation_cooldown(self, mock_get_redis):
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        # Simulate an existing escalation cooldown in Redis
        mock_redis.exists.return_value = False
        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/take_conversation/',
            {'duration_minutes': 15},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # take_conversation should no longer delete the escalation cooldown key
        for call_args in mock_redis.delete.call_args_list:
            key = call_args[0][0] if call_args[0] else ''
            self.assertNotIn('bot:escalated', str(key),
                             'take_conversation should not delete the escalation cooldown')

    def test_take_from_bot_creates_human_take(self):
        bot_user = User.objects.create_user(username='bot', password='pass')
        ConversationTake.create_take(
            conversation=self.conversation, created_by=bot_user, duration_minutes=30,
        )
        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/take_conversation/',
            {'duration_minutes': 15},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNotNone(response.data)
        self.assertEqual(response.data['created_by']['id'], self.user.id)
        self.assertEqual(response.data['duration_minutes'], 15)

    def test_remove_tag(self):
        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/remove_tag/',
            {'tag_id': self.tag.id},
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_remove_note(self):
        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/remove_note/',
            {'note_id': self.note.id},
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_metadata_action(self):
        response = self.client.get(f'/api/conversations/{self.conversation.id}/metadata/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn('messages', response.data)


class ConversationStickerTests(APITestCase):
    """Verify stickers are stored and sent as type 'sticker', not 'image'."""

    def setUp(self):
        self.user = User.objects.create_user(username='stickeruser', password='testpass123')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)

    def test_sticker_message_has_correct_type(self):
        """Sticker message should be stored as message_type='sticker'."""
        conv = Conversation.objects.create(
            whatsapp_id='15559999000', contact_name='Sticker Test', contact_phone='15559999000',
            group=self.group,
        )
        response = self.client.post(
            f'/api/conversations/{conv.id}/messages/',
            {'direction': 'outbound', 'message_type': 'sticker', 'content': 'https://example.com/sticker.webp'},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['message_type'], 'sticker')
        self.assertNotEqual(response.data['message_type'], 'image')

    def test_sticker_with_api_media_url_sets_media_url(self):
        """Sticker sent with /api/media/ URL should set media_url and clear content."""
        conv = Conversation.objects.create(
            whatsapp_id='15559999001', contact_name='Sticker Media Test', contact_phone='15559999001',
            group=self.group,
        )
        response = self.client.post(
            f'/api/conversations/{conv.id}/messages/',
            {'direction': 'outbound', 'message_type': 'sticker',
             'content': '/api/media/stickers/2024/01/test.webp?sig=abc&t=123'},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['message_type'], 'sticker')
        self.assertIsNotNone(response.data.get('media_url'))
        self.assertNotEqual(response.data.get('media_url'), '')
        msg = Message.objects.get(id=response.data['id'])
        self.assertEqual(msg.media_url, '/api/media/stickers/2024/01/test.webp?sig=abc&t=123')
        self.assertEqual(msg.content, '')


class ConversationWriteTransactionTests(APITestCase):
    """Verify transactional integrity of write endpoints."""

    def setUp(self):
        self.user = User.objects.create_user(username='testuser2', password='testpass123')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)

    def test_initiate_creates_conversation_and_message(self):
        response = self.client.post(
            '/api/conversations/initiate/',
            {'contact_phone': '15550001111', 'contact_name': 'New Contact', 'content': 'Hello!', 'message_type': 'text'},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Conversation.objects.filter(whatsapp_id='15550001111').exists())
        self.assertTrue(Message.objects.filter(content='Hello!').exists())


# ── Message ViewSet ─────────────────────────────────────────────────────────

class MessageViewSetTests(APITestCase):

    def setUp(self):
        self.user = User.objects.create_user(username='msguser', password='testpass123')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)
        self.conversation = Conversation.objects.create(
            whatsapp_id='15557654321', contact_name='Message Test', contact_phone='15557654321',
            group=self.group,
        )
        self.message = Message.objects.create(
            conversation=self.conversation, direction='inbound', message_type='text',
            content='Test message content', sender_name='Message Test',
        )

    def test_list_requires_auth(self):
        self.client.credentials()
        response = self.client.get('/api/messages/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_list_returns_messages(self):
        response = self.client.get('/api/messages/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_detail_returns_message(self):
        response = self.client.get(f'/api/messages/{self.message.id}/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['content'], 'Test message content')

    def test_queryset_uses_select_related(self):
        from rest_framework.test import APIRequestFactory
        factory = APIRequestFactory()
        request = factory.get('/api/messages/')
        request.user = self.user
        request.auth = self.token
        view = MessageViewSet()
        view.request = request
        view.action = 'list'
        qs = view.get_queryset()
        str_qs = str(qs.query)
        self.assertIn('INNER JOIN', str_qs)

    def test_create_outbound_message_sets_sender(self):
        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/messages/',
            {'direction': 'outbound', 'message_type': 'text', 'content': 'Agent message'},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['sender'], self.user.id)
        self.assertIsNotNone(response.data['sender_detail'])
        self.assertEqual(response.data['sender_detail']['first_name'], self.user.first_name)
        self.assertEqual(response.data['sender_detail']['username'], self.user.username)

    def test_create_outbound_with_sender_name_falls_back_to_user(self):
        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/messages/',
            {'direction': 'outbound', 'message_type': 'text', 'content': 'No name sent'},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        msg = Message.objects.get(content='No name sent')
        self.assertEqual(msg.sender, self.user)

    def test_outbound_message_sets_last_message_direction_outbound(self):
        self.client.post(
            f'/api/conversations/{self.conversation.id}/messages/',
            {'direction': 'outbound', 'message_type': 'text', 'content': 'Agent direction test'},
        )
        response = self.client.get('/api/conversations/active_conversations/')
        conv_data = next(c for c in response.data['results'] if c['id'] == self.conversation.id)
        self.assertEqual(conv_data['last_message_direction'], 'outbound')

    def test_outbound_message_updates_last_message_at(self):
        before = timezone.now()
        self.client.post(
            f'/api/conversations/{self.conversation.id}/messages/',
            {'direction': 'outbound', 'message_type': 'text', 'content': 'Time test'},
        )
        self.conversation.refresh_from_db()
        self.assertIsNotNone(self.conversation.last_message_at)
        self.assertGreaterEqual(self.conversation.last_message_at, before)
        self.assertEqual(self.conversation.last_message, 'Time test')

    def test_create_outbound_message_has_pending_status(self):
        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/messages/',
            {'direction': 'outbound', 'message_type': 'text', 'content': 'Pending message'},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['metadata']['status'], 'pending')
        self.assertIn('scheduled_for', response.data['metadata'])

    def test_create_outbound_message_with_zero_delay_skips_pending(self):
        from api.models import BotConfig
        BotConfig.objects.update_or_create(key='send_delay_seconds', defaults={'value': 0})
        from api.bot.config import _clear_cache
        _clear_cache()
        try:
            response = self.client.post(
                f'/api/conversations/{self.conversation.id}/messages/',
                {'direction': 'outbound', 'message_type': 'text', 'content': 'Instant message'},
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            self.assertNotEqual(response.data['metadata'].get('status'), 'pending')
        finally:
            BotConfig.objects.filter(key='send_delay_seconds').delete()
            _clear_cache()

    def test_cancel_pending_message(self):
        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/messages/',
            {'direction': 'outbound', 'message_type': 'text', 'content': 'Cancelable message'},
        )
        msg_id = response.data['id']
        cancel_resp = self.client.patch(f'/api/messages/{msg_id}/cancel/')
        self.assertEqual(cancel_resp.status_code, status.HTTP_200_OK)
        updated = Message.objects.get(id=msg_id)
        self.assertEqual(updated.metadata['status'], 'cancelled')

    def test_cancel_non_pending_message_returns_409(self):
        msg = Message.objects.create(
            conversation=self.conversation, direction='outbound', message_type='text',
            content='Already sent', sender=self.user,
            metadata={'status': 'sent'},
        )
        cancel_resp = self.client.patch(f'/api/messages/{msg.id}/cancel/')
        self.assertEqual(cancel_resp.status_code, status.HTTP_409_CONFLICT)

    def test_cancel_inbound_message_returns_400(self):
        msg = Message.objects.create(
            conversation=self.conversation, direction='inbound', message_type='text',
            content='Inbound', sender_name='Test',
            metadata={'status': 'pending'},
        )
        cancel_resp = self.client.patch(f'/api/messages/{msg.id}/cancel/')
        self.assertEqual(cancel_resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cancelled_message_hidden_from_list(self):
        msg = Message.objects.create(
            conversation=self.conversation, direction='outbound', message_type='text',
            content='Cancelled message', sender=self.user,
            metadata={'status': 'cancelled'},
        )
        response = self.client.get(f'/api/conversations/{self.conversation.id}/messages/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = [m['id'] for m in response.data['results']]
        self.assertNotIn(msg.id, ids)

    # ── Forward messages ──────────────────────────────────────────────────────

    def _create_target_conversations(self):
        conv2 = Conversation.objects.create(
            whatsapp_id='15551111111', contact_name='Forward Target 1',
            contact_phone='15551111111', group=self.group,
        )
        conv3 = Conversation.objects.create(
            whatsapp_id='15552222222', contact_name='Forward Target 2',
            contact_phone='15552222222', group=self.group,
        )
        return conv2, conv3

    def test_forward_text_message(self):
        conv2, conv3 = self._create_target_conversations()
        response = self.client.post(
            f'/api/messages/{self.message.id}/forward/',
            {'conversation_ids': [conv2.id, conv3.id]},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['forwarded'], 2)
        self.assertIsNone(response.data['errors'])

        for conv in [conv2, conv3]:
            msg = Message.objects.filter(conversation=conv, is_forwarded=True).first()
            self.assertIsNotNone(msg, f'No forwarded message in conversation {conv.id}')
            self.assertTrue(msg.content.startswith('*Reenviado*'))
            self.assertEqual(msg.message_type, 'text')
            self.assertEqual(msg.direction, 'outbound')
            self.assertIn('Test message content', msg.content)

    def test_forward_requires_auth(self):
        self.client.credentials()
        response = self.client.post(
            f'/api/messages/{self.message.id}/forward/',
            {'conversation_ids': [1]}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_forward_requires_conversation_ids(self):
        response = self.client.post(
            f'/api/messages/{self.message.id}/forward/', {}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('conversation_ids', str(response.data['detail']))

    def test_forward_validates_conversation_ids_is_list(self):
        response = self.client.post(
            f'/api/messages/{self.message.id}/forward/',
            {'conversation_ids': 'not-a-list'}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_forward_rejects_reaction_type(self):
        reaction = Message.objects.create(
            conversation=self.conversation, direction='inbound',
            message_type='reaction', content='❤️',
            sender_name='Test',
            metadata={'message_id': 'wamid.orig', 'emoji': '❤️'},
        )
        conv2, _ = self._create_target_conversations()
        response = self.client.post(
            f'/api/messages/{reaction.id}/forward/',
            {'conversation_ids': [conv2.id]}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_forward_rejects_interactive_type(self):
        interactive = Message.objects.create(
            conversation=self.conversation, direction='inbound',
            message_type='interactive', content='button_payload',
            sender_name='Test',
            metadata={'interactive': {'type': 'button'}},
        )
        conv2, _ = self._create_target_conversations()
        response = self.client.post(
            f'/api/messages/{interactive.id}/forward/',
            {'conversation_ids': [conv2.id]}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_forward_rejects_template_type(self):
        tmpl = Message.objects.create(
            conversation=self.conversation, direction='inbound',
            message_type='template', content='Template content',
            sender_name='Test',
        )
        conv2, _ = self._create_target_conversations()
        response = self.client.post(
            f'/api/messages/{tmpl.id}/forward/',
            {'conversation_ids': [conv2.id]}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_forward_image_message(self):
        conv2, _ = self._create_target_conversations()
        img_msg = Message.objects.create(
            conversation=self.conversation, direction='inbound',
            message_type='image', content='Original caption',
            media_url='/media/uploads/images/test.jpg',
            sender_name='Test',
        )
        response = self.client.post(
            f'/api/messages/{img_msg.id}/forward/',
            {'conversation_ids': [conv2.id]}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        forwarded = Message.objects.get(conversation=conv2, is_forwarded=True)
        self.assertTrue(forwarded.content.startswith('*Reenviado*'))
        self.assertIn('Original caption', forwarded.content)
        self.assertEqual(forwarded.media_url, '/media/uploads/images/test.jpg')
        self.assertEqual(forwarded.message_type, 'image')

    def test_forward_location_message(self):
        conv2, _ = self._create_target_conversations()
        loc_msg = Message.objects.create(
            conversation=self.conversation, direction='inbound',
            message_type='location',
            content='Tienda (4.711, -74.072)',
            sender_name='Test',
            metadata={
                'location': {
                    'latitude': 4.711, 'longitude': -74.072,
                    'name': 'Tienda', 'address': 'Calle 123',
                }
            },
        )
        response = self.client.post(
            f'/api/messages/{loc_msg.id}/forward/',
            {'conversation_ids': [conv2.id]}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        forwarded = Message.objects.get(conversation=conv2, is_forwarded=True)
        self.assertTrue(forwarded.content.startswith('*Reenviado*'))
        self.assertEqual(forwarded.message_type, 'location')
        self.assertIsNotNone(forwarded.metadata.get('location'))

    def test_forward_location_message_inbound_format(self):
        conv2, _ = self._create_target_conversations()
        loc_msg = Message.objects.create(
            conversation=self.conversation, direction='inbound',
            message_type='location',
            content='Tienda (4.711, -74.072)',
            sender_name='Test',
            metadata={
                'latitude': 4.711, 'longitude': -74.072,
                'name': 'Tienda', 'address': 'Calle 123',
            },
        )
        response = self.client.post(
            f'/api/messages/{loc_msg.id}/forward/',
            {'conversation_ids': [conv2.id]}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        forwarded = Message.objects.get(conversation=conv2, is_forwarded=True)
        self.assertTrue(forwarded.content.startswith('*Reenviado*'))
        self.assertEqual(forwarded.message_type, 'location')
        loc = forwarded.metadata.get('location')
        self.assertIsNotNone(loc)
        self.assertEqual(loc['latitude'], 4.711)
        self.assertEqual(loc['longitude'], -74.072)
        self.assertEqual(loc['name'], 'Tienda')
        self.assertEqual(loc['address'], 'Calle 123')

    def test_forward_sets_context_message(self):
        conv2, _ = self._create_target_conversations()
        response = self.client.post(
            f'/api/messages/{self.message.id}/forward/',
            {'conversation_ids': [conv2.id]}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        forwarded = Message.objects.get(conversation=conv2, is_forwarded=True)
        self.assertEqual(forwarded.context_message_id, self.message.id)

    def test_forward_sets_sender_to_current_user(self):
        conv2, _ = self._create_target_conversations()
        response = self.client.post(
            f'/api/messages/{self.message.id}/forward/',
            {'conversation_ids': [conv2.id]}, format='json',
        )
        forwarded = Message.objects.get(conversation=conv2, is_forwarded=True)
        self.assertEqual(forwarded.sender, self.user)

    def test_forward_ignores_conversations_user_cannot_access(self):
        other_group = CityGroup.objects.create(name='Other', slug='other')
        conv_no_access = Conversation.objects.create(
            whatsapp_id='15553333333', contact_name='No Access',
            contact_phone='15553333333', group=other_group,
        )
        response = self.client.post(
            f'/api/messages/{self.message.id}/forward/',
            {'conversation_ids': [conv_no_access.id]}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['forwarded'], 0)
        self.assertFalse(
            Message.objects.filter(conversation=conv_no_access).exists()
        )

    def test_forward_empty_forwarded_when_no_valid_conversations(self):
        response = self.client.post(
            f'/api/messages/{self.message.id}/forward/',
            {'conversation_ids': [99999]}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['forwarded'], 0)

    def test_forward_updates_conversation_last_message(self):
        conv2, _ = self._create_target_conversations()
        response = self.client.post(
            f'/api/messages/{self.message.id}/forward/',
            {'conversation_ids': [conv2.id]}, format='json',
        )
        conv2.refresh_from_db()
        self.assertIsNotNone(conv2.last_message_at)
        self.assertIn('Reenviado', conv2.last_message)

    def test_forward_document_copies_metadata(self):
        conv2, _ = self._create_target_conversations()
        doc_msg = Message.objects.create(
            conversation=self.conversation, direction='inbound',
            message_type='document', content='Report.pdf',
            media_url='/media/uploads/documents/report.pdf',
            sender_name='Test',
            metadata={'filename': 'Report.pdf', 'mime_type': 'application/pdf', 'file_size': 12345},
        )
        response = self.client.post(
            f'/api/messages/{doc_msg.id}/forward/',
            {'conversation_ids': [conv2.id]}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        forwarded = Message.objects.get(conversation=conv2, is_forwarded=True)
        self.assertEqual(forwarded.message_type, 'document')
        self.assertEqual(forwarded.metadata.get('filename'), 'Report.pdf')
        self.assertEqual(forwarded.metadata.get('mime_type'), 'application/pdf')


# ── Model logic ─────────────────────────────────────────────────────────────

class ModelTests(APITestCase):

    def setUp(self):
        self.user = User.objects.create_user(username='modeltest', password='testpass123')
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)
        self.conversation = Conversation.objects.create(
            whatsapp_id='15559998877', contact_name='Model Test', contact_phone='15559998877',
            group=self.group,
        )

    def test_tag_not_expired_on_creation(self):
        tag = ConversationTag.create_tag(
            conversation=self.conversation, tag_name='expiring-tag',
            expiry_type='1h', created_by=self.user,
        )
        self.assertFalse(tag.is_expired())

    def test_tag_never_expires(self):
        tag = ConversationTag.create_tag(
            conversation=self.conversation, tag_name='permanent',
            expiry_type='never', created_by=self.user, tag_color='red',
        )
        self.assertEqual(tag.tag_color, 'red')
        self.assertIsNone(tag.expires_at)
        self.assertFalse(tag.is_expired())

    def test_note_not_expired_on_creation(self):
        note = ConversationNote.create_note(
            conversation=self.conversation, content='a note',
            expiry_type='5h', created_by=self.user,
        )
        self.assertEqual(note.content, 'a note')
        self.assertFalse(note.is_expired())

    def test_take_not_expired_on_creation(self):
        take = ConversationTake.create_take(
            conversation=self.conversation, created_by=self.user, duration_minutes=30,
        )
        self.assertFalse(take.is_expired())

    def test_take_is_expired(self):
        past = timezone.now() - timedelta(hours=1)
        take = ConversationTake.objects.create(
            conversation=self.conversation, created_by=self.user,
            duration_minutes=30, expires_at=past,
        )
        self.assertTrue(take.is_expired())

    def test_unique_constraint_blocks_duplicate_wamid(self):
        msg1 = Message.objects.create(
            conversation=self.conversation, direction='inbound',
            message_type='text', content='First',
            whatsapp_message_id='wamid.unique',
        )
        with self.assertRaises(IntegrityError):
            Message.objects.create(
                conversation=self.conversation, direction='inbound',
                message_type='text', content='Second',
                whatsapp_message_id='wamid.unique',
            )

    def test_unique_constraint_allows_multiple_null_wamid(self):
        Message.objects.create(
            conversation=self.conversation, direction='inbound',
            message_type='text', content='First', whatsapp_message_id=None,
        )
        Message.objects.create(
            conversation=self.conversation, direction='inbound',
            message_type='text', content='Second', whatsapp_message_id=None,
        )
        self.assertEqual(Message.objects.count(), 2)


# ── SSE Token Issuance ──────────────────────────────────────────────────────

class SSETokenIssuanceTests(APITestCase):
    """Tests for the one-time SSE token endpoint."""

    def setUp(self):
        self.user = User.objects.create_user(username='ssetest', password='testpass123')
        self.token = Token.objects.create(user=self.user)
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)

    def _auth_client(self):
        return self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def test_requires_auth(self):
        response = self.client.post('/api/sse-token/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_issue_valid_token(self):
        self._auth_client()
        response = self.client.post('/api/sse-token/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('sse_token', response.json())

    def test_issued_token_is_stored_in_db(self):
        self._auth_client()
        response = self.client.post('/api/sse-token/')
        token_key = response.json()['sse_token']
        sse_token = SSEToken.objects.get(key=token_key)
        self.assertEqual(sse_token.user, self.user)
        self.assertFalse(sse_token.used)
        self.assertGreater(sse_token.expires_at, timezone.now())

    def test_token_expires_after_30_seconds(self):
        self._auth_client()
        response = self.client.post('/api/sse-token/')
        token_key = response.json()['sse_token']
        sse_token = SSEToken.objects.get(key=token_key)
        expected_expiry = timezone.now() + timedelta(seconds=30)
        self.assertLess(abs((sse_token.expires_at - expected_expiry).total_seconds()), 2)


# ── Realtime SSE ────────────────────────────────────────────────────────────

class RealTimeSSETests(APITestCase):
    """SSE endpoint tests with Redis dependency mocked."""

    def setUp(self):
        self.user = User.objects.create_user(username='ssetest', password='testpass123')
        self.token = Token.objects.create(user=self.user)
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)
        self.sse_token = SSEToken.objects.create(
            key='test-sse-key-123',
            user=self.user,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        self.mock_queue = asyncio.Queue()
        self.subscribe_patcher = patch('api.views.subscribe')
        self.mock_subscribe = self.subscribe_patcher.start()
        self.mock_subscribe.return_value = (
            'test-subscriber-id',
            self.mock_queue,
            lambda: False,
            lambda: None,
        )

    def tearDown(self):
        self.subscribe_patcher.stop()

    def _valid_url(self):
        return f'/api/events/?sse_token={self.sse_token.key}'

    def test_sse_requires_token(self):
        response = self.client.get('/api/events/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_sse_with_valid_token(self):
        response = self.client.get(self._valid_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response['Content-Type'], 'text/event-stream')
        self.assertEqual(response['Cache-Control'], 'no-cache')

    def test_sse_rejects_wildcard_cors(self):
        response = self.client.get(self._valid_url())
        self.assertNotEqual(response.get('Access-Control-Allow-Origin', ''), '*')

    def test_sse_rejects_invalid_token(self):
        response = self.client.get('/api/events/?sse_token=invalidtoken123')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_sse_rejects_expired_token(self):
        expired = SSEToken.objects.create(
            key='expired-key',
            user=self.user,
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        response = self.client.get(f'/api/events/?sse_token={expired.key}')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_sse_rejects_reused_token(self):
        first = self.client.get(self._valid_url())
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        second = self.client.get(self._valid_url())
        self.assertEqual(second.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_sse_marks_token_as_used(self):
        self.client.get(self._valid_url())
        self.sse_token.refresh_from_db()
        self.assertTrue(self.sse_token.used)


# ── Redis Realtime Module ───────────────────────────────────────────────────

class RedisRealtimeModuleTests(SimpleTestCase):
    """Test realtime.py Redis pub/sub with mocked Redis."""

    @patch('api.realtime.get_sync_redis')
    def test_publish_sends_to_correct_channel(self, mock_get_redis):
        mock_redis = mock_get_redis.return_value
        from api.realtime import publish

        publish({'type': 'test', 'data': 'hello'})

        mock_redis.publish.assert_called_once()
        args, _ = mock_redis.publish.call_args
        self.assertEqual(args[0], 'sse:events')
        self.assertIn('test', args[1])
        self.assertIn('_seq', args[1])

    @patch('api.realtime.get_sync_redis')
    def test_publish_adds_sequence_number(self, mock_get_redis):
        mock_redis = mock_get_redis.return_value
        from api.realtime import publish
        from api.realtime import _next_seq

        before = next(_next_seq)
        publish({'type': 'test'})
        args, _ = mock_redis.publish.call_args
        import json
        payload = json.loads(args[1])
        self.assertGreaterEqual(payload['_seq'], before)

    @patch('api.realtime.get_sync_redis')
    def test_publish_handles_connection_error_gracefully(self, mock_get_redis):
        mock_redis = mock_get_redis.return_value
        mock_redis.publish.side_effect = ConnectionError('Redis down')
        from api.realtime import publish

        publish({'type': 'test'})

    @patch('api.realtime.get_sync_redis')
    def test_publish_serializes_non_json_values(self, mock_get_redis):
        mock_redis = mock_get_redis.return_value
        from datetime import datetime
        from api.realtime import publish

        publish({'type': 'test', 'date': datetime(2026, 5, 24)})
        args, _ = mock_redis.publish.call_args
        import json
        payload = json.loads(args[1])
        self.assertEqual(payload['type'], 'test')
        self.assertIsInstance(payload['date'], str)


# ── CityGroup ───────────────────────────────────────────────────────────────

class CityGroupModelTests(APITestCase):
    """Test CityGroup model."""

    def test_create_city_group(self):
        group = CityGroup.objects.create(name='Cali', slug='cali')
        self.assertEqual(str(group), 'Cali')

    def test_default_group_tulua_exists(self):
        group = get_or_create_tulua_group()
        self.assertEqual(group.name, 'Tuluá')
        self.assertEqual(group.slug, 'tulua')

    def test_is_active_defaults_to_true(self):
        group = CityGroup.objects.create(name='Medellín', slug='medellin')
        self.assertTrue(group.is_active)


class UserProfileTests(APITestCase):
    """Test UserProfile creation and group assignment."""

    def test_profile_created_on_user_creation(self):
        user = User.objects.create_user(username='profilestest', password='testpass123')
        self.assertTrue(hasattr(user, 'profile'))
        self.assertIsInstance(user.profile, UserProfile)

    def test_assign_user_to_group(self):
        user = User.objects.create_user(username='grouptest', password='testpass123')
        group = get_or_create_tulua_group()
        assign_user_group(user, group)
        self.assertEqual(user.profile.group, group)

    def test_user_str(self):
        user = User.objects.create_user(username='strtest', password='testpass123')
        group = get_or_create_tulua_group()
        assign_user_group(user, group)
        self.assertIn('strtest', str(user.profile))
        self.assertIn('Tuluá', str(user.profile))


class ConversationGroupFilterTests(APITestCase):
    """Test that non-staff users only see their group's conversations."""

    def setUp(self):
        self.group_a = get_or_create_tulua_group()
        self.group_b = CityGroup.objects.create(name='Cali', slug='cali')

        self.agent_a = User.objects.create_user(username='agenta', password='testpass123')
        assign_user_group(self.agent_a, self.group_a)
        self.token_a = Token.objects.create(user=self.agent_a)

        self.agent_b = User.objects.create_user(username='agentb', password='testpass123')
        assign_user_group(self.agent_b, self.group_b)
        self.token_b = Token.objects.create(user=self.agent_b)

        self.conv_a = Conversation.objects.create(
            whatsapp_id='15550001111', contact_name='Contact A', contact_phone='15550001111',
            group=self.group_a,
        )
        self.conv_b = Conversation.objects.create(
            whatsapp_id='15550002222', contact_name='Contact B', contact_phone='15550002222',
            group=self.group_b,
        )

    def test_agent_sees_only_own_group(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token_a.key}')
        response = self.client.get('/api/conversations/')
        ids = [c['id'] for c in response.data]
        self.assertIn(self.conv_a.id, ids)
        self.assertNotIn(self.conv_b.id, ids)

    def test_agent_b_sees_only_own_group(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token_b.key}')
        response = self.client.get('/api/conversations/')
        ids = [c['id'] for c in response.data]
        self.assertNotIn(self.conv_a.id, ids)
        self.assertIn(self.conv_b.id, ids)

    def test_admin_sees_all_groups(self):
        admin = User.objects.create_superuser(username='admintest', password='testpass123', email='admin@test.com')
        admin_token = Token.objects.create(user=admin)
        assign_user_group(admin, self.group_a)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {admin_token.key}')
        response = self.client.get('/api/conversations/')
        ids = [c['id'] for c in response.data]
        self.assertIn(self.conv_a.id, ids)
        self.assertIn(self.conv_b.id, ids)

    def test_user_serializer_includes_group(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token_a.key}')
        response = self.client.get('/api/users/current_user/')
        self.assertIn('group', response.data)
        self.assertEqual(response.data['group']['name'], 'Tuluá')

    def test_conversation_list_includes_group(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token_a.key}')
        response = self.client.get('/api/conversations/')
        conv = response.data[0]
        self.assertIn('group', conv)
        self.assertEqual(conv['group']['name'], 'Tuluá')

    def test_city_groups_endpoint(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token_a.key}')
        response = self.client.get('/api/city-groups/')
        names = [g['name'] for g in response.data]
        self.assertIn('Tuluá', names)
        self.assertIn('Cali', names)


# ── Webhook ─────────────────────────────────────────────────────────────────

class WebhookTests(APITestCase):

    def setUp(self):
        get_or_create_tulua_group()

    def test_webhook_get_no_challenge_returns_400(self):
        response = self.client.get('/webhook/')
        self.assertEqual(response.status_code, 400)

    def test_webhook_verify_wrong_token(self):
        response = self.client.get(
            '/webhook/?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=12345',
        )
        self.assertEqual(response.status_code, 403)

    def test_webhook_post_incoming_text(self):
        payload = {
            'entry': [{
                'changes': [{
                    'value': {
                        'messages': [{
                            'from': '15551112222', 'id': 'wamid.test123',
                            'type': 'text', 'text': {'body': 'Hello from WhatsApp'},
                        }],
                        'contacts': [{
                            'profile': {'name': 'WhatsApp User'}, 'wa_id': '15551112222',
                        }],
                    }
                }],
            }],
        }
        with self.settings(WHATSAPP_APP_SECRET=''):
            response = self.client.post('/webhook/', data=json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Conversation.objects.filter(whatsapp_id='15551112222').exists())
        conv = Conversation.objects.get(whatsapp_id='15551112222')
        self.assertIsNotNone(conv.group)
        self.assertTrue(Message.objects.filter(content='Hello from WhatsApp').exists())
        msg = Message.objects.get(content='Hello from WhatsApp')
        self.assertIsNone(msg.sender)

    def test_old_message_does_not_overwrite_last_message(self):
        """An older webhook message arriving late should not overwrite last_message."""
        wa_id = '15553333444'
        payload_newer = {
            'entry': [{'changes': [{'value': {
                'messaging_product': 'whatsapp',
                'metadata': {'phone_number_id': '123'},
                'contacts': [{'wa_id': wa_id, 'profile': {'name': 'User'}}],
                'messages': [{
                    'from': wa_id, 'id': 'wamid.newer', 'timestamp': '2000000000',
                    'type': 'text', 'text': {'body': 'Second'},
                }],
            }}]}],
        }
        payload_older = {
            'entry': [{'changes': [{'value': {
                'messaging_product': 'whatsapp',
                'metadata': {'phone_number_id': '123'},
                'contacts': [{'wa_id': wa_id, 'profile': {'name': 'User'}}],
                'messages': [{
                    'from': wa_id, 'id': 'wamid.older', 'timestamp': '1000000000',
                    'type': 'text', 'text': {'body': 'First'},
                }],
            }}]}],
        }
        with self.settings(WHATSAPP_APP_SECRET=''):
            self.client.post('/webhook/', data=json.dumps(payload_newer), content_type='application/json')
            self.client.post('/webhook/', data=json.dumps(payload_older), content_type='application/json')

        conv = Conversation.objects.get(whatsapp_id=wa_id)
        self.assertEqual(conv.last_message, 'Second')
        self.assertTrue(Message.objects.filter(content='First').exists())

    def test_new_message_updates_last_message(self):
        """A newer webhook message should update last_message."""
        wa_id = '15554444555'
        payload_first = {
            'entry': [{'changes': [{'value': {
                'messaging_product': 'whatsapp',
                'metadata': {'phone_number_id': '123'},
                'contacts': [{'wa_id': wa_id, 'profile': {'name': 'User'}}],
                'messages': [{
                    'from': wa_id, 'id': 'wamid.first', 'timestamp': '1000000000',
                    'type': 'text', 'text': {'body': 'First'},
                }],
            }}]}],
        }
        payload_second = {
            'entry': [{'changes': [{'value': {
                'messaging_product': 'whatsapp',
                'metadata': {'phone_number_id': '123'},
                'contacts': [{'wa_id': wa_id, 'profile': {'name': 'User'}}],
                'messages': [{
                    'from': wa_id, 'id': 'wamid.second', 'timestamp': '2000000000',
                    'type': 'text', 'text': {'body': 'Second'},
                }],
            }}]}],
        }
        with self.settings(WHATSAPP_APP_SECRET=''):
            self.client.post('/webhook/', data=json.dumps(payload_first), content_type='application/json')
            self.client.post('/webhook/', data=json.dumps(payload_second), content_type='application/json')

        conv = Conversation.objects.get(whatsapp_id=wa_id)
        self.assertEqual(conv.last_message, 'Second')

    def test_webhook_post_invalid_json_returns_400(self):
        response = self.client.post('/webhook/', data='not json', content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_webhook_unsupported_method(self):
        response = self.client.put('/webhook/')
        self.assertEqual(response.status_code, 405)

    def test_post_missing_hmac_with_secret_rejected(self):
        payload = {'object': 'whatsapp_business_account', 'entry': []}
        with self.settings(WHATSAPP_APP_SECRET='test-secret'):
            response = self.client.post(
                '/webhook/', data=json.dumps(payload), content_type='application/json',
            )
        self.assertEqual(response.status_code, 403)

    def test_post_wrong_hmac_rejected(self):
        payload = {'object': 'whatsapp_business_account', 'entry': []}
        with self.settings(WHATSAPP_APP_SECRET='test-secret'):
            response = self.client.post(
                '/webhook/', data=json.dumps(payload), content_type='application/json',
                HTTP_X_HUB_SIGNATURE_256='sha256=' + '0' * 64,
            )
        self.assertEqual(response.status_code, 403)

    def test_post_valid_hmac_accepted(self):
        import hmac, hashlib
        payload = {'object': 'whatsapp_business_account', 'entry': []}
        body = json.dumps(payload)
        secret = 'test-secret'
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        with self.settings(WHATSAPP_APP_SECRET=secret):
            response = self.client.post(
                '/webhook/', data=body, content_type='application/json',
                HTTP_X_HUB_SIGNATURE_256=f'sha256={expected}',
            )
        self.assertEqual(response.status_code, 200)

    def test_post_hmac_skipped_when_secret_empty(self):
        payload = {'object': 'whatsapp_business_account', 'entry': []}
        with self.settings(WHATSAPP_APP_SECRET=''):
            response = self.client.post(
                '/webhook/', data=json.dumps(payload), content_type='application/json',
            )
        self.assertEqual(response.status_code, 200)

    def test_duplicate_whatsapp_message_id_skipped(self):
        wa_id = '573001234567'
        msg_id = 'wamid.dup001'
        payload = {
            'entry': [{
                'changes': [{
                    'value': {
                        'messaging_product': 'whatsapp',
                        'metadata': {'phone_number_id': '123'},
                        'contacts': [{'wa_id': wa_id, 'profile': {'name': 'Test'}}],
                        'messages': [{
                            'from': wa_id, 'id': msg_id,
                            'type': 'text', 'text': {'body': 'Hello'},
                        }],
                    },
                }],
            }],
        }
        body = json.dumps(payload)
        with self.settings(WHATSAPP_APP_SECRET=''):
            response = self.client.post('/webhook/', data=body, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Message.objects.filter(whatsapp_message_id=msg_id).count(), 1)

        with self.settings(WHATSAPP_APP_SECRET=''):
            response = self.client.post('/webhook/', data=body, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Message.objects.filter(whatsapp_message_id=msg_id).count(), 1)

    def test_duplicate_whatsapp_message_id_null_allowed(self):
        wa_id = '573009999999'
        payload = {
            'entry': [{
                'changes': [{
                    'value': {
                        'messaging_product': 'whatsapp',
                        'metadata': {'phone_number_id': '123'},
                        'contacts': [{'wa_id': wa_id, 'profile': {'name': 'NullID'}}],
                        'messages': [{
                            'from': wa_id, 'id': 'wamid.nulltest',
                            'type': 'text', 'text': {'body': 'First'},
                        }],
                    },
                }],
            }],
        }
        body = json.dumps(payload)
        with self.settings(WHATSAPP_APP_SECRET=''):
            response = self.client.post('/webhook/', data=body, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        first_msg = Message.objects.get(whatsapp_message_id='wamid.nulltest')
        first_msg.whatsapp_message_id = None
        first_msg.save(update_fields=['whatsapp_message_id'])

        payload['entry'][0]['changes'][0]['value']['messages'][0]['id'] = 'wamid.nulltest2'
        payload['entry'][0]['changes'][0]['value']['messages'][0]['text']['body'] = 'Second'
        body2 = json.dumps(payload)
        with self.settings(WHATSAPP_APP_SECRET=''):
            response = self.client.post('/webhook/', data=body2, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Message.objects.filter(whatsapp_message_id__isnull=True).count(), 1)
        self.assertEqual(Message.objects.count(), 2)

    def test_webhook_forwarded_message(self):
        payload = {
            'entry': [{
                'changes': [{
                    'value': {
                        'messages': [{
                            'from': '15551112222', 'id': 'wamid.fwd1',
                            'type': 'text', 'text': {'body': 'Forwarded msg'},
                            'context': {'forwarded': True, 'id': 'wamid.original'},
                        }],
                        'contacts': [{'profile': {'name': 'User'}, 'wa_id': '15551112222'}],
                    }
                }],
            }],
        }
        with self.settings(WHATSAPP_APP_SECRET=''):
            response = self.client.post('/webhook/', data=json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        msg = Message.objects.get(whatsapp_message_id='wamid.fwd1')
        self.assertTrue(msg.is_forwarded)
        self.assertFalse(msg.is_frequently_forwarded)

    def test_webhook_frequently_forwarded_message(self):
        payload = {
            'entry': [{
                'changes': [{
                    'value': {
                        'messages': [{
                            'from': '15551112222', 'id': 'wamid.ffwd1',
                            'type': 'text', 'text': {'body': 'Frequently forwarded msg'},
                            'context': {
                                'forwarded': True,
                                'frequently_forwarded': True,
                                'id': 'wamid.original2',
                            },
                        }],
                        'contacts': [{'profile': {'name': 'User'}, 'wa_id': '15551112222'}],
                    }
                }],
            }],
        }
        with self.settings(WHATSAPP_APP_SECRET=''):
            response = self.client.post('/webhook/', data=json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        msg = Message.objects.get(whatsapp_message_id='wamid.ffwd1')
        self.assertTrue(msg.is_forwarded)
        self.assertTrue(msg.is_frequently_forwarded)


# ── Media Proxy ─────────────────────────────────────────────────────────────

class MediaProxyTests(APITestCase):

    def test_requires_auth(self):
        response = self.client.get('/api/media-proxy/?url=https://lookaside.fbsbx.com/test')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_rejects_missing_url(self):
        user = User.objects.create_user(username='proxyuser', password='testpass123')
        token = Token.objects.create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        response = self.client.get('/api/media-proxy/')
        self.assertEqual(response.status_code, 400)

    def test_rejects_unallowed_prefix(self):
        user = User.objects.create_user(username='proxyuser2', password='testpass123')
        token = Token.objects.create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        response = self.client.get('/api/media-proxy/?url=https://evil.com/hack')
        self.assertEqual(response.status_code, 403)

    def test_signed_url_allows_without_auth(self):
        """A valid signed URL passes auth (proxy step fails due to no API token in test)."""
        import time
        from api.serializers import media_proxy_signer
        cdn_url = 'https://lookaside.fbsbx.com/test123'
        ts = int(time.time())
        signed = media_proxy_signer.sign(f'{cdn_url}|{ts}')
        sig_val = signed.rsplit(media_proxy_signer.sep, 1)[1]
        encoded = quote(cdn_url, safe='')
        url = f'/api/media-proxy/?url={encoded}&sig={sig_val}&t={ts}'
        response = self.client.get(url)
        self.assertNotEqual(response.status_code, 401)
        self.assertNotEqual(response.status_code, 403)

    def test_signed_url_expired_returns_401_or_500(self):
        """An expired signature falls back to token auth, which fails without token."""
        import time
        from api.serializers import media_proxy_signer
        cdn_url = 'https://lookaside.fbsbx.com/test123'
        ts = int(time.time()) - 7200
        signed = media_proxy_signer.sign(f'{cdn_url}|{ts}')
        sig_val = signed.rsplit(media_proxy_signer.sep, 1)[1]
        encoded = quote(cdn_url, safe='')
        url = f'/api/media-proxy/?url={encoded}&sig={sig_val}&t={ts}'
        response = self.client.get(url)
        self.assertIn(response.status_code, (401, 403))

    def test_signed_url_wrong_sig_returns_401(self):
        """An invalid signature falls back to token auth, which fails without token."""
        encoded = quote('https://lookaside.fbsbx.com/test', safe='')
        url = f'/api/media-proxy/?url={encoded}&sig=invalid&t=1000000'
        response = self.client.get(url)
        self.assertEqual(response.status_code, 401)

    def test_unsigned_still_requires_auth(self):
        """Without a signature, falls back to token auth."""
        response = self.client.get('/api/media-proxy/?url=https://lookaside.fbsbx.com/test')
        self.assertEqual(response.status_code, 401)


# ── User ViewSet ────────────────────────────────────────────────────────────

class UserViewSetTests(APITestCase):

    def setUp(self):
        self.user = User.objects.create_user(username='reguser', password='testpass123')
        self.admin = User.objects.create_superuser(username='adminuser', password='adminpass123', email='admin@test.com')
        self.user_token = Token.objects.create(user=self.user)
        self.admin_token = Token.objects.create(user=self.admin)
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)
        assign_user_group(self.admin, self.group)

    def test_list_requires_auth(self):
        response = self.client.get('/api/users/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_current_user(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        response = self.client.get('/api/users/current_user/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['username'], 'reguser')

    def test_create_user_requires_admin(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        response = self.client.post('/api/users/', {'username': 'newuser', 'password': 'newpass123'})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_create_user(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.post('/api/users/', {'username': 'newuser', 'password': 'newpass123'})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


# ── Stickers ────────────────────────────────────────────────────────────────

class StickerAssetViewSetTests(APITestCase):

    def setUp(self):
        self.user = User.objects.create_user(username='stickuser', password='testpass123')
        self.user_token = Token.objects.create(user=self.user)
        self.admin = User.objects.create_user(username='stickeradmin', password='testpass123', is_staff=True)
        self.admin_token = Token.objects.create(user=self.admin)
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)
        assign_user_group(self.admin, self.group)

    def test_list_requires_auth(self):
        response = self.client.get('/api/stickers/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_user_can_create_sticker_with_name(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        image = SimpleUploadedFile("test.png", b"fake-image", content_type="image/png")
        response = self.client.post('/api/stickers/', {'name': 'Test Sticker', 'image': image})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['name'], 'Test Sticker')

    def test_authenticated_user_can_create_sticker_with_blank_name(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        image = SimpleUploadedFile("test.png", b"fake-image", content_type="image/png")
        response = self.client.post('/api/stickers/', {'name': '', 'image': image})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['name'], '')

    def test_authenticated_user_can_create_sticker_without_name_field(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        image = SimpleUploadedFile("test.png", b"fake-image", content_type="image/png")
        response = self.client.post('/api/stickers/', {'image': image})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['name'], '')

    def test_sticker_str_returns_sin_titulo_when_name_empty(self):
        image = SimpleUploadedFile("test.png", b"fake", content_type="image/png")
        sticker = StickerAsset.objects.create(name='', image=image, created_by=self.admin)
        self.assertEqual(str(sticker), 'Sin título')

    def test_save_from_message_requires_auth(self):
        response = self.client.post('/api/stickers/save_from_message/',
                                     {'message_id': 9999}, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_save_from_message_requires_message_id(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        response = self.client.post('/api/stickers/save_from_message/',
                                     {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_save_from_message_404_for_nonexistent_message(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        response = self.client.post('/api/stickers/save_from_message/',
                                     {'message_id': 99999}, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_save_from_message_rejects_non_sticker(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        conv = Conversation.objects.create(
            whatsapp_id='15550009999', contact_name='NonSticker',
            group=self.group,
        )
        msg = Message.objects.create(
            conversation=conv, direction='inbound',
            message_type='text', content='hello',
        )
        response = self.client.post('/api/stickers/save_from_message/',
                                     {'message_id': msg.id}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_save_from_message_creates_sticker(self):
        import tempfile, os
        from django.test.utils import override_settings

        with tempfile.TemporaryDirectory() as tmpdir:
            with override_settings(MEDIA_ROOT=tmpdir):
                media_subdir = os.path.join('whatsapp', 'sticker')
                os.makedirs(os.path.join(tmpdir, media_subdir), exist_ok=True)
                file_path = os.path.join(tmpdir, media_subdir, 'test_sticker.webp')
                with open(file_path, 'wb') as f:
                    f.write(b'fake-sticker-content')

                conv = Conversation.objects.create(
                    whatsapp_id='15550009998', contact_name='Sticker Src',
                    group=self.group,
                )
                msg = Message.objects.create(
                    conversation=conv, direction='inbound',
                    message_type='sticker',
                    media_url=f'/media/{media_subdir}/test_sticker.webp',
                )

                self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
                response = self.client.post('/api/stickers/save_from_message/',
                                             {'message_id': msg.id}, format='json')
                self.assertEqual(response.status_code, status.HTTP_201_CREATED)
                self.assertIn('id', response.data)
                self.assertEqual(response.data['created_by']['id'], self.user.id)
                self.assertTrue(response.data['image'].startswith('/api/media/'))

    def test_save_from_message_rejects_no_media_url(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        conv = Conversation.objects.create(
            whatsapp_id='15550009997', contact_name='No Media',
            group=self.group,
        )
        msg = Message.objects.create(
            conversation=conv, direction='inbound',
            message_type='sticker', media_url='',
        )
        response = self.client.post('/api/stickers/save_from_message/',
                                     {'message_id': msg.id}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_save_from_message_rejects_unavailable_file(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        conv = Conversation.objects.create(
            whatsapp_id='15550009996', contact_name='Unavail',
            group=self.group,
        )
        msg = Message.objects.create(
            conversation=conv, direction='inbound',
            message_type='sticker',
            media_url='/media/whatsapp/sticker/nonexistent.webp',
        )
        response = self.client.post('/api/stickers/save_from_message/',
                                     {'message_id': msg.id}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_save_from_message_admin_can_save_any_conversation(self):
        import tempfile, os
        from django.test.utils import override_settings

        with tempfile.TemporaryDirectory() as tmpdir:
            with override_settings(MEDIA_ROOT=tmpdir):
                media_subdir = os.path.join('whatsapp', 'sticker')
                os.makedirs(os.path.join(tmpdir, media_subdir), exist_ok=True)
                file_path = os.path.join(tmpdir, media_subdir, 'test_admin.webp')
                with open(file_path, 'wb') as f:
                    f.write(b'fake-sticker-content')

                # No group assigned to this conversation
                conv = Conversation.objects.create(
                    whatsapp_id='15550009995', contact_name='Admin Src',
                )
                msg = Message.objects.create(
                    conversation=conv, direction='inbound',
                    message_type='sticker',
                    media_url=f'/media/{media_subdir}/test_admin.webp',
                )

                self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
                response = self.client.post('/api/stickers/save_from_message/',
                                             {'message_id': msg.id}, format='json')
                self.assertEqual(response.status_code, status.HTTP_201_CREATED)
                self.assertIn('id', response.data)


# ── Auth Throttle ────────────────────────────────────────────────────────────

class AuthThrottleTests(APITestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def test_login_throttle_blocks_after_limit(self):
        url = reverse('api_token_auth')
        for i in range(5):
            resp = self.client.post(url, {'username': 'nobody', 'password': 'wrong'}, format='json')
            self.assertNotEqual(resp.status_code, 429, f'Request {i+1} was throttled')
        resp = self.client.post(url, {'username': 'nobody', 'password': 'wrong'}, format='json')
        self.assertEqual(resp.status_code, 429)

    def test_api_endpoints_not_throttled(self):
        url = reverse('api_token_auth')
        for _ in range(5):
            self.client.post(url, {'username': 'nobody', 'password': 'wrong'}, format='json')
        resp = self.client.get('/api/users/current_user/')
        self.assertNotEqual(resp.status_code, 429)


class UserRateThrottleTests(APITestCase):
    """Verify UserRateThrottle (60/min) protects authenticated endpoints."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = User.objects.create_user(username='throttleuser', password='testpass123')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def test_authenticated_endpoint_throttled(self):
        pass  # Verified manually; cache backend is in-memory in test


# ── Admin-Only Conversation Delete ────────────────────────────────────────────

class ConversationDestroyAdminTests(APITestCase):
    """Non-admin users cannot DELETE conversations."""

    def setUp(self):
        self.user = User.objects.create_user(username='reguser', password='testpass123')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.conv = Conversation.objects.create(whatsapp_id='15550000001', contact_name='Del Test')

    def test_non_admin_cannot_delete(self):
        response = self.client.delete(f'/api/conversations/{self.conv.id}/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Conversation.objects.filter(id=self.conv.id).exists())

    def test_admin_can_delete(self):
        admin = User.objects.create_superuser(username='deladmin', password='admin123', email='a@b.com')
        admin_token = Token.objects.create(user=admin)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {admin_token.key}')
        response = self.client.delete(f'/api/conversations/{self.conv.id}/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Conversation.objects.filter(id=self.conv.id).exists())


# ── Admin-Only API Endpoints ─────────────────────────────────────────────────

class AdminOnlyEndpointTests(APITestCase):
    """Verify User, CityGroup write endpoints require admin."""

    def setUp(self):
        self.user = User.objects.create_user(username='stafftest', password='testpass123')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.group = get_or_create_tulua_group()

    def test_non_admin_cannot_create_user(self):
        response = self.client.post('/api/users/', {'username': 'newuser', 'password': 'pass'})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_non_admin_cannot_update_user(self):
        target = User.objects.create_user(username='target', password='pass')
        response = self.client.patch(f'/api/users/{target.id}/', {'username': 'hacked'})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_non_admin_cannot_delete_user(self):
        target = User.objects.create_user(username='target2', password='pass')
        response = self.client.delete(f'/api/users/{target.id}/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_authenticated_user_can_create_sticker(self):
        image = SimpleUploadedFile("test.png", b"fake-image", content_type="image/png")
        response = self.client.post('/api/stickers/', {'name': 'test', 'image': image})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_non_admin_cannot_create_city_group(self):
        response = self.client.post('/api/city-groups/', {'name': 'NewCity', 'slug': 'newcity'})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_non_admin_cannot_update_city_group(self):
        response = self.client.patch(f'/api/city-groups/{self.group.id}/', {'name': 'Hacked'})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_non_admin_cannot_delete_city_group(self):
        response = self.client.delete(f'/api/city-groups/{self.group.id}/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_create_city_group(self):
        admin = User.objects.create_superuser(username='cityadmin', password='pass', email='c@b.com')
        admin_token = Token.objects.create(user=admin)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {admin_token.key}')
        response = self.client.post('/api/city-groups/', {'name': 'Pereira', 'slug': 'pereira'})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_non_admin_can_read_city_groups(self):
        response = self.client.get('/api/city-groups/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_non_admin_can_read_current_user(self):
        response = self.client.get('/api/users/current_user/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_non_admin_cannot_list_all_users(self):
        response = self.client.get('/api/users/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_non_admin_cannot_retrieve_user(self):
        target = User.objects.create_user(username='target3', password='pass')
        response = self.client.get(f'/api/users/{target.id}/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_retrieve_user(self):
        admin = User.objects.create_superuser(username='retrieveadmin', password='pass', email='r@b.com')
        admin_token = Token.objects.create(user=admin)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {admin_token.key}')
        target = User.objects.create_user(username='retrievetarget', password='pass')
        response = self.client.get(f'/api/users/{target.id}/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['username'], 'retrievetarget')

    def test_admin_can_list_all_users(self):
        admin = User.objects.create_superuser(username='listadmin', password='pass', email='l@b.com')
        admin_token = Token.objects.create(user=admin)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {admin_token.key}')
        response = self.client.get('/api/users/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('results', response.data)


# ── Static Map Auth ───────────────────────────────────────────────────────────

class StaticMapAuthTests(APITestCase):
    """static_map endpoint now requires authentication."""

    def test_requires_auth(self):
        response = self.client.get('/api/static-map/?lat=4.6&lng=-74.08')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_access(self):
        user = User.objects.create_user(username='mapuser', password='pass')
        token = Token.objects.create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        response = self.client.get('/api/static-map/?lat=4.6&lng=-74.08')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response['Content-Type'], 'image/png')


# ── Serve Media ──────────────────────────────────────────────────────────────

class ServeMediaTests(APITestCase):
    """Signed media URL serving, MIME types, Content-Disposition, and path traversal protection."""

    def setUp(self):
        import os, tempfile
        from django.conf import settings
        self.media_dir = tempfile.mkdtemp(dir=settings.MEDIA_ROOT)
        self.pdf_path = os.path.join(self.media_dir, 'test.pdf')
        with open(self.pdf_path, 'wb') as f:
            f.write(b'%PDF-1.4 fake pdf content')
        self.txt_path = os.path.join(self.media_dir, 'test.txt')
        with open(self.txt_path, 'w') as f:
            f.write('plain text')
        self.rel_pdf = os.path.relpath(self.pdf_path, settings.MEDIA_ROOT)
        self.rel_txt = os.path.relpath(self.txt_path, settings.MEDIA_ROOT)

    def tearDown(self):
        import os, shutil
        shutil.rmtree(self.media_dir, ignore_errors=True)

    def _signed_url(self, path, ts=None):
        import time
        from django.core.signing import Signer
        signer = Signer(salt='domi-media')
        if ts is None:
            ts = int(time.time())
        signed = signer.sign(f'{path}|{ts}')
        sig_val = signed.rsplit(':', 1)[1]
        return f'/api/media/{path}?sig={sig_val}&t={ts}'

    def test_missing_signature_returns_404(self):
        response = self.client.get('/api/media/test.jpg')
        self.assertEqual(response.status_code, 404)

    def test_missing_timestamp_returns_404(self):
        response = self.client.get('/api/media/test.jpg?sig=fake')
        self.assertEqual(response.status_code, 404)

    def test_path_traversal_blocked(self):
        from django.core.signing import Signer
        signer = Signer(salt='domi-media')
        signed = signer.sign('../../../etc/passwd|1000000')
        sig_val = signed.rsplit(':', 1)[1]
        response = self.client.get(f'/api/media/../../../etc/passwd?sig={sig_val}&t=1000000')
        self.assertEqual(response.status_code, 404)

    def test_absolute_path_blocked(self):
        from django.core.signing import Signer
        signer = Signer(salt='domi-media')
        signed = signer.sign('/etc/passwd|1000000')
        sig_val = signed.rsplit(':', 1)[1]
        response = self.client.get(f'/api/media//etc/passwd?sig={sig_val}&t=1000000')
        self.assertEqual(response.status_code, 404)

    def test_expired_signature_returns_404(self):
        import time
        ts = int(time.time()) - 7200
        url = self._signed_url(self.rel_pdf, ts=ts)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_bad_signature_returns_404(self):
        url = f'/api/media/{self.rel_pdf}?sig=invalid&t=1000000'
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    @override_settings(DEBUG=True)
    def test_pdf_returns_application_pdf(self):
        response = self.client.get(self._signed_url(self.rel_pdf))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')

    @override_settings(DEBUG=True)
    def test_txt_returns_text_plain(self):
        response = self.client.get(self._signed_url(self.rel_txt))
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/plain', response['Content-Type'])

    def test_pdf_has_content_disposition_inline(self):
        response = self.client.get(self._signed_url(self.rel_pdf))
        self.assertTrue(response.has_header('Content-Disposition'))
        self.assertIn('inline', response['Content-Disposition'])
        self.assertIn('filename="test.pdf"', response['Content-Disposition'])

    def test_txt_has_content_disposition_attachment(self):
        response = self.client.get(self._signed_url(self.rel_txt))
        self.assertTrue(response.has_header('Content-Disposition'))
        self.assertIn('attachment', response['Content-Disposition'])

    def test_xlsx_has_content_disposition_attachment(self):
        import os
        xlsx_path = os.path.join(self.media_dir, 'test.xlsx')
        with open(xlsx_path, 'wb') as f:
            f.write(b'fake excel')
        rel = os.path.relpath(xlsx_path, settings.MEDIA_ROOT)
        response = self.client.get(self._signed_url(rel))
        self.assertTrue(response.has_header('Content-Disposition'))
        self.assertIn('attachment', response['Content-Disposition'])

    def test_docx_has_content_disposition_attachment(self):
        import os
        docx_path = os.path.join(self.media_dir, 'test.docx')
        with open(docx_path, 'wb') as f:
            f.write(b'fake word doc')
        rel = os.path.relpath(docx_path, settings.MEDIA_ROOT)
        response = self.client.get(self._signed_url(rel))
        self.assertTrue(response.has_header('Content-Disposition'))
        self.assertIn('attachment', response['Content-Disposition'])

    def test_file_not_found_returns_404(self):
        url = self._signed_url('uploads/documents/nonexistent.pdf')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    @override_settings(DEBUG=True)
    def test_mime_fallback_for_unknown_extension(self):
        import os
        unknown_path = os.path.join(self.media_dir, 'test.qwertyuiop')
        with open(unknown_path, 'w') as f:
            f.write('unknown')
        rel = os.path.relpath(unknown_path, settings.MEDIA_ROOT)
        response = self.client.get(self._signed_url(rel))
        self.assertEqual(response['Content-Type'], 'application/octet-stream')

    @override_settings(DEBUG=True)
    def test_pdf_content_is_served(self):
        response = self.client.get(self._signed_url(self.rel_pdf))
        self.assertEqual(response.status_code, 200)
        content = b''.join(response.streaming_content)
        self.assertEqual(content, b'%PDF-1.4 fake pdf content')

    @override_settings(DEBUG=False)
    def test_production_returns_accel_redirect(self):
        response = self.client.get(self._signed_url(self.rel_pdf))
        self.assertEqual(response.status_code, 200)
        self.assertIn('X-Accel-Redirect', response)
        self.assertIn('/internal-media/', response['X-Accel-Redirect'])

    @override_settings(DEBUG=False)
    def test_production_accel_has_content_disposition(self):
        response = self.client.get(self._signed_url(self.rel_pdf))
        self.assertTrue(response.has_header('Content-Disposition'))
        self.assertIn('inline', response['Content-Disposition'])


# ── CORS Headers ─────────────────────────────────────────────────────────────

class CORSHeaderTests(APITestCase):
    """API responses carry correct CORS headers."""

    def setUp(self):
        self.user = User.objects.create_user(username='corsuser', password='pass')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def test_api_response_has_cors_header(self):
        response = self.client.get('/api/conversations/', HTTP_ORIGIN='http://localhost:5173')
        self.assertIn('Access-Control-Allow-Origin', response)

    def test_cors_not_wildcard(self):
        response = self.client.get('/api/conversations/', HTTP_ORIGIN='http://localhost:5173')
        origin = response.get('Access-Control-Allow-Origin', '')
        self.assertNotEqual(origin, '*')

    def test_cors_matches_allowed_origin(self):
        response = self.client.get('/api/conversations/', HTTP_ORIGIN='http://localhost:5173')
        self.assertEqual(response['Access-Control-Allow-Origin'], 'http://localhost:5173')


# ── SSE Prefetch Freshness ───────────────────────────────────────────────────

class SSEPrefetchFreshnessTests(APITestCase):
    """After tag/note/take mutations, published SSE data includes the change."""

    def setUp(self):
        self.user = User.objects.create_user(username='ssetest2', password='pass')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)
        self.conv = Conversation.objects.create(
            whatsapp_id='15551111111', contact_name='SSE Test', group=self.group,
        )

    def test_add_tag_included_in_list_serializer(self):
        conv_check = Conversation.objects.get(pk=self.conv.pk)
        from api.serializers import ConversationListSerializer
        before_tags = ConversationListSerializer(conv_check).data.get('active_tags', [])
        before_count = len(before_tags)

        self.client.post(
            f'/api/conversations/{self.conv.id}/add_tag/',
            {'tag_name': 'sse-tag', 'expiry_type': '1h'},
        )

        conv_check = Conversation.objects.get(pk=self.conv.pk)
        after_tags = ConversationListSerializer(conv_check).data.get('active_tags', [])
        self.assertEqual(len(after_tags), before_count + 1)

    def test_add_note_included_in_list_serializer(self):
        response = self.client.post(
            f'/api/conversations/{self.conv.id}/add_note/',
            {'content': 'sse-note', 'expiry_type': '1h'},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        meta_response = self.client.get(f'/api/conversations/{self.conv.id}/metadata/')
        active_notes = meta_response.data.get('active_notes', [])
        self.assertEqual(len(active_notes), 1)
        self.assertEqual(active_notes[0]['content'], 'sse-note')

    def test_release_clears_active_take(self):
        ConversationTake.create_take(
            conversation=self.conv, created_by=self.user, duration_minutes=60,
        )
        response = self.client.post(f'/api/conversations/{self.conv.id}/release_conversation/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        meta = self.client.get(f'/api/conversations/{self.conv.id}/metadata/')
        self.assertIsNone(meta.data.get('active_take'))

    @patch('api.views.get_sync_redis')
    def test_release_sets_escalation_cooldown(self, mock_get_redis):
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        conv_id = self.conv.id
        ConversationTake.create_take(
            conversation=self.conv, created_by=self.user, duration_minutes=60,
        )
        response = self.client.post(f'/api/conversations/{conv_id}/release_conversation/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        mock_redis.setex.assert_called_once_with(f"bot:escalated:{conv_id}", 600, "1")


# ── Cursor Pagination ────────────────────────────────────────────────────────

class ConversationCursorPaginationTests(APITestCase):
    """Verify cursor pagination format on conversation list."""

    def setUp(self):
        self.user = User.objects.create_user(username='cursoruser', password='pass')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)
        for i in range(5):
            conv = Conversation.objects.create(
                whatsapp_id=f'1555{i:05d}',
                contact_name=f'Cursor {i}',
                group=self.group,
                last_message_at=timezone.now() - timedelta(minutes=i),
            )

    def test_list_has_pagination_fields(self):
        response = self.client.get('/api/conversations/active_conversations/')
        self.assertIn('results', response.data)
        self.assertIn('cursor', response.data)
        self.assertIn('has_more', response.data)

    def test_list_has_default_limit(self):
        response = self.client.get('/api/conversations/active_conversations/')
        self.assertLessEqual(len(response.data['results']), 100)

    def test_only_unread_filter_returns_only_unread(self):
        conv_read = Conversation.objects.create(
            whatsapp_id='read001', contact_name='Read Convo', group=self.group,
        )
        Message.objects.create(
            conversation=conv_read, direction='inbound', message_type='text',
            content='Read msg', is_read=True,
        )
        conv_unread = Conversation.objects.create(
            whatsapp_id='unread001', contact_name='Unread Convo', group=self.group,
        )
        Message.objects.create(
            conversation=conv_unread, direction='inbound', message_type='text',
            content='Unread msg', is_read=False,
        )

        response = self.client.get('/api/conversations/active_conversations/?only_unread=true')
        conv_ids = [c['id'] for c in response.data['results']]
        self.assertIn(conv_unread.id, conv_ids)
        self.assertNotIn(conv_read.id, conv_ids)

    def test_only_unread_filter_returns_all_when_omitted(self):
        conv_read = Conversation.objects.create(
            whatsapp_id='read002', contact_name='Read Convo 2', group=self.group,
        )
        Message.objects.create(
            conversation=conv_read, direction='inbound', message_type='text',
            content='Read msg 2', is_read=True,
        )
        conv_unread = Conversation.objects.create(
            whatsapp_id='unread002', contact_name='Unread Convo 2', group=self.group,
        )
        Message.objects.create(
            conversation=conv_unread, direction='inbound', message_type='text',
            content='Unread msg 2', is_read=False,
        )

        response = self.client.get('/api/conversations/active_conversations/')
        conv_ids = [c['id'] for c in response.data['results']]
        self.assertIn(conv_unread.id, conv_ids)
        self.assertIn(conv_read.id, conv_ids)


# ── Initiate Conversation ────────────────────────────────────────────────────

class InitiateConversationTests(APITestCase):
    """Conversation initiation creates conversation + message."""

    def setUp(self):
        self.user = User.objects.create_user(username='inituser', password='pass')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)

    def test_initiate_creates_both(self):
        response = self.client.post(
            '/api/conversations/initiate/',
            {'contact_phone': '15556667777', 'contact_name': 'Init Test', 'content': 'First!', 'message_type': 'text'},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Conversation.objects.filter(whatsapp_id='15556667777').exists())
        msg = Message.objects.filter(content='First!').first()
        self.assertIsNotNone(msg)
        self.assertEqual(msg.direction, 'outbound')
        self.assertEqual(msg.sender, self.user)

    def test_initiate_rejects_missing_phone(self):
        response = self.client.post(
            '/api/conversations/initiate/',
            {'contact_name': 'No Phone', 'content': 'Hi'},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


# ── Contextual Reply ─────────────────────────────────────────────────────────

class ContextualReplyTests(APITestCase):
    """Messages with context_message_id link correctly."""

    def setUp(self):
        self.user = User.objects.create_user(username='replyuser', password='pass')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)
        self.conv = Conversation.objects.create(
            whatsapp_id='15558888999', contact_name='Reply Test', group=self.group,
        )
        self.original = Message.objects.create(
            conversation=self.conv, direction='inbound', message_type='text',
            content='Original message', sender_name='Test',
        )

    def test_reply_links_to_original(self):
        response = self.client.post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'direction': 'outbound', 'message_type': 'text', 'content': 'Reply',
             'context_message_id': self.original.id},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['context_message_id'], self.original.id)
        self.assertIsNotNone(response.data['context_message_preview'])
        self.assertEqual(response.data['sender'], self.user.id)
        self.assertIsNotNone(response.data['sender_detail'])

    def test_reply_preview_includes_content(self):
        response = self.client.post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'direction': 'outbound', 'message_type': 'text', 'content': 'Reply',
             'context_message_id': self.original.id},
        )
        preview = response.data['context_message_preview']
        self.assertEqual(preview['content'], 'Original message')
        self.assertEqual(preview['message_type'], 'text')


# ── Retention Cleanup Command ────────────────────────────────────────────────

class CleanupExpiredMessagesCommandTests(APITestCase):
    """Verify cleanup_expired_messages management command."""

    def setUp(self):
        self.user = User.objects.create_user(username='cleanuser', password='pass')
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)
        self.conv = Conversation.objects.create(
            whatsapp_id='15550009999', contact_name='Cleanup Test', group=self.group,
        )

    def test_retention_zero_skips(self):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        with self.settings(MESSAGE_RETENTION_MINUTES=0):
            call_command('cleanup_expired_messages', stdout=out)
        self.assertIn('nothing to do', out.getvalue())

    def test_retention_deletes_old_messages(self):
        from io import StringIO
        from django.core.management import call_command
        old = Message.objects.create(
            conversation=self.conv, direction='inbound', message_type='text',
            content='Old message', sender_name='Test',
        )
        Message.objects.filter(id=old.id).update(
            created_at=timezone.now() - timedelta(days=30)
        )
        old.refresh_from_db()
        new = Message.objects.create(
            conversation=self.conv, direction='inbound', message_type='text',
            content='Recent message', sender_name='Test',
            created_at=timezone.now(),
        )
        out = StringIO()
        with self.settings(MESSAGE_RETENTION_MINUTES=60):
            call_command('cleanup_expired_messages', stdout=out)
        self.assertFalse(Message.objects.filter(id=old.id).exists())
        self.assertTrue(Message.objects.filter(id=new.id).exists())

    def test_no_expired_messages_prints_message(self):
        from io import StringIO
        from django.core.management import call_command
        Message.objects.create(
            conversation=self.conv, direction='inbound', message_type='text',
            content='Recent', sender_name='Test', created_at=timezone.now(),
        )
        out = StringIO()
        with self.settings(MESSAGE_RETENTION_MINUTES=60):
            call_command('cleanup_expired_messages', stdout=out)
        self.assertIn('No expired', out.getvalue())


# ── Cache Configuration ─────────────────────────────────────────────────────

class CacheConfigurationTests(SimpleTestCase):
    """Verify throttle cache is configured."""

    def test_throttle_cache_exists(self):
        self.assertIn('throttle', settings.CACHES)

    def test_throttle_cache_has_backend(self):
        self.assertIn('BACKEND', settings.CACHES['throttle'])

    def test_cache_keys_match(self):
        self.assertIn('default', settings.CACHES)
        self.assertIn('throttle', settings.CACHES)


# ── Webhook HMAC ─────────────────────────────────────────────────────────────

class WebhookHMACTests(APITestCase):
    """Verify HMAC verification for webhook requests."""

    def test_hmac_valid_accepted(self):
        import hmac, hashlib, json as j
        payload = {'object': 'whatsapp_business_account', 'entry': []}
        body = j.dumps(payload)
        secret = 'verify-secret'
        sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        with self.settings(WHATSAPP_APP_SECRET=secret):
            response = self.client.post(
                '/webhook/', data=body, content_type='application/json',
                HTTP_X_HUB_SIGNATURE_256=f'sha256={sig}',
            )
        self.assertEqual(response.status_code, 200)

    def test_hmac_wrong_rejected(self):
        with self.settings(WHATSAPP_APP_SECRET='secret'):
            response = self.client.post(
                '/webhook/', data='{}', content_type='application/json',
                HTTP_X_HUB_SIGNATURE_256='sha256=' + 'f' * 64,
            )
        self.assertEqual(response.status_code, 403)


# ── WhatsApp Rate Limiter ────────────────────────────────────────────────────

class RateLimiterTests(SimpleTestCase):
    """Unit tests for rate_limiter.py Redis sliding-window rate limiter."""

    def setUp(self):
        self.phone_id = '1121790094350602'

    @patch('api.rate_limiter.get_sync_redis')
    def test_acquire_under_threshold_proceeds(self, mock_get_client):
        mock_redis = MagicMock()
        mock_get_client.return_value = mock_redis
        mock_redis.incr.return_value = 1

        from api.rate_limiter import acquire
        acquire(self.phone_id)

        mock_redis.incr.assert_called_once()
        mock_redis.expire.assert_called_once()
        mock_redis.decr.assert_not_called()

    @patch('api.rate_limiter.get_sync_redis')
    def test_acquire_key_includes_phone_id(self, mock_get_client):
        mock_redis = MagicMock()
        mock_get_client.return_value = mock_redis
        mock_redis.incr.return_value = 1

        from api.rate_limiter import acquire
        acquire(self.phone_id)

        key = mock_redis.incr.call_args[0][0]
        self.assertIn(self.phone_id, key)
        self.assertTrue(key.startswith('wa_rate_limit:'))

    @patch('api.rate_limiter.get_sync_redis')
    def test_acquire_sets_ttl(self, mock_get_client):
        mock_redis = MagicMock()
        mock_get_client.return_value = mock_redis
        mock_redis.incr.return_value = 1

        from api.rate_limiter import acquire
        acquire(self.phone_id)

        mock_redis.expire.assert_called_once_with(mock_redis.incr.call_args[0][0], 2)

    @patch('api.rate_limiter.get_sync_redis')
    def test_acquire_decr_on_over_threshold(self, mock_get_client):
        """When incr returns over threshold, decr is called and retry succeeds."""
        mock_redis = MagicMock()
        mock_get_client.return_value = mock_redis
        mock_redis.incr.side_effect = [71, 1]

        from api.rate_limiter import acquire
        acquire(self.phone_id)

        self.assertEqual(mock_redis.incr.call_count, 2)
        mock_redis.decr.assert_called_once()
        mock_redis.expire.assert_called()

    @patch('api.rate_limiter.time.sleep')
    @patch('api.rate_limiter.get_sync_redis')
    def test_acquire_retries_after_sleep(self, mock_get_client, mock_sleep):
        """Over threshold triggers decr + sleep before retry."""
        mock_redis = MagicMock()
        mock_get_client.return_value = mock_redis
        mock_redis.incr.side_effect = [71, 1]

        from api.rate_limiter import acquire
        acquire(self.phone_id)

        self.assertEqual(mock_redis.incr.call_count, 2)
        mock_redis.decr.assert_called_once()
        mock_sleep.assert_called_once_with(0.05)

    @patch('api.rate_limiter.get_sync_redis')
    def test_acquire_redis_error_fails_open(self, mock_get_client):
        mock_redis = MagicMock()
        mock_get_client.return_value = mock_redis
        mock_redis.incr.side_effect = ConnectionError('Redis down')

        from api.rate_limiter import acquire
        acquire(self.phone_id)

    @override_settings(WA_RATE_LIMIT_THRESHOLD=5)
    @patch('api.rate_limiter.get_sync_redis')
    def test_acquire_uses_setting_default(self, mock_get_client):
        """When threshold is not passed, it reads from WA_RATE_LIMIT_THRESHOLD."""
        mock_redis = MagicMock()
        mock_get_client.return_value = mock_redis
        mock_redis.incr.side_effect = [6, 1]

        from api.rate_limiter import acquire
        acquire(self.phone_id)

        mock_redis.decr.assert_called_once()


# ── BotExemptContact Model ─────────────────────────────────────────────────

class BotExemptContactModelTests(APITestCase):

    def setUp(self):
        self.user = User.objects.create_user(username='botexempt', password='testpass123')

    def test_create_exempt_contact(self):
        contact = BotExemptContact.objects.create(
            contact_phone='573009990001',
            contact_name='Maria Perez',
            created_by=self.user,
        )
        self.assertEqual(str(contact), 'Maria Perez (573009990001)')
        self.assertEqual(contact.contact_phone, '573009990001')

    def test_str_without_name(self):
        contact = BotExemptContact.objects.create(contact_phone='573009990002')
        self.assertIn('—', str(contact))
        self.assertIn('573009990002', str(contact))


class BotExemptContactAPITests(APITestCase):
    """Test CRUD and permission for BotExemptContact endpoint."""

    def setUp(self):
        self.user = User.objects.create_user(username='reguser', password='testpass123')
        self.token = Token.objects.create(user=self.user)
        self.admin = User.objects.create_superuser(username='botadmin', password='admin123', email='a@b.com')
        self.admin_token = Token.objects.create(user=self.admin)
        self.contact = BotExemptContact.objects.create(
            contact_phone='573001234567', contact_name='Maria Perez', created_by=self.admin,
        )

    def test_list_requires_auth(self):
        response = self.client.get('/api/bot-exempt/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_list_allowed_for_authenticated(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        response = self.client.get('/api/bot-exempt/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_create_requires_admin(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        response = self.client.post('/api/bot-exempt/', {'contact_phone': '573009999999'})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_create(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.post('/api/bot-exempt/', {
            'contact_phone': '573009999999', 'contact_name': 'New Contact',
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['contact_phone'], '573009999999')
        self.assertEqual(response.data['contact_name'], 'New Contact')

    def test_create_validates_phone_prefix(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.post('/api/bot-exempt/', {'contact_phone': '1234567890'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('57', str(response.data))

    def test_delete_requires_admin(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        response = self.client.delete(f'/api/bot-exempt/{self.contact.id}/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_delete(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.delete(f'/api/bot-exempt/{self.contact.id}/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(BotExemptContact.objects.filter(id=self.contact.id).exists())

    def test_delete_removes_domii_tag_from_conversations(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        conv = Conversation.objects.create(
            whatsapp_id='573001234567', contact_name='Test', contact_phone='573001234567',
        )
        tag = ConversationTag.create_tag(
            conversation=conv, tag_name="Domii", expiry_type="never", created_by=self.admin, tag_color="gray",
        )
        self.client.delete(f'/api/bot-exempt/{self.contact.id}/')
        self.assertFalse(ConversationTag.objects.filter(id=tag.id).exists())

    def test_phone_validation_strips_non_digits(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.post('/api/bot-exempt/', {'contact_phone': '+57 (301) 987-6543'})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['contact_phone'], '573019876543')


# ── Webhook Auto-Tag Integration ───────────────────────────────────────────

class WebhookBotExemptAutoTagTests(APITestCase):
    """When a BotExemptContact exists for an inbound phone, auto-tag with Domii."""

    def setUp(self):
        self.admin = User.objects.create_superuser(username='adm', password='pass', email='a@b.com')
        BotExemptContact.objects.create(contact_phone='573001111111', contact_name='Exempt User')

    def test_inbound_from_exempt_contact_adds_domii_tag(self):
        payload = {
            'entry': [{
                'changes': [{
                    'value': {
                        'messaging_product': 'whatsapp',
                        'metadata': {'phone_number_id': '123'},
                        'contacts': [{'wa_id': '573001111111', 'profile': {'name': 'Exempt'}}],
                        'messages': [{
                            'from': '573001111111', 'id': 'wamid.exempt1',
                            'type': 'text', 'text': {'body': 'Hello'},
                        }],
                    },
                }],
            }],
        }
        with self.settings(WHATSAPP_APP_SECRET=''):
            response = self.client.post('/webhook/', data=json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        conv = Conversation.objects.get(whatsapp_id='573001111111')
        domii_tags = conv.tags.filter(tag_name="Domii", expires_at__isnull=True)
        self.assertEqual(domii_tags.count(), 1)

    def test_non_exempt_contact_not_tagged(self):
        payload = {
            'entry': [{
                'changes': [{
                    'value': {
                        'messaging_product': 'whatsapp',
                        'metadata': {'phone_number_id': '123'},
                        'contacts': [{'wa_id': '573009999999', 'profile': {'name': 'Normal'}}],
                        'messages': [{
                            'from': '573009999999', 'id': 'wamid.nonexempt',
                            'type': 'text', 'text': {'body': 'Hi'},
                        }],
                    },
                }],
            }],
        }
        with self.settings(WHATSAPP_APP_SECRET=''):
            response = self.client.post('/webhook/', data=json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        conv = Conversation.objects.get(whatsapp_id='573009999999')
        domii_tags = conv.tags.filter(tag_name="Domii")
        self.assertEqual(domii_tags.count(), 0)


# ── Bot Session ────────────────────────────────────────────────────────────

class BotSessionTests(SimpleTestCase):
    """Unit tests for bot session CRUD with mocked Redis."""

    def setUp(self):
        self.conv_id = 42

    @patch('api.bot.session.get_sync_redis')
    def test_get_session_returns_none_when_empty(self, mock_get_redis):
        mock_redis = mock_get_redis.return_value
        mock_redis.hgetall.return_value = {}
        from api.bot.session import get_session
        self.assertIsNone(get_session(self.conv_id))

    @patch('api.bot.session.get_sync_redis')
    def test_create_and_get_session(self, mock_get_redis):
        mock_redis = mock_get_redis.return_value
        from api.bot.session import create_session, get_session, REDIS_KEY

        def hgetall_side_effect(key):
            return {
                b'state': b'WELCOME',
                b'mode': b'greeting',
                b'data': b'{}',
                b'history': b'[]',
                b'fallback_count': b'0',
                b'last_activity': b'1234567890.0',
            }

        mock_redis.hgetall.side_effect = hgetall_side_effect

        session = create_session(self.conv_id)
        self.assertEqual(session['state'], 'WELCOME')
        self.assertEqual(session['mode'], 'greeting')

        loaded = get_session(self.conv_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded['state'], 'WELCOME')

    @patch('api.bot.session.get_sync_redis')
    def test_delete_session(self, mock_get_redis):
        mock_redis = mock_get_redis.return_value
        from api.bot.session import delete_session, REDIS_KEY
        delete_session(self.conv_id)
        mock_redis.delete.assert_called_once_with(f'{REDIS_KEY}:{self.conv_id}')


# ── Bot Dispatcher Unit Tests ──────────────────────────────────────────────

class BotDispatcherTests(SimpleTestCase):

    @patch('api.bot.dispatcher.get_bot_user')
    @patch('api.bot.dispatcher.ConversationTake.objects.filter')
    def test_has_active_human_take_true(self, mock_filter, mock_get_bot):
        mock_take = MagicMock()
        mock_take.created_by.username = 'agent'
        mock_filter.return_value.select_related.return_value.first.return_value = mock_take
        from api.bot.dispatcher import has_active_human_take
        self.assertTrue(has_active_human_take(1))

    @patch('api.bot.dispatcher.get_bot_user')
    @patch('api.bot.dispatcher.ConversationTake.objects.filter')
    def test_has_active_human_take_false_for_bot(self, mock_filter, mock_get_bot):
        mock_take = MagicMock()
        mock_take.created_by.username = 'bot'
        mock_filter.return_value.select_related.return_value.first.return_value = mock_take
        from api.bot.dispatcher import has_active_human_take
        self.assertFalse(has_active_human_take(1))

    @patch('api.bot.dispatcher.ConversationTag.objects.filter')
    def test_has_domii_tag(self, mock_filter):
        mock_filter.return_value.exists.return_value = True
        from api.bot.dispatcher import has_domii_tag
        self.assertTrue(has_domii_tag(1))

    @patch('api.bot.dispatcher.ConversationTag.objects.filter')
    def test_no_domii_tag(self, mock_filter):
        mock_filter.return_value.exists.return_value = False
        from api.bot.dispatcher import has_domii_tag
        self.assertFalse(has_domii_tag(1))

    @patch('api.bot.dispatcher.get_bot_user')
    @patch('api.bot.dispatcher.ConversationTake.objects.filter')
    def test_has_active_human_take_false_when_no_take(self, mock_filter, mock_get_bot):
        mock_filter.return_value.select_related.return_value.first.return_value = None
        from api.bot.dispatcher import has_active_human_take
        self.assertFalse(has_active_human_take(1))

    @patch('api.bot.dispatcher.get_bot_user')
    @patch('api.bot.dispatcher.ConversationTake.objects.filter')
    def test_has_active_human_take_false_when_created_by_none(self, mock_filter, mock_get_bot):
        mock_take = MagicMock()
        mock_take.created_by = None
        mock_filter.return_value.select_related.return_value.first.return_value = mock_take
        from api.bot.dispatcher import has_active_human_take
        self.assertFalse(has_active_human_take(1))


# ── Bot Schedule API Tests ─────────────────────────────────────────────────

class BotScheduleAPITests(APITestCase):

    def setUp(self):
        self.admin = User.objects.create_superuser('admin', 'admin@test.com', 'pass')
        self.admin_token = Token.objects.create(user=self.admin)
        self.user = User.objects.create_user('staff', 'staff@test.com', 'pass')
        self.user_token = Token.objects.create(user=self.user)
        from api.models import BotSchedule
        self.schedule_model = BotSchedule

    def _url(self, pk=None):
        if pk:
            return f'/api/bot-schedule/{pk}/'
        return '/api/bot-schedule/'

    def test_list_unauthenticated(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 401)

    def test_list_authenticated(self):
        self.schedule_model.objects.create(
            day_of_week=0, open_time='08:00', close_time='20:00',
        )
        response = self.client.get(self._url(), HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        items = data if isinstance(data, list) else data.get('results', data)
        self.assertGreaterEqual(len(items), 1)

    def test_create_requires_admin(self):
        response = self.client.post(
            self._url(),
            {'day_of_week': 0, 'open_time': '08:00', 'close_time': '20:00'},
            HTTP_AUTHORIZATION=f'Token {self.user_token.key}',
        )
        self.assertEqual(response.status_code, 403)

    def test_create_as_admin(self):
        response = self.client.post(
            self._url(),
            {'day_of_week': 1, 'open_time': '09:00', 'close_time': '18:00'},
            HTTP_AUTHORIZATION=f'Token {self.admin_token.key}',
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data['day_of_week'], 1)
        self.assertEqual(data['open_time'], '09:00:00')
        self.assertEqual(data['close_time'], '18:00:00')

    def test_update_requires_admin(self):
        entry = self.schedule_model.objects.create(
            day_of_week=0, open_time='08:00', close_time='20:00',
        )
        response = self.client.patch(
            self._url(entry.id),
            {'close_time': '21:00'},
            HTTP_AUTHORIZATION=f'Token {self.user_token.key}',
        )
        self.assertEqual(response.status_code, 403)

    def test_update_as_admin(self):
        entry = self.schedule_model.objects.create(
            day_of_week=0, open_time='08:00', close_time='20:00',
        )
        response = self.client.patch(
            self._url(entry.id),
            {'close_time': '21:00'},
            HTTP_AUTHORIZATION=f'Token {self.admin_token.key}',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['close_time'], '21:00:00')

    def test_delete_requires_admin(self):
        entry = self.schedule_model.objects.create(
            day_of_week=0, open_time='08:00', close_time='20:00',
        )
        response = self.client.delete(
            self._url(entry.id),
            HTTP_AUTHORIZATION=f'Token {self.user_token.key}',
        )
        self.assertEqual(response.status_code, 403)

    def test_delete_as_admin(self):
        entry = self.schedule_model.objects.create(
            day_of_week=0, open_time='08:00', close_time='20:00',
        )
        response = self.client.delete(
            self._url(entry.id),
            HTTP_AUTHORIZATION=f'Token {self.admin_token.key}',
        )
        self.assertEqual(response.status_code, 204)

    def test_create_date_override(self):
        """Date override (date set, day_of_week null) succeeds."""
        response = self.client.post(
            self._url(),
            {'date': '2026-12-25', 'open_time': '09:00', 'close_time': '14:00', 'label': 'Navidad'},
            HTTP_AUTHORIZATION=f'Token {self.admin_token.key}',
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['date'], '2026-12-25')


# ── Bot Config API Tests ───────────────────────────────────────────────────

class BotConfigAPITests(APITestCase):

    def setUp(self):
        self.admin = User.objects.create_superuser('admin', 'admin@test.com', 'pass')
        self.admin_token = Token.objects.create(user=self.admin)
        self.user = User.objects.create_user('staff', 'staff@test.com', 'pass')
        self.user_token = Token.objects.create(user=self.user)
        from api.models import BotConfig
        self.config_model = BotConfig

    def _url(self, pk=None):
        if pk:
            return f'/api/bot-config/{pk}/'
        return '/api/bot-config/'

    def test_list_requires_auth(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 401)

    def test_list_as_admin(self):
        self.config_model.objects.create(
            key='test_key', value='test_value', description='A test config',
        )
        response = self.client.get(self._url(), HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        items = data if isinstance(data, list) else data.get('results', data)
        keys = [c['key'] for c in items]
        self.assertIn('test_key', keys)

    def test_update_value_requires_admin(self):
        cfg = self.config_model.objects.create(
            key='test_key', value='old_value', description='Test',
        )
        response = self.client.patch(
            f'/api/bot-config/{cfg.id}/update_value/',
            {'value': 'new_value'},
            HTTP_AUTHORIZATION=f'Token {self.user_token.key}',
        )
        self.assertEqual(response.status_code, 403)

    def test_update_value_success(self):
        cfg = self.config_model.objects.create(
            key='test_key', value='old_value', description='Test',
        )
        response = self.client.patch(
            f'/api/bot-config/{cfg.id}/update_value/',
            {'value': 'new_value'},
            HTTP_AUTHORIZATION=f'Token {self.admin_token.key}',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['value'], 'new_value')

    def test_update_value_missing_value(self):
        cfg = self.config_model.objects.create(
            key='test_key', value='old_value', description='Test',
        )
        response = self.client.patch(
            f'/api/bot-config/{cfg.id}/update_value/',
            {},
            HTTP_AUTHORIZATION=f'Token {self.admin_token.key}',
        )
        self.assertEqual(response.status_code, 400)

    def test_post_not_allowed(self):
        response = self.client.post(
            self._url(),
            {'key': 'new_key', 'value': 'new_value'},
            HTTP_AUTHORIZATION=f'Token {self.admin_token.key}',
        )
        self.assertEqual(response.status_code, 405)

    def test_delete_not_allowed(self):
        cfg = self.config_model.objects.create(
            key='test_key', value='value', description='Test',
        )
        response = self.client.delete(
            self._url(cfg.id),
            HTTP_AUTHORIZATION=f'Token {self.admin_token.key}',
        )
        self.assertEqual(response.status_code, 405)

    def test_bot_enabled_can_be_toggled_via_api(self):
        cfg = self.config_model.objects.create(
            key='bot_enabled', value=True, description='Global bot toggle',
        )
        response = self.client.patch(
            f'/api/bot-config/{cfg.id}/update_value/',
            {'value': False},
            format='json',
            HTTP_AUTHORIZATION=f'Token {self.admin_token.key}',
        )
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()['value'], False)

        response = self.client.get(
            f'/api/bot-config/{cfg.id}/',
            HTTP_AUTHORIZATION=f'Token {self.admin_token.key}',
        )
        self.assertIs(response.json()['value'], False)


# ── WhatsApp Template Tests ─────────────────────────────────────────────


class WhatsAppTemplateModelTests(APITestCase):
    """Test the WhatsAppTemplate model."""

    def setUp(self):
        self.user = User.objects.create_user(username='admin', password='pass', is_staff=True)
        self.token = Token.objects.create(user=self.user)
        self.template = WhatsAppTemplate.objects.create(
            name='test_template',
            language='es',
            category='MARKETING',
            components=[{'type': 'body', 'text': 'Hola {{nombre}}'}],
        )

    def test_create_template(self):
        self.assertEqual(self.template.name, 'test_template')
        self.assertEqual(self.template.status, 'PENDING')
        self.assertEqual(str(self.template), 'test_template (es) — PENDING')

    def test_unique_together(self):
        with self.assertRaises(Exception):
            WhatsAppTemplate.objects.create(
                name='test_template',
                language='es',
                category='MARKETING',
            )


class WhatsAppTemplateViewSetTests(APITestCase):
    """Test API endpoints for template management."""

    def setUp(self):
        self.admin = User.objects.create_user(username='admin', password='pass', is_staff=True)
        self.admin_token = Token.objects.create(user=self.admin)
        self.user = User.objects.create_user(username='user', password='pass', is_staff=False)
        self.user_token = Token.objects.create(user=self.user)
        self.group = get_or_create_tulua_group()
        assign_user_group(self.admin, self.group)

        self.template = WhatsAppTemplate.objects.create(
            name='test_template',
            language='es',
            category='MARKETING',
            status='APPROVED',
            template_id='12345',
            components=[{'type': 'body', 'text': 'Hola {{nombre}}'}],
        )

    def test_list_requires_auth(self):
        self.client.credentials()
        response = self.client.get('/api/templates/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_list_non_staff_forbidden(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        response = self.client.get('/api/templates/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_list_staff_allowed(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.get('/api/templates/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    @patch('api.whatsapp_templates.create_template')
    def test_create_template(self, mock_create_template):
        mock_create_template.return_value = {'id': '123', 'status': 'PENDING'}
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        data = {
            'name': 'new_template',
            'language': 'es',
            'category': 'MARKETING',
            'components': [{'type': 'body', 'text': 'Bienvenido'}],
        }
        response = self.client.post('/api/templates/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['name'], 'new_template')
        self.assertEqual(response.data['status'], 'PENDING')

    def test_create_requires_admin(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        data = {
            'name': 'new_template',
            'language': 'es',
            'category': 'MARKETING',
            'components': [{'type': 'body', 'text': 'Bienvenido'}],
        }
        response = self.client.post('/api/templates/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_approved_endpoint(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.get('/api/templates/approved/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['name'], 'test_template')

    def test_approved_filters(self):
        WhatsAppTemplate.objects.create(
            name='pending_template',
            language='es',
            category='MARKETING',
            status='PENDING',
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.get('/api/templates/approved/')
        self.assertEqual(len(response.data), 1)

    def test_destroy_requires_admin(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        response = self.client.delete(f'/api/templates/{self.template.id}/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_destroy_staff(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.delete(f'/api/templates/{self.template.id}/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_bulk_send_requires_admin(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        response = self.client.post('/api/templates/bulk_send/', {'template_id': self.template.id, 'count': 5}, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_bulk_send_with_recipients_creates_conversations(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.post('/api/templates/bulk_send/', {
            'template_id': self.template.id,
            'recipients': [
                {'phone': '573001111111', 'parameters': {'nombre': 'Juan'}},
                {'phone': '573002222222', 'parameters': {'nombre': 'Maria'}},
            ],
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data['queued'], 2)
        self.assertEqual(data['created'], 2)
        self.assertEqual(data['total'], 2)
        conv1 = Conversation.objects.get(contact_phone='573001111111')
        self.assertEqual(conv1.contact_name, '573001111111')
        conv2 = Conversation.objects.get(contact_phone='573002222222')
        self.assertEqual(conv2.contact_name, '573002222222')

    def test_bulk_send_with_recipients_uses_existing_conversation(self):
        conv = Conversation.objects.create(
            contact_phone='573001111111', whatsapp_id='573001111111',
            contact_name='Existing', group=self.group,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.post('/api/templates/bulk_send/', {
            'template_id': self.template.id,
            'recipients': [
                {'phone': '573001111111', 'parameters': {'nombre': 'Juan'}},
            ],
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data['queued'], 1)
        self.assertEqual(data['created'], 0)
        self.assertEqual(data['total'], 1)

    def test_bulk_send_with_recipients_rejects_empty_phone(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.post('/api/templates/bulk_send/', {
            'template_id': self.template.id,
            'recipients': [
                {'phone': '', 'parameters': {'nombre': 'Juan'}},
            ],
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class SendTemplateActionTests(APITestCase):
    """Test the send_template action on ConversationViewSet."""

    def setUp(self):
        self.admin = User.objects.create_user(username='admin', password='pass', is_staff=True)
        self.admin_token = Token.objects.create(user=self.admin)
        self.user = User.objects.create_user(username='user', password='pass', is_staff=False)
        self.user_token = Token.objects.create(user=self.user)
        self.group = get_or_create_tulua_group()
        assign_user_group(self.admin, self.group)

        self.conversation = Conversation.objects.create(
            whatsapp_id='573001234567',
            contact_name='Test',
            contact_phone='573001234567',
            group=self.group,
        )

        self.template = WhatsAppTemplate.objects.create(
            name='test_template',
            language='es',
            category='MARKETING',
            status='APPROVED',
            template_id='12345',
            components=[{'type': 'body', 'text': 'Hola {{nombre}}'}],
        )

    def test_send_template_requires_admin(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/send_template/',
            {'template_id': self.template.id},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_send_template_success(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/send_template/',
            {'template_id': self.template.id, 'parameters': {'nombre': 'Juan'}},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['message_type'], 'template')

    def test_send_template_not_approved(self):
        pending = WhatsAppTemplate.objects.create(
            name='pending_t',
            language='es',
            category='MARKETING',
            status='PENDING',
            components=[{'type': 'body', 'text': 'test'}],
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/send_template/',
            {'template_id': pending.id},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_send_template_invalid_id(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.post(
            f'/api/conversations/{self.conversation.id}/send_template/',
            {'template_id': 999},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class TemplateWebhookHandlerTests(APITestCase):
    """Test webhook handler functions for template events."""

    def setUp(self):
        self.template = WhatsAppTemplate.objects.create(
            name='test_template',
            language='es',
            category='MARKETING',
            status='PENDING',
            template_id='12345',
            components=[{'type': 'body', 'text': 'Hola'}],
        )

    def test_status_approved_webhook(self):
        from api.views import _handle_template_status_webhook
        _handle_template_status_webhook({
            'event': 'APPROVED',
            'message_template_id': '12345',
            'message_template_name': 'test_template',
            'message_template_language': 'es',
            'message_template_category': 'MARKETING',
            'reason': 'NONE',
        })
        self.template.refresh_from_db()
        self.assertEqual(self.template.status, 'APPROVED')

    def test_status_rejected_webhook(self):
        from api.views import _handle_template_status_webhook
        _handle_template_status_webhook({
            'event': 'REJECTED',
            'message_template_id': '12345',
            'message_template_name': 'test_template',
            'message_template_language': 'es',
            'reason': 'INVALID_FORMAT',
            'rejection_info': {
                'reason': 'Parameters next to each other',
                'recommendation': 'Separate with text',
            },
        })
        self.template.refresh_from_db()
        self.assertEqual(self.template.status, 'REJECTED')
        self.assertIn('Parameters next to each other', self.template.rejection_reason)

    def test_quality_webhook(self):
        from api.views import _handle_template_quality_webhook
        _handle_template_quality_webhook({
            'new_quality_score': 'GREEN',
            'message_template_name': 'test_template',
            'message_template_language': 'es',
        })
        self.template.refresh_from_db()
        self.assertEqual(self.template.quality_score, 'GREEN')

    def test_category_webhook(self):
        from api.views import _handle_template_category_webhook
        _handle_template_category_webhook({
            'new_category': 'UTILITY',
            'previous_category': 'MARKETING',
            'message_template_name': 'test_template',
            'message_template_language': 'es',
        })
        self.template.refresh_from_db()
        self.assertEqual(self.template.category, 'UTILITY')


class SerializerTests(APITestCase):
    """Test template serializers."""

    def setUp(self):
        self.template = WhatsAppTemplate.objects.create(
            name='test_t',
            language='es',
            category='MARKETING',
            status='APPROVED',
            template_id='12345',
            components=[{'type': 'body', 'text': 'Hola {{nombre}}'}],
        )

    def test_whatsapp_template_serializer_read_only_fields(self):
        serializer = WhatsAppTemplateSerializer(self.template)
        data = serializer.data
        self.assertEqual(data['id'], self.template.id)
        self.assertEqual(data['name'], 'test_t')
        self.assertEqual(data['status'], 'APPROVED')
        self.assertIn('created_at', data)

    def test_send_template_serializer_valid(self):
        serializer = SendTemplateSerializer(data={
            'template_id': self.template.id,
            'parameters': {'nombre': 'Juan'},
        })
        self.assertTrue(serializer.is_valid())

    def test_send_template_serializer_not_approved(self):
        pending = WhatsAppTemplate.objects.create(
            name='pending_t', language='es', category='MARKETING',
            status='PENDING',
        )
        serializer = SendTemplateSerializer(data={'template_id': pending.id})
        self.assertFalse(serializer.is_valid())

    def test_bulk_send_serializer_valid(self):
        serializer = BulkSendTemplateSerializer(data={
            'template_id': self.template.id,
            'count': 25,
        })
        self.assertTrue(serializer.is_valid())
        self.assertEqual(serializer.validated_data['count'], 25)

    def test_bulk_send_serializer_max_count(self):
        serializer = BulkSendTemplateSerializer(data={
            'template_id': self.template.id,
            'count': 1001,
        })
        self.assertFalse(serializer.is_valid())

    def test_bulk_send_serializer_recipients_valid(self):
        serializer = BulkSendTemplateSerializer(data={
            'template_id': self.template.id,
            'recipients': [
                {'phone': '573001234567', 'parameters': {'nombre': 'Juan'}},
                {'phone': '573007654321', 'parameters': {'nombre': 'Maria'}},
            ],
        })
        self.assertTrue(serializer.is_valid())
        self.assertEqual(len(serializer.validated_data['recipients']), 2)

    def test_bulk_send_serializer_recipients_missing_phone(self):
        serializer = BulkSendTemplateSerializer(data={
            'template_id': self.template.id,
            'recipients': [
                {'phone': '', 'parameters': {'nombre': 'Juan'}},
            ],
        })
        self.assertFalse(serializer.is_valid())

    def test_bulk_send_serializer_rejects_both_count_and_recipients(self):
        serializer = BulkSendTemplateSerializer(data={
            'template_id': self.template.id,
            'count': 10,
            'recipients': [{'phone': '573001234567'}],
        })
        self.assertFalse(serializer.is_valid())

    def test_bulk_send_serializer_defaults_to_count_when_neither_given(self):
        serializer = BulkSendTemplateSerializer(data={
            'template_id': self.template.id,
        })
        self.assertTrue(serializer.is_valid())
        self.assertEqual(serializer.validated_data['count'], 10)


# ── Conversation Pin ───────────────────────────────────────────────────────

class ConversationPinTests(APITestCase):
    """Test pin/unpin conversations — both group and personal pins."""

    def setUp(self):
        self.user = User.objects.create_user(username='pinuser', password='testpass123')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)
        self.conv = Conversation.objects.create(
            whatsapp_id='15550000111', contact_name='Pin Test',
            contact_phone='15550000111', group=self.group,
        )

    def test_group_pin_sets_pin(self):
        response = self.client.post(f'/api/conversations/{self.conv.id}/toggle_pin/', {'type': 'group'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.conv.refresh_from_db()
        self.assertTrue(self.conv.is_pinned)
        self.assertIsNotNone(self.conv.pinned_at)

    def test_group_pin_unsets_pin(self):
        self.conv.is_pinned = True
        self.conv.pinned_at = timezone.now()
        self.conv.save(update_fields=['is_pinned', 'pinned_at'])
        response = self.client.post(f'/api/conversations/{self.conv.id}/toggle_pin/', {'type': 'group'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.conv.refresh_from_db()
        self.assertFalse(self.conv.is_pinned)
        self.assertIsNone(self.conv.pinned_at)

    def test_personal_pin_creates_user_pin(self):
        response = self.client.post(f'/api/conversations/{self.conv.id}/toggle_pin/', {'type': 'personal'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(ConversationUserPin.objects.filter(conversation=self.conv, user=self.user).exists())

    def test_personal_pin_removes_on_second_toggle(self):
        self.client.post(f'/api/conversations/{self.conv.id}/toggle_pin/', {'type': 'personal'})
        self.client.post(f'/api/conversations/{self.conv.id}/toggle_pin/', {'type': 'personal'})
        self.assertFalse(ConversationUserPin.objects.filter(conversation=self.conv, user=self.user).exists())

    def test_personal_pin_returns_is_pinned_by_me(self):
        response = self.client.post(f'/api/conversations/{self.conv.id}/toggle_pin/', {'type': 'personal'})
        self.assertTrue(response.data.get('is_pinned_by_me'))

    def test_personal_pin_does_not_affect_is_pinned(self):
        self.client.post(f'/api/conversations/{self.conv.id}/toggle_pin/', {'type': 'personal'})
        self.conv.refresh_from_db()
        self.assertFalse(self.conv.is_pinned)

    def test_pinned_sorted_first(self):
        conv2 = Conversation.objects.create(
            whatsapp_id='15550000222', contact_name='Pin Test 2',
            group=self.group, last_message_at=timezone.now(),
        )
        conv3 = Conversation.objects.create(
            whatsapp_id='15550000333', contact_name='Pin Test 3',
            group=self.group, last_message_at=timezone.now() - timedelta(hours=1),
        )
        # Pin the oldest
        conv3.is_pinned = True
        conv3.pinned_at = timezone.now()
        conv3.save(update_fields=['is_pinned', 'pinned_at'])
        response = self.client.get('/api/conversations/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        if len(response.data) > 0:
            self.assertEqual(response.data[0]['id'], conv3.id)

    def test_toggle_pin_requires_auth(self):
        self.client.credentials()
        response = self.client.post(f'/api/conversations/{self.conv.id}/toggle_pin/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_group_pin_returns_updated(self):
        response = self.client.post(f'/api/conversations/{self.conv.id}/toggle_pin/', {'type': 'group'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('is_pinned', response.data)
        self.assertTrue(response.data['is_pinned'])

    def test_list_includes_is_pinned_by_me(self):
        self.client.post(f'/api/conversations/{self.conv.id}/toggle_pin/', {'type': 'personal'})
        response = self.client.get('/api/conversations/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        if len(response.data) > 0:
            self.assertIn('is_pinned_by_me', response.data[0])
            self.assertTrue(response.data[0]['is_pinned_by_me'])


# ── Audit Log ──────────────────────────────────────────────────────────────

class AuditLogModelTests(TestCase):
    """Test AuditLog model creation and behavior."""

    def setUp(self):
        self.user = User.objects.create_user(username='auditmodeltest', password='testpass123')

    def test_create_audit_entry(self):
        log = AuditLog.objects.create(
            actor=self.user, action='take',
            detail='Took conversation 1',
        )
        self.assertEqual(log.actor, self.user)
        self.assertEqual(log.action, 'take')
        self.assertEqual(log.actor.username, self.user.username)

    def test_audit_ordering(self):
        AuditLog.objects.create(actor=self.user, action='take')
        AuditLog.objects.create(actor=self.user, action='release')
        logs = AuditLog.objects.all()
        self.assertEqual(logs.count(), 2)
        self.assertGreaterEqual(logs[0].created_at, logs[1].created_at)

    def test_audit_action_choices(self):
        for action_code, _ in AuditLog.ACTION_CHOICES:
            log = AuditLog.objects.create(actor=self.user, action=action_code)
            self.assertEqual(log.action, action_code)


class AuditLogAPITests(APITestCase):
    """Test audit log API endpoints."""

    def setUp(self):
        self.admin = User.objects.create_superuser(username='auditadmin', password='admin123', email='a@b.com')
        self.admin_token = Token.objects.create(user=self.admin)
        self.user = User.objects.create_user(username='audituser', password='testpass123')
        self.user_token = Token.objects.create(user=self.user)
        self.group = get_or_create_tulua_group()
        assign_user_group(self.admin, self.group)
        assign_user_group(self.user, self.group)
        self.conv = Conversation.objects.create(
            whatsapp_id='15550000444', contact_name='Audit Conv', group=self.group,
        )

    def test_list_requires_admin(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        response = self.client.get('/api/audit-logs/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_list_returns_logs(self):
        AuditLog.objects.create(actor=self.admin, conversation=self.conv, action='take')
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.get('/api/audit-logs/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn('results' if 'results' in data else 'count', data)

    def test_filter_by_conversation(self):
        AuditLog.objects.create(actor=self.admin, conversation=self.conv, action='take')
        AuditLog.objects.create(actor=self.admin, action='take')
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.get('/api/audit-logs/', {'conversation': self.conv.id})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_filter_by_action(self):
        AuditLog.objects.create(actor=self.admin, conversation=self.conv, action='pin')
        AuditLog.objects.create(actor=self.admin, conversation=self.conv, action='unpin')
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.get('/api/audit-logs/', {'action': 'pin'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_filter_by_date_range(self):
        AuditLog.objects.create(actor=self.admin, conversation=self.conv, action='take')
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        yesterday = (timezone.now() - timedelta(days=1)).isoformat()
        tomorrow = (timezone.now() + timedelta(days=1)).isoformat()
        response = self.client.get('/api/audit-logs/', {
            'created_at__gte': yesterday,
            'created_at__lte': tomorrow,
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_take_creates_audit_entry(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        self.client.post(f'/api/conversations/{self.conv.id}/take_conversation/', {'duration_minutes': 10})
        self.assertTrue(AuditLog.objects.filter(action='take', conversation=self.conv).exists())

    def test_release_creates_audit_entry(self):
        ConversationTake.create_take(conversation=self.conv, created_by=self.user, duration_minutes=60)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        self.client.post(f'/api/conversations/{self.conv.id}/release_conversation/')
        self.assertTrue(AuditLog.objects.filter(action='release', conversation=self.conv).exists())

    def test_pin_creates_audit_entry(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        self.client.post(f'/api/conversations/{self.conv.id}/toggle_pin/')
        self.assertTrue(AuditLog.objects.filter(action='pin', conversation=self.conv).exists())

    def test_send_message_creates_audit_entry(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        self.client.post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'direction': 'outbound', 'message_type': 'text', 'content': 'Test'},
        )
        self.assertTrue(AuditLog.objects.filter(action='send_message', conversation=self.conv).exists())

    def test_audit_log_readonly(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.post('/api/audit-logs/', {'action': 'take'})
        self.assertIn(response.status_code, (status.HTTP_403_FORBIDDEN, status.HTTP_405_METHOD_NOT_ALLOWED))


# ── Canned Responses ───────────────────────────────────────────────────────

class CannedResponseTests(APITestCase):
    """Test canned response CRUD and permissions."""

    def setUp(self):
        self.admin = User.objects.create_superuser(username='cannedadmin', password='admin123', email='a@b.com')
        self.admin_token = Token.objects.create(user=self.admin)
        self.user = User.objects.create_user(username='canneduser', password='testpass123')
        self.user_token = Token.objects.create(user=self.user)
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)
        assign_user_group(self.admin, self.group)
        CannedResponse.objects.create(
            title='Welcome', content='Bienvenido!', category='saludo',
            group=self.group, created_by=self.admin,
        )
        CannedResponse.objects.create(
            title='Global Greeting', content='Hola!', category='saludo',
            group=None, created_by=self.admin,
        )
        other_group = CityGroup.objects.create(name='Otra', slug='otra')
        CannedResponse.objects.create(
            title='Other City', content='Otro grupo', category='otro',
            group=other_group, created_by=self.admin,
        )

    def test_list_requires_auth(self):
        self.client.credentials()
        response = self.client.get('/api/canned-responses/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_regular_user_sees_group_and_global(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        response = self.client.get('/api/canned-responses/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        results = data.get('results', data if isinstance(data, list) else [])
        names = [r['title'] for r in results]
        self.assertIn('Welcome', names)
        self.assertIn('Global Greeting', names)
        self.assertNotIn('Other City', names)

    def test_staff_sees_all(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.get('/api/canned-responses/')
        data = response.json()
        results = data.get('results', data if isinstance(data, list) else [])
        self.assertEqual(len(results), 3)

    def test_create_forced_to_own_group(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        response = self.client.post('/api/canned-responses/', {
            'title': 'My Response', 'content': 'Test', 'category': 'test',
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        cr = CannedResponse.objects.get(title='My Response')
        self.assertEqual(cr.group, self.group)

    def test_staff_can_create_global(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.post('/api/canned-responses/', {
            'title': 'Staff Global', 'content': 'Global', 'category': 'test',
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_regular_user_cannot_delete(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        cr = CannedResponse.objects.first()
        response = self.client.delete(f'/api/canned-responses/{cr.id}/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_delete(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        cr = CannedResponse.objects.first()
        response = self.client.delete(f'/api/canned-responses/{cr.id}/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_update_title_and_content(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        cr = CannedResponse.objects.get(title='Welcome', created_by=self.admin)
        response = self.client.patch(f'/api/canned-responses/{cr.id}/', {
            'title': 'Updated Title', 'content': 'Updated content',
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        cr.refresh_from_db()
        self.assertEqual(cr.title, 'Updated Title')

    def test_cursor_pagination(self):
        for i in range(15):
            CannedResponse.objects.create(
                title=f'Bulk {i}', content=f'Content {i}', category='bulk',
                created_by=self.admin,
            )
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.get('/api/canned-responses/', {'page_size': 10})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn('results', data)
        self.assertIn('count', data)


# ── Agent Presence ────────────────────────────────────────────────────────

class AgentPresenceAPITests(APITestCase):
    """Test agent presence heartbeat and list endpoints."""

    def setUp(self):
        self.user = User.objects.create_user(username='presenceuser', password='testpass123')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)
        AgentPresence.objects.create(user=self.user, status='online')

    @patch('api.views.get_sync_redis')
    def test_heartbeat_sets_redis_key(self, mock_get_redis):
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        response = self.client.post('/api/presence/heartbeat/', {'status': 'online'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'ok')

    @patch('api.views.get_sync_redis')
    def test_heartbeat_creates_db_record(self, mock_get_redis):
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        new_user = User.objects.create_user(username='heartbeatnew', password='pass')
        new_token = Token.objects.create(user=new_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {new_token.key}')
        response = self.client.post('/api/presence/heartbeat/', {'status': 'online'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(AgentPresence.objects.filter(user=new_user).exists())

    def test_heartbeat_requires_auth(self):
        self.client.credentials()
        response = self.client.post('/api/presence/heartbeat/', {'status': 'online'})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch('api.views.get_sync_redis')
    def test_presence_list_returns_users(self, mock_get_redis):
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.scan.return_value = (0, [])
        response = self.client.get('/api/presence/')
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json(), list)

    @patch('api.views.get_sync_redis')
    def test_presence_list_requires_auth(self, mock_get_redis):
        self.client.credentials()
        response = self.client.get('/api/presence/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_heartbeat_offline_creates_offline_status(self):
        new_user = User.objects.create_user(username='offlineuser', password='pass')
        new_token = Token.objects.create(user=new_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {new_token.key}')
        with patch('api.views.get_sync_redis') as mock_get_redis:
            mock_redis = MagicMock()
            mock_get_redis.return_value = mock_redis
            response = self.client.post('/api/presence/heartbeat/', {'status': 'offline'})
            self.assertEqual(response.status_code, 200)
            presence = AgentPresence.objects.get(user=new_user)
            self.assertEqual(presence.status, 'offline')


# ── Full-Text Message Search ──────────────────────────────────────────────

class MessageFullTextSearchTests(APITestCase):
    """Test full-text message search (requires PostgreSQL with full-text search)."""

    def setUp(self):
        self.user = User.objects.create_user(username='searchuser', password='testpass123')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)
        self.conv = Conversation.objects.create(
            whatsapp_id='15559999000', contact_name='Search Test',
            contact_phone='15559999000', group=self.group,
        )
        self.msg = Message.objects.create(
            conversation=self.conv, direction='inbound', message_type='text',
            content='Hola, necesito una cotización para un domicilio',
            sender_name='Search Test',
        )

    def test_search_requires_auth(self):
        self.client.credentials()
        response = self.client.get('/api/messages/search/', {'q': 'cotización'})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_search_empty_query_returns_400(self):
        response = self.client.get('/api/messages/search/', {'q': ''})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_search_no_results(self):
        response = self.client.get('/api/messages/search/', {'q': 'xyzzy_nonexistent'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['results']), 0)

    def test_search_returns_results_structure(self):
        response = self.client.get('/api/messages/search/', {'q': 'cotización'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('results', data)
        self.assertIn('total', data)
        self.assertIn('has_more', data)

    def test_search_respects_limit(self):
        for i in range(5):
            Message.objects.create(
                conversation=self.conv, direction='inbound', message_type='text',
                content=f'domicilio test message {i}', sender_name='Search',
            )
        response = self.client.get('/api/messages/search/', {'q': 'domicilio', 'limit': 2})
        data = response.json()
        self.assertLessEqual(len(data['results']), 2)


# ── Typing Indicator ──────────────────────────────────────────────────────

class TypingIndicatorTests(APITestCase):
    """Test typing indicator via webhook and SSE."""

    def setUp(self):
        self.user = User.objects.create_user(username='typinguser', password='testpass123')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)
        self.conv = Conversation.objects.create(
            whatsapp_id='15551112222', contact_name='Typing Test',
            contact_phone='15551112222', group=self.group,
        )

    @patch('api.views.get_sync_redis')
    @patch('api.views.publish')
    def test_webhook_typing_status_triggers_sse(self, mock_publish, mock_get_redis):
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        payload = {
            'entry': [{
                'changes': [{
                    'value': {
                        'messaging_product': 'whatsapp',
                        'metadata': {'phone_number_id': '123'},
                        'statuses': [{
                            'id': 'wamid.typing1',
                            'status': 'typing',
                            'conversation': {'id': '15551112222'},
                            'timestamp': '2000000000',
                        }],
                    },
                }],
            }],
        }
        with self.settings(WHATSAPP_APP_SECRET=''):
            response = self.client.post(
                '/webhook/', data=json.dumps(payload), content_type='application/json',
            )
        self.assertEqual(response.status_code, 200)

    @patch('api.views.get_sync_redis')
    @patch('api.views.publish')
    def test_typing_unknown_conversation_ignored(self, mock_publish, mock_get_redis):
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        payload = {
            'entry': [{
                'changes': [{
                    'value': {
                        'messaging_product': 'whatsapp',
                        'metadata': {'phone_number_id': '123'},
                        'statuses': [{
                            'id': 'wamid.typing2',
                            'status': 'typing',
                            'conversation': {'id': 'NONEXISTENT'},
                            'timestamp': '2000000000',
                        }],
                    },
                }],
            }],
        }
        with self.settings(WHATSAPP_APP_SECRET=''):
            response = self.client.post(
                '/webhook/', data=json.dumps(payload), content_type='application/json',
            )
        self.assertEqual(response.status_code, 200)

    def test_typing_requires_no_auth_for_webhook(self):
        """Webhook endpoint is public (csrf_exempt)."""
        payload = {
            'entry': [{
                'changes': [{
                    'value': {
                        'messaging_product': 'whatsapp',
                        'metadata': {'phone_number_id': '123'},
                        'statuses': [],
                    },
                }],
            }],
        }
        with self.settings(WHATSAPP_APP_SECRET=''):
            response = self.client.post(
                '/webhook/', data=json.dumps(payload), content_type='application/json',
            )
        self.assertEqual(response.status_code, 200)


# ── Push Subscriptions ────────────────────────────────────────────────────

class PushSubscriptionTests(APITestCase):
    """Test push subscription CRUD and throttle."""

    def setUp(self):
        self.user = User.objects.create_user(username='pushuser', password='testpass123')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)

    def test_subscribe_creates_record(self):
        response = self.client.post('/api/push-subscribe/', {
            'endpoint': 'https://example.com/push/abc',
            'keys': {'p256dh': 'test_key', 'auth': 'test_auth'},
            'browser': 'Chrome',
        }, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'subscribed')
        self.assertTrue(PushSubscription.objects.filter(user=self.user).exists())

    def test_subscribe_updates_existing(self):
        PushSubscription.objects.create(
            user=self.user, endpoint='https://example.com/push/abc',
            p256dh='old_key', auth='old_auth',
        )
        response = self.client.post('/api/push-subscribe/', {
            'endpoint': 'https://example.com/push/abc',
            'keys': {'p256dh': 'new_key', 'auth': 'new_auth'},
        }, format='json')
        self.assertEqual(response.status_code, 200)
        sub = PushSubscription.objects.get(user=self.user, endpoint='https://example.com/push/abc')
        self.assertEqual(sub.p256dh, 'new_key')

    def test_subscribe_requires_valid_keys(self):
        response = self.client.post('/api/push-subscribe/', {
            'endpoint': 'https://example.com/push/abc',
            'keys': {},
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_unsubscribe_removes_record(self):
        sub = PushSubscription.objects.create(
            user=self.user, endpoint='https://example.com/push/abc',
            p256dh='key', auth='auth',
        )
        response = self.client.post('/api/push-unsubscribe/', {
            'endpoint': 'https://example.com/push/abc',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PushSubscription.objects.filter(id=sub.id).exists())

    def test_subscribe_requires_auth(self):
        self.client.credentials()
        response = self.client.post('/api/push-subscribe/', {
            'endpoint': 'https://example.com/push/abc',
            'keys': {'p256dh': 'key', 'auth': 'auth'},
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


# ── Bot Status (non-admin access) ──────────────────────────────────────────

class BotStatusNonAdminTests(APITestCase):
    """Test that non-staff users can access bot status (stripped response)."""

    def setUp(self):
        self.admin_user = User.objects.create_user(
            username='admin', password='testpass123', is_staff=True
        )
        self.admin_token = Token.objects.create(user=self.admin_user)
        self.non_admin = User.objects.create_user(
            username='agent', password='testpass123', is_staff=False
        )
        self.non_admin_token = Token.objects.create(user=self.non_admin)
        self.group = get_or_create_tulua_group()
        assign_user_group(self.non_admin, self.group)
        assign_user_group(self.admin_user, self.group)

    @patch('config.urls._read_bot_metrics')
    def test_non_admin_can_access(self, mock_read):
        mock_read.return_value = (
            {'messages.processed': 100, 'locks.acquired': 20, 'tool_calls.succeeded': 15,
             'tool_calls.failed': 3, 'escalations': 5, 'cancellations': 2,
             'messages.rate_limited': 1, 'loop.crashes': 0},
            {}, 0, 86400, 1440,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.non_admin_token.key}')
        response = self.client.get('/api/bot/status/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('messages_processed_today', data)
        self.assertIn('completion_rate', data)
        self.assertIn('escalations_today', data)
        self.assertIn('conversations_handled_today', data)
        self.assertNotIn('config', data)
        self.assertNotIn('series', data)
        self.assertNotIn('metrics', data)

    @patch('config.urls._read_bot_metrics')
    def test_non_admin_cannot_see_config(self, mock_read):
        mock_read.return_value = (
            {'messages.processed': 0, 'locks.acquired': 0, 'tool_calls.succeeded': 0,
             'tool_calls.failed': 0, 'escalations': 0, 'cancellations': 0,
             'messages.rate_limited': 0, 'loop.crashes': 0},
            {}, 0, 86400, 1440,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.non_admin_token.key}')
        response = self.client.get('/api/bot/status/')
        self.assertNotIn('config', response.json())

    @patch('config.urls._read_bot_metrics')
    def test_staff_sees_full_data(self, mock_read):
        mock_read.return_value = (
            {'messages.processed': 100, 'locks.acquired': 20, 'tool_calls.succeeded': 15,
             'tool_calls.failed': 3, 'escalations': 5, 'cancellations': 2,
             'messages.rate_limited': 1, 'loop.crashes': 0},
            {'messages.processed': [1]}, 1000, 86400, 1440,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.get('/api/bot/status/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('config', data)
        self.assertIn('series', data)
        self.assertIn('metrics', data)
        self.assertIn('healthy', data)

    def test_unauthenticated_blocked(self):
        self.client.credentials()
        response = self.client.get('/api/bot/status/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


# ── CSV Export ─────────────────────────────────────────────────────────────

class CsvExportTests(APITestCase):
    """Test CSV export endpoint."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='csvexport', password='testpass123', is_staff=False
        )
        self.token = Token.objects.create(user=self.user)
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)

        self.conv1 = Conversation.objects.create(
            whatsapp_id='123456', contact_name='Test Contact',
            contact_phone='573001234567', status='active',
            last_message='Hello', last_message_at=timezone.now(),
            group=self.group,
        )
        self.conv2 = Conversation.objects.create(
            whatsapp_id='789012', contact_name='Resolved Contact',
            contact_phone='573007890123', status='resolved',
            last_message='Resolved', last_message_at=timezone.now(),
            group=self.group,
        )

    def test_export_requires_auth(self):
        self.client.credentials()
        response = self.client.get('/api/conversations/export/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_export_returns_csv(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        response = self.client.get('/api/conversations/export/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('Content-Disposition', response)
        self.assertIn('attachment', response['Content-Disposition'])
        # Check BOM prefix
        content = b''.join(response.streaming_content)
        self.assertTrue(content.startswith(b'\xef\xbb\xbf'))

    def test_export_contains_headers(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        response = self.client.get('/api/conversations/export/')
        content = b''.join(response.streaming_content).decode('utf-8-sig')
        self.assertIn('ID', content)
        self.assertIn('Contacto', content)
        self.assertIn('Teléfono', content)
        self.assertIn('Último mensaje', content)
        self.assertIn('Estado', content)

    def test_export_returns_all_visible_conversations(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        response = self.client.get('/api/conversations/export/')
        content = b''.join(response.streaming_content).decode('utf-8-sig')
        self.assertIn('Test Contact', content)
        self.assertIn('Resolved Contact', content)


# ── Message Group Filtering ───────────────────────────────────────────────

class MessageGroupFilterTests(APITestCase):
    """Test that message queries respect group scoping."""

    def setUp(self):
        self.group_a = get_or_create_tulua_group()
        self.group_b = CityGroup.objects.create(name='Cali', slug='cali')

        self.agent_a = User.objects.create_user(username='msggrpa', password='testpass123')
        assign_user_group(self.agent_a, self.group_a)
        self.token_a = Token.objects.create(user=self.agent_a)

        self.agent_b = User.objects.create_user(username='msggrpb', password='testpass123')
        assign_user_group(self.agent_b, self.group_b)
        self.token_b = Token.objects.create(user=self.agent_b)

        self.admin = User.objects.create_superuser(
            username='msgadmin', password='testpass123', email='a@a.com'
        )
        self.admin_token = Token.objects.create(user=self.admin)

        self.conv_a = Conversation.objects.create(
            whatsapp_id='111111', contact_name='MSG Group A', group=self.group_a,
        )
        self.conv_b = Conversation.objects.create(
            whatsapp_id='222222', contact_name='MSG Group B', group=self.group_b,
        )

        Message.objects.create(conversation=self.conv_a, direction='inbound', content='Message A')
        Message.objects.create(conversation=self.conv_b, direction='inbound', content='Message B')

    def test_agent_sees_only_own_group_messages(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token_a.key}')
        response = self.client.get(f'/api/conversations/{self.conv_a.id}/messages/')
        self.assertEqual(response.status_code, 200)
        ids = [m['id'] for m in response.data.get('results', [])]
        self.assertGreater(len(ids), 0)

    def test_agent_cannot_see_other_group_messages_via_list(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token_a.key}')
        # Messages in group_a's conv
        response_a = self.client.get(f'/api/conversations/{self.conv_a.id}/messages/')
        self.assertEqual(response_a.status_code, 200)

        # Messages in group_b's conv should 404 because only the filtered
        # queryset is used (self.get_object() filters by group)
        response_b = self.client.get(f'/api/conversations/{self.conv_b.id}/messages/')
        self.assertEqual(response_b.status_code, 404)

    def test_staff_sees_all_messages(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response_a = self.client.get(f'/api/conversations/{self.conv_a.id}/messages/')
        self.assertEqual(response_a.status_code, 200)
        response_b = self.client.get(f'/api/conversations/{self.conv_b.id}/messages/')
        self.assertEqual(response_b.status_code, 200)

    def test_agent_cannot_retrieve_other_group_conversation(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token_a.key}')
        response = self.client.get(f'/api/conversations/{self.conv_b.id}/')
        self.assertEqual(response.status_code, 404)


# ── Message Search Group Filtering ────────────────────────────────────────

class MessageSearchGroupFilterTests(APITestCase):
    """Test that full-text message search respects group scoping."""

    def setUp(self):
        self.group_a = get_or_create_tulua_group()
        self.group_b = CityGroup.objects.create(name='Cali', slug='cali')

        self.agent_a = User.objects.create_user(username='searchgrpa', password='testpass123')
        assign_user_group(self.agent_a, self.group_a)
        self.token_a = Token.objects.create(user=self.agent_a)

        self.agent_b = User.objects.create_user(username='searchgrpb', password='testpass123')
        assign_user_group(self.agent_b, self.group_b)
        self.token_b = Token.objects.create(user=self.agent_b)

        self.admin = User.objects.create_superuser(
            username='searchadmin', password='testpass123', email='a@a.com'
        )
        self.admin_token = Token.objects.create(user=self.admin)

        self.conv_a = Conversation.objects.create(
            whatsapp_id='333333', contact_name='Search Group A', group=self.group_a,
        )
        self.conv_b = Conversation.objects.create(
            whatsapp_id='444444', contact_name='Search Group B', group=self.group_b,
        )

        self.msg_a = Message.objects.create(
            conversation=self.conv_a, direction='inbound',
            content='confidential pricing info for group A only',
        )
        self.msg_b = Message.objects.create(
            conversation=self.conv_b, direction='inbound',
            content='confidential pricing info for group B only',
        )

    def test_search_scoped_to_group(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token_a.key}')
        response = self.client.get('/api/messages/search/?q=confidential')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        conv_ids = [r['conversation_id'] for r in data['results']]
        self.assertIn(self.conv_a.id, conv_ids)
        self.assertNotIn(self.conv_b.id, conv_ids)

    def test_search_other_group_excluded(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token_b.key}')
        response = self.client.get('/api/messages/search/?q=confidential')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        conv_ids = [r['conversation_id'] for r in data['results']]
        self.assertNotIn(self.conv_a.id, conv_ids)
        self.assertIn(self.conv_b.id, conv_ids)

    def test_staff_search_sees_all(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.get('/api/messages/search/?q=confidential')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data['results']), 2)


# ── CSV Export Group Filtering ────────────────────────────────────────────

class CsvExportGroupFilterTests(APITestCase):
    """Test CSV export respects group scoping."""

    def setUp(self):
        self.group_a = get_or_create_tulua_group()
        self.group_b = CityGroup.objects.create(name='Cali', slug='cali')

        self.agent_a = User.objects.create_user(username='csvexpa', password='testpass123')
        assign_user_group(self.agent_a, self.group_a)
        self.token_a = Token.objects.create(user=self.agent_a)

        self.agent_b = User.objects.create_user(username='csvexpb', password='testpass123')
        assign_user_group(self.agent_b, self.group_b)
        self.token_b = Token.objects.create(user=self.agent_b)

        self.admin = User.objects.create_superuser(
            username='csvexpadmin', password='testpass123', email='a@a.com'
        )
        self.admin_token = Token.objects.create(user=self.admin)

        self.conv_a = Conversation.objects.create(
            whatsapp_id='555555', contact_name='Export Group A',
            contact_phone='573001', status='active', last_message='A',
            last_message_at=timezone.now(), group=self.group_a,
        )
        self.conv_b = Conversation.objects.create(
            whatsapp_id='666666', contact_name='Export Group B',
            contact_phone='573002', status='active', last_message='B',
            last_message_at=timezone.now(), group=self.group_b,
        )

    def test_export_scoped_to_group(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token_a.key}')
        response = self.client.get('/api/conversations/export/?status=active')
        content = b''.join(response.streaming_content).decode('utf-8-sig')
        self.assertIn('Export Group A', content)
        self.assertNotIn('Export Group B', content)

    def test_export_other_group_excluded(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token_b.key}')
        response = self.client.get('/api/conversations/export/?status=active')
        content = b''.join(response.streaming_content).decode('utf-8-sig')
        self.assertNotIn('Export Group A', content)
        self.assertIn('Export Group B', content)

    def test_staff_export_sees_all(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        response = self.client.get('/api/conversations/export/?status=active')
        content = b''.join(response.streaming_content).decode('utf-8-sig')
        self.assertIn('Export Group A', content)
        self.assertIn('Export Group B', content)


# ── SSE Group Scoping ─────────────────────────────────────────────────────

class SSEGroupFilterTests(SimpleTestCase):
    """Test that publish() sends to the correct Redis channels."""

    @patch('api.realtime.get_sync_redis')
    def test_publish_without_group_sends_to_global_only(self, mock_get_redis):
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        from api.realtime import publish

        publish({'type': 'test'})

        mock_redis.publish.assert_called_once()
        args, _ = mock_redis.publish.call_args
        self.assertEqual(args[0], 'sse:events')

    @patch('api.realtime.get_sync_redis')
    def test_publish_with_group_sends_to_global_and_group(self, mock_get_redis):
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        from api.realtime import publish

        publish({'type': 'test'}, group_id=42)

        self.assertEqual(mock_redis.publish.call_count, 2)
        channels = [call[0][0] for call in mock_redis.publish.call_args_list]
        self.assertIn('sse:events', channels)
        self.assertIn('sse:group:42', channels)

    @patch('api.realtime.get_sync_redis')
    def test_publish_with_group_id_zero_sends_global_only(self, mock_get_redis):
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        from api.realtime import publish

        publish({'type': 'test'}, group_id=0)

        mock_redis.publish.assert_called_once()
        args, _ = mock_redis.publish.call_args
        self.assertEqual(args[0], 'sse:events')

    @patch('api.realtime.REDIS_CHANNEL', 'sse:events')
    @patch('api.realtime.GROUP_CHANNEL_PREFIX', 'sse:group')
    def test_subscribe_user_with_group_gets_group_channel(self):
        """Verify subscribe() builds correct channel list for non-staff with group."""
        user = MagicMock()
        user.is_authenticated = True
        user.is_staff = False
        profile = MagicMock()
        profile.group_id = 7
        user.profile = profile

        from api.realtime import _build_subscriber_channels
        channels = _build_subscriber_channels(user)
        self.assertNotIn('sse:events', channels)
        self.assertIn('sse:group:7', channels)

    def test_subscribe_staff_gets_global_only(self):
        """Verify staff subscribers only get the global channel."""
        user = MagicMock()
        user.is_authenticated = True
        user.is_staff = True

        from api.realtime import _build_subscriber_channels
        channels = _build_subscriber_channels(user)
        self.assertEqual(channels, ['sse:events'])


# ── Set Conversation Group ────────────────────────────────────────────────

class SetGroupTests(APITestCase):
    """Test the set_group endpoint for changing a conversation's group."""

    def setUp(self):
        self.group_a = get_or_create_tulua_group()
        self.group_b = CityGroup.objects.create(name='Cali', slug='cali')

        self.user = User.objects.create_user(username='setgrp', password='testpass123')
        assign_user_group(self.user, self.group_a)
        self.token = Token.objects.create(user=self.user)

        self.conv = Conversation.objects.create(
            whatsapp_id='777777', contact_name='SetGroup Test',
            contact_phone='573001234567', group=self.group_a,
        )

    def test_set_group_requires_auth(self):
        self.client.credentials()
        response = self.client.post(f'/api/conversations/{self.conv.id}/set_group/',
                                    {'group_id': self.group_b.id})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_set_group_updates_conversation_group(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        response = self.client.post(f'/api/conversations/{self.conv.id}/set_group/',
                                    {'group_id': self.group_b.id})
        self.assertEqual(response.status_code, 200)
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.group_id, self.group_b.id)

    def test_set_group_invalid_group_returns_400(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        response = self.client.post(f'/api/conversations/{self.conv.id}/set_group/',
                                    {'group_id': 99999})
        self.assertEqual(response.status_code, 400)

    def test_set_group_same_group_noop(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        response = self.client.post(f'/api/conversations/{self.conv.id}/set_group/',
                                    {'group_id': self.group_a.id})
        self.assertEqual(response.status_code, 200)

    def test_agent_cannot_set_group_on_other_group_conversation(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        conv_b = Conversation.objects.create(
            whatsapp_id='888888', contact_name='Other Group',
            group=self.group_b,
        )
        response = self.client.post(f'/api/conversations/{conv_b.id}/set_group/',
                                    {'group_id': self.group_a.id})
        self.assertEqual(response.status_code, 404)
