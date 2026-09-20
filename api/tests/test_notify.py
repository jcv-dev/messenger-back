"""Tests for the outbound notify helpers (plan Phase 0)."""

import json
from unittest.mock import patch

from django.test import TestCase

from api.integrations.notify import (
    build_template_payload,
    create_and_send_outbound,
    send_text,
)
from api.models import Conversation, Message, WhatsAppTemplate


class BuildTemplatePayloadTests(TestCase):
    def make_template(self, components):
        return WhatsAppTemplate.objects.create(
            name='pedido_estado', language='es', category='UTILITY',
            status='APPROVED', components=components,
        )

    def test_body_params_use_named_parameters(self):
        template = self.make_template([{'type': 'body', 'text': 'Hola {{cliente}}'}])
        payload = build_template_payload(template, {'cliente': 'Ana'})

        body = payload['components'][0]
        self.assertEqual(body['type'], 'body')
        self.assertEqual(body['parameters'], [
            {'type': 'text', 'parameter_name': 'cliente', 'text': 'Ana'},
        ])

    def test_header_media_and_buttons_are_not_body_params(self):
        template = self.make_template([
            {'type': 'header', 'format': 'image'},
            {'type': 'body', 'text': 'Hola'},
            {'type': 'buttons', 'buttons': [{'type': 'url', 'text': 'Ver', 'url': 'https://x'}]},
        ])
        payload = build_template_payload(template, {
            'nombre': 'Ana',
            'header_media_id': 'media-1',
            'buttons': ['https://domi.test/o/1234'],
        })

        types = [c['type'] for c in payload['components']]
        self.assertEqual(types, ['header', 'body', 'button'])
        self.assertEqual(payload['components'][0]['parameters'][0]['image']['id'], 'media-1')
        self.assertEqual(payload['components'][1]['parameters'], [
            {'type': 'text', 'parameter_name': 'nombre', 'text': 'Ana'},
        ])
        self.assertEqual(payload['components'][2]['parameters'][0]['url'], 'https://domi.test/o/1234')

    def test_language_code_is_included(self):
        template = self.make_template([{'type': 'body', 'text': 'Hola'}])
        payload = build_template_payload(template)
        self.assertEqual(payload['language'], {'code': 'es'})
        self.assertEqual(payload['name'], 'pedido_estado')


class CreateAndSendOutboundTests(TestCase):
    def setUp(self):
        self.conversation = Conversation.objects.create(
            whatsapp_id='573001234567', contact_name='Ana',
            contact_phone='573001234567',
        )

    @patch('api.views._send_pool')
    @patch('api.views.publish_conversation_update')
    def test_creates_message_and_sends_immediately(self, publish, pool):
        with self.captureOnCommitCallbacks(execute=True):
            message = create_and_send_outbound(self.conversation, 'text', 'Pedido #1234 recibido')

        self.assertEqual(message.message_type, 'text')
        self.assertEqual(message.direction, 'outbound')
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.last_message, 'Pedido #1234 recibido')
        self.assertEqual(self.conversation._last_msg_direction, 'outbound')
        pool.submit.assert_called_once()
        publish.assert_called_once()
        # The message payload rides the SSE so open threads show it immediately.
        self.assertEqual(publish.call_args.args[0], self.conversation)
        self.assertEqual(publish.call_args.args[1]['id'], message.id)
        self.assertEqual(publish.call_args.args[1]['content'], 'Pedido #1234 recibido')

    def test_preview_for_media(self):
        with patch('api.views._send_pool'), patch('api.views.publish_conversation_update'):
            with self.captureOnCommitCallbacks(execute=True):
                create_and_send_outbound(self.conversation, 'document', '/media/factura.pdf')

        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.last_message, '[Document]')

    def test_message_has_no_pending_status(self):
        with patch('api.views._send_pool'), patch('api.views.publish_conversation_update'):
            with self.captureOnCommitCallbacks(execute=True):
                message = create_and_send_outbound(self.conversation, 'text', 'Hola')

        message.refresh_from_db()
        self.assertNotEqual(message.metadata.get('status'), 'pending')
        self.assertTrue(Message.objects.filter(id=message.id).exists())

    @patch('api.views._send_pool')
    @patch('api.views.publish_conversation_update')
    def test_fallback_template_is_stored_in_metadata(self, publish, pool):
        fallback = {
            'status': 'en_ruta', 'name': 'aviso_en_ruta', 'language': 'es',
            'params': ['order_number'], 'values': {'order_number': '#1234'},
        }
        with self.captureOnCommitCallbacks(execute=True):
            message = send_text(
                self.conversation, 'Pedido en camino', fallback_template=fallback,
            )

        message.refresh_from_db()
        self.assertEqual(message.metadata['fallback_template']['name'], 'aviso_en_ruta')
        # The pool receives the delivery wrapper (fallback-aware), not the raw
        # sender, so a window rejection can be replaced by the template.
        from api.integrations.notify import _deliver_outbound

        self.assertEqual(pool.submit.call_args.args[0], _deliver_outbound)
        self.assertEqual(pool.submit.call_args.args[3]['name'], 'aviso_en_ruta')

    @patch('api.views._send_pool')
    @patch('api.views.publish_conversation_update')
    def test_preview_overrides_the_conversation_last_message(self, publish, pool):
        with self.captureOnCommitCallbacks(execute=True):
            message = create_and_send_outbound(
                self.conversation, 'template',
                json.dumps({'name': 'aviso_en_ruta', 'language': {'code': 'es'}, 'components': []}),
                preview='🛵 Pedido #1234 en camino.',
            )

        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.last_message, '🛵 Pedido #1234 en camino.')
        self.assertEqual(message.message_type, 'template')
