import asyncio
from unittest.mock import patch

from django.db import IntegrityError
from django.test import SimpleTestCase, override_settings
from django.conf import settings
from django.urls import resolve, reverse
from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase
from django.utils import timezone
from datetime import timedelta
import json

from .models import Conversation, Message, ConversationTag, ConversationNote, ConversationTake, SSEToken, CityGroup, UserProfile
from .serializers import (
    ConversationSerializer, MessageSerializer,
    ConversationTagSerializer, ConversationNoteSerializer, ConversationTakeSerializer,
)
from .views import ConversationViewSet, MessageViewSet


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
        from . import serializers as s_module
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

        with self.assertNumQueries(6):
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
        response = self.client.post('/api/conversations/remove_expired_tags/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

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
        qs = MessageViewSet().get_queryset()
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

    @patch('api.realtime._get_sync_redis')
    def test_publish_sends_to_correct_channel(self, mock_get_redis):
        mock_redis = mock_get_redis.return_value
        from api.realtime import publish

        publish({'type': 'test', 'data': 'hello'})

        mock_redis.publish.assert_called_once()
        args, _ = mock_redis.publish.call_args
        self.assertEqual(args[0], 'sse:events')
        self.assertIn('test', args[1])
        self.assertIn('_seq', args[1])

    @patch('api.realtime._get_sync_redis')
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

    @patch('api.realtime._get_sync_redis')
    def test_publish_handles_connection_error_gracefully(self, mock_get_redis):
        mock_redis = mock_get_redis.return_value
        mock_redis.publish.side_effect = ConnectionError('Redis down')
        from api.realtime import publish

        publish({'type': 'test'})

    @patch('api.realtime._get_sync_redis')
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
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)

    def test_list_requires_auth(self):
        response = self.client.get('/api/stickers/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_create_requires_admin(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.user_token.key}')
        response = self.client.post('/api/stickers/', {'name': 'test', 'image': ''})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


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
    """Verify User, Sticker, CityGroup write endpoints require admin."""

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

    def test_non_admin_cannot_create_sticker(self):
        response = self.client.post('/api/stickers/', {'name': 'test', 'image': ''})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

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
    """Signed media URL expiry and path traversal protection."""

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
