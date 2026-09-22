"""Outbound sends to username-only (BSUID) WhatsApp contacts.

Meta delivers some contacts without a phone number: ``contacts[].user_id`` is a
business-scoped user ID such as ``CO.956283237534428`` and the webhook stores it
as ``Conversation.whatsapp_id`` with an empty ``contact_phone``. Outbound sends
must target the top-level ``recipient`` field instead of ``to``.
"""

import json
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from api.models import Conversation, Message
from api.tests import get_or_create_tulua_group
from api.views import (
    _handle_message_status_webhook,
    _resolve_whatsapp_target,
    send_whatsapp_outbound,
)

BSUID = 'CO.956283237534428'
PHONE = '573001112233'

WA_SETTINGS = {
    'WHATSAPP_PHONE_NUMBER_ID': '1234567890',
    'WHATSAPP_API_TOKEN': 'test-token',
    'WHATSAPP_GRAPH_BASE_URL': 'https://graph.test/v20.0',
}


@override_settings(**WA_SETTINGS)
class ResolveWhatsappTargetTests(TestCase):
    def setUp(self):
        self.group = get_or_create_tulua_group()

    def test_phone_goes_to_to(self):
        self.assertEqual(_resolve_whatsapp_target(PHONE), (PHONE, None))

    def test_phone_with_plus_is_stripped(self):
        self.assertEqual(_resolve_whatsapp_target(f'+{PHONE}'), (PHONE, None))

    def test_bsuid_goes_to_recipient(self):
        self.assertEqual(_resolve_whatsapp_target(BSUID), (None, BSUID))

    def test_explicit_recipient_wins_when_phone_missing(self):
        self.assertEqual(_resolve_whatsapp_target(None, recipient=BSUID), (None, BSUID))

    def test_phone_wins_over_recipient(self):
        self.assertEqual(_resolve_whatsapp_target(PHONE, recipient=BSUID), (PHONE, None))

    def test_username_conversation_falls_back_to_whatsapp_id(self):
        conv = Conversation.objects.create(
            whatsapp_id=BSUID, contact_name='Mao', contact_phone=None, group=self.group,
        )
        self.assertEqual(
            _resolve_whatsapp_target(None, conversation_id=conv.id), (None, BSUID),
        )

    def test_phone_conversation_falls_back_to_whatsapp_id(self):
        conv = Conversation.objects.create(
            whatsapp_id=PHONE, contact_name='Phone', contact_phone=None, group=self.group,
        )
        self.assertEqual(
            _resolve_whatsapp_target(None, conversation_id=conv.id), (PHONE, None),
        )

    def test_no_target(self):
        self.assertEqual(_resolve_whatsapp_target(None), (None, None))


@override_settings(**WA_SETTINGS)
class BsuidSendTests(TestCase):
    def setUp(self):
        self.group = get_or_create_tulua_group()
        self.conv = Conversation.objects.create(
            whatsapp_id=BSUID, contact_name='Mao', contact_phone=None, group=self.group,
        )
        self.msg = Message.objects.create(
            conversation=self.conv, direction='outbound', message_type='text',
            content='buenas', sender_name='Agent',
        )

    @patch('api.views.acquire_rate_capacity')
    @patch('api.views.publish_conversation_update')
    @patch('api.views.urllib.request')
    def test_send_targets_recipient_field(self, mock_request, mock_publish, mock_acquire):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            'messaging_product': 'whatsapp',
            'contacts': [{'input': BSUID, 'user_id': BSUID}],
            'messages': [{'id': 'wamid.bsuid1'}],
        }).encode()
        mock_request.urlopen.return_value.__enter__.return_value = mock_response

        send_whatsapp_outbound(
            'text', 'buenas', self.conv.contact_phone,
            message_id=self.msg.id, conversation_id=self.conv.id,
        )

        payload = json.loads(mock_request.Request.call_args.kwargs['data'].decode())
        self.assertEqual(payload['recipient'], BSUID)
        self.assertNotIn('to', payload)

        self.msg.refresh_from_db()
        self.assertEqual(self.msg.whatsapp_message_id, 'wamid.bsuid1')
        self.assertEqual(self.msg.metadata['status'], 'sent')

    @patch('api.views.acquire_rate_capacity')
    @patch('api.views.publish_conversation_update')
    @patch('api.views.urllib.request')
    def test_phone_send_still_uses_to(self, mock_request, mock_publish, mock_acquire):
        conv = Conversation.objects.create(
            whatsapp_id=PHONE, contact_name='Phone', contact_phone=PHONE, group=self.group,
        )
        msg = Message.objects.create(
            conversation=conv, direction='outbound', message_type='text',
            content='hola', sender_name='Agent',
        )
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            'messages': [{'id': 'wamid.phone1'}],
        }).encode()
        mock_request.urlopen.return_value.__enter__.return_value = mock_response

        send_whatsapp_outbound(
            'text', 'hola', conv.contact_phone, message_id=msg.id, conversation_id=conv.id,
        )

        payload = json.loads(mock_request.Request.call_args.kwargs['data'].decode())
        self.assertEqual(payload['to'], PHONE)
        self.assertNotIn('recipient', payload)

    @patch('api.views._mark_send_failed')
    def test_blocked_send_marks_message_failed(self, mock_mark_failed):
        send_whatsapp_outbound('text', 'hola', None, message_id=99)

        mock_mark_failed.assert_called_once()
        args = mock_mark_failed.call_args
        self.assertEqual(args.args[0], 99)
        self.assertIn('no tiene número', args.args[2])

    def test_status_webhook_accepts_recipient_user_id(self):
        self.msg.whatsapp_message_id = 'wamid.bsuid2'
        self.msg.metadata = {'status': 'sent'}
        self.msg.save(update_fields=['whatsapp_message_id', 'metadata'])

        _handle_message_status_webhook({
            'id': 'wamid.bsuid2',
            'status': 'delivered',
            'timestamp': '1790101800',
            'recipient_user_id': BSUID,
        })

        self.msg.refresh_from_db()
        self.assertEqual(self.msg.metadata.get('delivery_status'), 'delivered')
