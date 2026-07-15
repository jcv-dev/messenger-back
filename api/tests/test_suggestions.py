"""Tests for message autocomplete suggestions (api/suggestions.py + endpoint)."""

from django.test import TestCase
from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from api.models import (
    MessageSuggestion, Message, Conversation, ConversationTag,
    CityGroup, UserProfile,
)


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


# ── Unit tests for api/suggestions.py ─────────────────────────────────────

class SearchTests(TestCase):
    """Test the search() function directly."""

    def setUp(self):
        MessageSuggestion.objects.create(
            text='Hola, gracias por comunicarte con Domii. ¿En qué puedo ayudarte?',
            tags=['saludo', 'general'],
            usage_count=10,
        )
        MessageSuggestion.objects.create(
            text='Hola, gracias por comunicarte. Estamos para servirte.',
            tags=['saludo'],
            usage_count=5,
        )
        MessageSuggestion.objects.create(
            text='El costo del domicilio es de $5,000 COP.',
            tags=['pricing', 'domicilio'],
            usage_count=20,
        )
        MessageSuggestion.objects.create(
            text='El costo total es de $8,000 COP.',
            tags=['pricing'],
            usage_count=15,
        )
        MessageSuggestion.objects.create(
            text='Totalmente diferente sin relación.',
            tags=[],
            usage_count=1,
        )

    def test_returns_top_completion(self):
        from api.suggestions import search
        result = search('Hola, gracias por')
        self.assertIsNotNone(result)
        self.assertIn('suggestion', result)
        self.assertIn('Hola', result['suggestion'])

    def test_returns_alternatives(self):
        from api.suggestions import search
        result = search('Hola, gracias por')
        self.assertIsNotNone(result)
        self.assertIn('alternatives', result)
        self.assertGreaterEqual(len(result['alternatives']), 1)

    def test_returns_none_for_short_text(self):
        from api.suggestions import search
        result = search('a')
        self.assertIsNone(result)

    def test_returns_none_for_empty_text(self):
        from api.suggestions import search
        result = search('')
        self.assertIsNone(result)

    def test_returns_none_when_no_match(self):
        from api.suggestions import search
        result = search('zzzxyznonexistent')
        self.assertIsNone(result)

    def test_boosts_by_tag_overlap(self):
        from api.suggestions import search
        result = search('El costo', conversation_tags=['pricing', 'domicilio'])
        self.assertIsNotNone(result)
        self.assertIn('$5,000', result['suggestion'])

    def test_partial_phrase_match(self):
        from api.suggestions import search
        result = search('gracias por')
        self.assertIsNotNone(result)
        self.assertIn('gracias', result['suggestion'])

    def test_usage_count_used_as_tiebreaker(self):
        from api.suggestions import search
        MessageSuggestion.objects.create(
            text='Hola, ¿cómo estás? ¿Necesitas algo?',
            tags=['saludo'],
            usage_count=50,
        )
        result = search('Hola,')
        self.assertIsNotNone(result)
        self.assertEqual(result['suggestion'], 'Hola, ¿cómo estás? ¿Necesitas algo?')


class IndexMessageTests(TestCase):
    """Test the index_message() function."""

    def test_creates_new_entry(self):
        from api.suggestions import index_message
        index_message('Hola, gracias por comunicarte con Domii.', tags=['saludo'])
        self.assertEqual(MessageSuggestion.objects.count(), 1)
        obj = MessageSuggestion.objects.first()
        self.assertEqual(obj.text, 'Hola, gracias por comunicarte con Domii.')
        self.assertEqual(obj.tags, ['saludo'])
        self.assertEqual(obj.usage_count, 1)

    def test_upserts_duplicate_text(self):
        from api.suggestions import index_message
        index_message('Mensaje repetido.', tags=['test'])
        index_message('Mensaje repetido.', tags=['test', 'otro'])
        self.assertEqual(MessageSuggestion.objects.count(), 1)
        obj = MessageSuggestion.objects.first()
        self.assertEqual(obj.usage_count, 2)
        self.assertEqual(obj.tags, ['test', 'otro'])

    def test_skips_empty_text(self):
        from api.suggestions import index_message
        index_message('')
        index_message('   ')
        index_message(None)
        self.assertEqual(MessageSuggestion.objects.count(), 0)

    def test_tracks_agent_id(self):
        from api.suggestions import index_message
        user = User.objects.create_user(username='agent1', password='test')
        index_message('Mensaje del agente.', agent_id=user.id)
        obj = MessageSuggestion.objects.first()
        self.assertEqual(obj.agent_id, user.id)

    def test_many_unique_messages(self):
        from api.suggestions import index_message
        for i in range(100):
            index_message(f'Mensaje único número {i}.')
        self.assertEqual(MessageSuggestion.objects.count(), 100)


# ── API endpoint tests ─────────────────────────────────────────────────────

class SuggestionsAPITests(APITestCase):
    """Test the POST /api/messages/suggestions/ endpoint."""

    def setUp(self):
        self.user = User.objects.create_user(username='sugguser', password='testpass123')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

        MessageSuggestion.objects.create(
            text='Hola, gracias por comunicarte con Domii.',
            tags=['saludo'],
            usage_count=10,
        )
        MessageSuggestion.objects.create(
            text='Hola, ¿en qué puedo ayudarte?',
            tags=['saludo'],
            usage_count=5,
        )

    def test_requires_auth(self):
        self.client.credentials()
        response = self.client.post('/api/messages/suggestions/', {
            'partial_text': 'Hola',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_returns_suggestion_and_alternatives(self):
        response = self.client.post('/api/messages/suggestions/', {
            'partial_text': 'Hola',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn('suggestion', data)
        self.assertIn('alternatives', data)
        self.assertIsNotNone(data['suggestion'])
        self.assertGreaterEqual(len(data['alternatives']), 1)

    def test_returns_none_for_too_short(self):
        response = self.client.post('/api/messages/suggestions/', {
            'partial_text': 'a',
        }, format='json')
        data = response.json()
        self.assertIsNone(data['suggestion'])
        self.assertEqual(data['alternatives'], [])

    def test_returns_none_for_no_match(self):
        response = self.client.post('/api/messages/suggestions/', {
            'partial_text': 'xyzzyx',
        }, format='json')
        data = response.json()
        self.assertIsNone(data['suggestion'])

    def test_context_tags_included(self):
        conversation = Conversation.objects.create(
            whatsapp_id='test12345', contact_name='Test', contact_phone='5551234',
        )
        ConversationTag.create_tag(
            conversation=conversation, tag_name='saludo',
            expiry_type='never', created_by=self.user,
        )
        response = self.client.post('/api/messages/suggestions/', {
            'conversation_id': conversation.id,
            'partial_text': 'Hola',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIsNotNone(data['suggestion'])


class IndexOnSendTests(APITestCase):
    """Test that sending a message indexes it for autocomplete."""

    def setUp(self):
        self.user = User.objects.create_user(username='senduser', password='testpass123')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)
        self.conversation = Conversation.objects.create(
            whatsapp_id='555sendtest', contact_name='Send Test',
            contact_phone='555sendtest', group=self.group,
        )

    def test_sending_text_indexes_it(self):
        self.client.post(
            f'/api/conversations/{self.conversation.id}/messages/',
            {'direction': 'outbound', 'message_type': 'text', 'content': 'Hola, gracias por comunicarte.'},
        )
        self.assertTrue(
            MessageSuggestion.objects.filter(text='Hola, gracias por comunicarte.').exists()
        )

    def test_sending_media_does_not_index(self):
        self.client.post(
            f'/api/conversations/{self.conversation.id}/messages/',
            {'direction': 'outbound', 'message_type': 'image', 'content': 'http://example.com/img.jpg'},
        )
        self.assertEqual(MessageSuggestion.objects.count(), 0)

    def test_inbound_message_not_indexed(self):
        self.client.post(
            f'/api/conversations/{self.conversation.id}/messages/',
            {'direction': 'inbound', 'message_type': 'text', 'content': 'Customer message'},
        )
        self.assertEqual(MessageSuggestion.objects.count(), 0)

    def test_repeated_message_increments_usage_count(self):
        for _ in range(3):
            self.client.post(
                f'/api/conversations/{self.conversation.id}/messages/',
                {'direction': 'outbound', 'message_type': 'text', 'content': 'Mensaje frecuente.'},
            )
        obj = MessageSuggestion.objects.get(text='Mensaje frecuente.')
        self.assertEqual(obj.usage_count, 3)

    def test_sending_text_also_adds_conversation_tags(self):
        ConversationTag.create_tag(
            conversation=self.conversation, tag_name='pricing',
            expiry_type='never', created_by=self.user,
        )
        self.client.post(
            f'/api/conversations/{self.conversation.id}/messages/',
            {'direction': 'outbound', 'message_type': 'text', 'content': 'El costo es de $5,000.'},
        )
        obj = MessageSuggestion.objects.get(text='El costo es de $5,000.')
        self.assertIn('pricing', obj.tags)
