"""Durable outbound send queue: claims, stale recovery and requeue.

Regression suite for messages stranded at ``metadata.status='sending'`` with no
wamid: uvicorn recycles workers (``--limit-max-requests``) and deploys/restarts
SIGKILL in-flight tasks, so a claimed send whose process died must be requeued
by the sweeper. Rows stranded by older code carry no ``send_claimed_at`` marker
and must never be retried automatically.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from api.bot.config import _clear_cache
from api.models import BotConfig, Conversation, Message
from api.tests import get_or_create_tulua_group
from api.views import (
    _SEND_CLAIM_ERROR,
    _enqueue_outbound_send,
    _outbound_payload,
    _process_pending_messages,
    _recover_stale_sends,
    _run_claimed_send,
    _send_claim_is_current,
    send_whatsapp_outbound,
)

WA_SETTINGS = {
    'WHATSAPP_PHONE_NUMBER_ID': '1234567890',
    'WHATSAPP_API_TOKEN': 'test-token',
    'WHATSAPP_GRAPH_BASE_URL': 'https://graph.test/v20.0',
}


class SendQueueTestCase(TestCase):
    def setUp(self):
        _clear_cache()
        self.group = get_or_create_tulua_group()
        self.conversation = Conversation.objects.create(
            whatsapp_id='573001112233',
            contact_name='Ana',
            contact_phone='573001112233',
            group=self.group,
        )

    def make_message(self, metadata=None, **kwargs):
        return Message.objects.create(
            conversation=self.conversation,
            direction='outbound',
            message_type=kwargs.pop('message_type', 'text'),
            content=kwargs.pop('content', 'hola'),
            sender_name='Agent',
            metadata=dict(metadata or {}),
            **kwargs,
        )

    def make_pending(self, **metadata):
        meta = {
            'status': 'pending',
            'send_attempts': 0,
            'scheduled_for': (timezone.now() - timedelta(seconds=1)).isoformat(),
        }
        meta.update(metadata)
        return self.make_message(meta)

    def make_claimed(self, claimed_ago=600, attempts=1, claim_id='old-claim'):
        return self.make_message({
            'status': 'sending',
            'send_attempts': attempts,
            'send_claimed_at': (timezone.now() - timedelta(seconds=claimed_ago)).isoformat(),
            'send_claim_id': claim_id,
        })


class EnqueueOutboundSendTests(SendQueueTestCase):
    @patch('api.views._start_sweeper')
    @patch('api.views.threading.Timer')
    def test_delayed_message_is_pending_and_scheduled(self, timer, sweeper):
        BotConfig.objects.create(key='send_delay_seconds', value=10)
        _clear_cache()

        message = self.make_message()
        _enqueue_outbound_send(message)

        message.refresh_from_db()
        self.assertEqual(message.metadata['status'], 'pending')
        self.assertEqual(message.metadata['send_attempts'], 0)
        scheduled = datetime.fromisoformat(message.metadata['scheduled_for'])
        self.assertGreater(scheduled, timezone.now())
        timer.assert_called_once()
        timer.return_value.start.assert_called_once()

    @patch('api.views._start_sweeper')
    @patch('api.views._dispatch_send')
    def test_immediate_message_is_due_and_dispatched(self, dispatch, sweeper):
        message = self.make_message()
        with self.captureOnCommitCallbacks(execute=True):
            _enqueue_outbound_send(message, schedule_delay=0)

        message.refresh_from_db()
        self.assertEqual(message.metadata['status'], 'pending')
        scheduled = datetime.fromisoformat(message.metadata['scheduled_for'])
        self.assertLessEqual(scheduled, timezone.now())
        dispatch.assert_called_once_with(message.id)


class ClaimAndSendTests(SendQueueTestCase):
    @patch('api.views.send_whatsapp_outbound')
    def test_worker_claims_and_passes_the_claim(self, sender):
        message = self.make_pending()

        _run_claimed_send(message.id)

        message.refresh_from_db()
        self.assertEqual(message.metadata['status'], 'sending')
        self.assertEqual(message.metadata['send_attempts'], 1)
        self.assertTrue(message.metadata['send_claimed_at'])
        claim_id = message.metadata['send_claim_id']
        self.assertTrue(claim_id)
        sender.assert_called_once()
        self.assertEqual(sender.call_args.kwargs['claim_id'], claim_id)

    @patch('api.views.send_whatsapp_outbound')
    def test_second_worker_skips_a_fresh_claim(self, sender):
        message = self.make_claimed(claimed_ago=1, claim_id='live')

        _run_claimed_send(message.id)

        sender.assert_not_called()

    @patch('api.views.send_whatsapp_outbound')
    def test_future_pending_message_is_not_sent(self, sender):
        message = self.make_pending(
            scheduled_for=(timezone.now() + timedelta(minutes=5)).isoformat(),
        )

        _run_claimed_send(message.id)

        sender.assert_not_called()
        message.refresh_from_db()
        self.assertEqual(message.metadata['status'], 'pending')
        self.assertNotIn('send_claimed_at', message.metadata)

    @patch('api.views.send_whatsapp_outbound')
    def test_cancelled_message_is_not_sent(self, sender):
        message = self.make_message({'status': 'cancelled'})

        _run_claimed_send(message.id)

        sender.assert_not_called()

    @patch('api.views.send_whatsapp_outbound')
    def test_message_with_a_wamid_is_not_sent_again(self, sender):
        message = self.make_claimed()
        message.whatsapp_message_id = 'wamid.already'
        message.save(update_fields=['whatsapp_message_id'])

        _run_claimed_send(message.id)

        sender.assert_not_called()

    @patch('api.views.send_whatsapp_outbound')
    def test_attempt_cap_marks_the_message_failed(self, sender):
        message = self.make_claimed(attempts=3)

        _run_claimed_send(message.id)

        sender.assert_not_called()
        message.refresh_from_db()
        self.assertEqual(message.metadata['status'], 'failed')
        self.assertEqual(message.metadata['send_error'], _SEND_CLAIM_ERROR)


class StaleSendRecoveryTests(SendQueueTestCase):
    @patch('api.views._dispatch_send')
    def test_stale_claim_is_requeued_with_a_new_claim_id(self, dispatch):
        message = self.make_claimed(claimed_ago=600)

        _recover_stale_sends()

        dispatch.assert_called_once_with(message.id)
        message.refresh_from_db()
        self.assertEqual(message.metadata['status'], 'sending')
        self.assertNotEqual(message.metadata['send_claim_id'], 'old-claim')
        self.assertEqual(message.metadata['send_attempts'], 1)

    @patch('api.views._dispatch_send')
    def test_fresh_claim_is_not_touched(self, dispatch):
        self.make_claimed(claimed_ago=5)

        _recover_stale_sends()

        dispatch.assert_not_called()

    @patch('api.views._dispatch_send')
    def test_legacy_sending_row_is_never_requeued(self, dispatch):
        # Rows stranded by older code have no send_claimed_at marker.
        message = self.make_message({'status': 'sending'})

        _recover_stale_sends()

        dispatch.assert_not_called()
        message.refresh_from_db()
        self.assertEqual(message.metadata['status'], 'sending')

    @patch('api.views._dispatch_send')
    def test_exhausted_attempts_marks_the_message_failed(self, dispatch):
        message = self.make_claimed(claimed_ago=600, attempts=3)

        _recover_stale_sends()

        dispatch.assert_not_called()
        message.refresh_from_db()
        self.assertEqual(message.metadata['status'], 'failed')
        self.assertEqual(message.metadata['send_error'], _SEND_CLAIM_ERROR)

    @patch('api.views._dispatch_send')
    def test_configured_lease_recovers_sooner(self, dispatch):
        BotConfig.objects.create(key='send_lease_seconds', value=1)
        _clear_cache()
        message = self.make_claimed(claimed_ago=5)

        _recover_stale_sends()

        dispatch.assert_called_once_with(message.id)

    @patch('api.views._dispatch_send')
    def test_message_with_a_wamid_is_ignored(self, dispatch):
        message = self.make_claimed(claimed_ago=600)
        message.whatsapp_message_id = 'wamid.already'
        message.save(update_fields=['whatsapp_message_id'])

        _recover_stale_sends()

        dispatch.assert_not_called()


class PendingSweepTests(SendQueueTestCase):
    @patch('api.views._dispatch_send')
    def test_only_due_pending_messages_are_dispatched(self, dispatch):
        due = self.make_pending()
        self.make_pending(scheduled_for=(timezone.now() + timedelta(hours=1)).isoformat())
        self.make_claimed(claimed_ago=600)

        _process_pending_messages()

        dispatch.assert_called_once_with(due.id)


class SendClaimGuardTests(SendQueueTestCase):
    def test_claim_is_current(self):
        message = self.make_message({'status': 'sending', 'send_claim_id': 'abc'})
        self.assertTrue(_send_claim_is_current(message.id, 'abc'))
        self.assertFalse(_send_claim_is_current(message.id, 'other'))

    def test_missing_message_is_not_current(self):
        self.assertFalse(_send_claim_is_current(999999, 'abc'))

    @override_settings(**WA_SETTINGS)
    @patch('api.views.urllib.request')
    def test_superseded_claim_aborts_before_http(self, mock_request):
        message = self.make_claimed(claim_id='new-claim')

        send_whatsapp_outbound(
            'text', 'hola', self.conversation.contact_phone,
            message.id, self.conversation.id, claim_id='stale-claim',
        )

        mock_request.urlopen.assert_not_called()

    @override_settings(**WA_SETTINGS)
    @patch('api.views.acquire_rate_capacity')
    @patch('api.views.urllib.request')
    def test_current_claim_still_sends(self, mock_request, rate):
        message = self.make_claimed(claim_id='live')
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            'messages': [{'id': 'wamid.claim1'}],
        }).encode()
        mock_request.urlopen.return_value.__enter__.return_value = mock_response

        send_whatsapp_outbound(
            'text', 'hola', self.conversation.contact_phone,
            message.id, self.conversation.id, claim_id='live',
        )

        mock_request.urlopen.assert_called_once()
        message.refresh_from_db()
        self.assertEqual(message.whatsapp_message_id, 'wamid.claim1')
        self.assertEqual(message.metadata['status'], 'sent')


class OutboundPayloadTests(SendQueueTestCase):
    def test_location_uses_metadata(self):
        payload = {'latitude': 4.08, 'longitude': -76.2}
        message = self.make_message({'location': payload}, message_type='location', content='Tuluá')
        self.assertEqual(_outbound_payload(message), ('location', payload))

    def test_interactive_uses_metadata(self):
        payload = {'type': 'button', 'body': {'text': 'Elige'}}
        message = self.make_message({'interactive': payload}, message_type='interactive', content='Elige')
        self.assertEqual(_outbound_payload(message), ('interactive', payload))

    def test_media_uses_media_url(self):
        message = self.make_message(message_type='image', content='')
        message.media_url = '/media/uploads/images/x.jpg'
        message.save(update_fields=['media_url'])
        self.assertEqual(_outbound_payload(message), ('image', '/media/uploads/images/x.jpg'))

    def test_text_uses_content(self):
        message = self.make_message(content='hola')
        self.assertEqual(_outbound_payload(message), ('text', 'hola'))


class EnqueuedSenderTests(SendQueueTestCase):
    @patch('api.views._send_pool')
    @patch('api.views.publish_conversation_update')
    def test_ops_notification_goes_through_the_queue(self, publish, pool):
        from api.integrations.notify import create_and_send_outbound

        with self.captureOnCommitCallbacks(execute=True):
            message = create_and_send_outbound(self.conversation, 'text', 'Pedido listo')

        message.refresh_from_db()
        self.assertEqual(message.metadata['status'], 'pending')
        pool.submit.assert_called_once()
        self.assertEqual(pool.submit.call_args.args[0].__name__, '_run_claimed_send')
        self.assertEqual(pool.submit.call_args.args[1], message.id)

    @patch('api.views._send_pool')
    @patch('api.views.publish_conversation_update')
    def test_bot_send_reply_goes_through_the_queue(self, publish, pool):
        User.objects.create_user(username='bot', password='pass')
        from api.bot.dispatcher import send_reply

        with self.captureOnCommitCallbacks(execute=True):
            message = send_reply(self.conversation, 'Hola bot')

        message.refresh_from_db()
        self.assertEqual(message.metadata['status'], 'pending')
        pool.submit.assert_called_once()
        self.assertEqual(pool.submit.call_args.args[1], message.id)

    @patch('api.views._send_pool')
    @patch('api.views.publish_conversation_update')
    def test_bot_interactive_message_is_created_before_send(self, publish, pool):
        User.objects.create_user(username='bot', password='pass')
        from api.bot.dispatcher import _send_interactive_payload

        payload = {'type': 'button', 'body': {'text': 'Elige'}, 'action': {'buttons': []}}
        with self.captureOnCommitCallbacks(execute=True):
            _send_interactive_payload(self.conversation, payload)

        message = Message.objects.get(conversation=self.conversation, message_type='interactive')
        self.assertEqual(message.metadata['status'], 'pending')
        self.assertEqual(message.metadata['interactive'], payload)
        pool.submit.assert_called_once()
        self.assertEqual(pool.submit.call_args.args[1], message.id)
