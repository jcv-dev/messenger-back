"""Tests for WhatsApp Calling — model, serializers, API helpers, webhooks, and REST endpoints."""

import json
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import MagicMock, patch, PropertyMock

from django.conf import settings
from django.test import TestCase, SimpleTestCase, override_settings

_LOCMEM_CACHES = {
    'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'},
    'throttle': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'},
}
from django.contrib.auth.models import User
from django.utils import timezone

from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from api.models import Call, Conversation, ConversationTake, CityGroup
from api.views import get_default_group
from api.serializers import CallSerializer


# ---------------------------------------------------------------------------
# Call model tests
# ---------------------------------------------------------------------------

class CallModelTests(TestCase):
    def setUp(self):
        self.group = CityGroup.objects.create(name="Test City")
        self.conversation = Conversation.objects.create(
            whatsapp_id="573001234567",
            contact_name="Test User",
            contact_phone="573001234567",
            group=self.group,
        )

    def test_create_inbound_call(self):
        call = Call.objects.create(
            call_id="wacid_inbound_1",
            conversation=self.conversation,
            direction="inbound",
            status="pending",
            from_number="573001234567",
            to_number="573009999999",
            sdp_offer="v=0\no=...",
        )
        self.assertEqual(call.call_id, "wacid_inbound_1")
        self.assertEqual(call.direction, "inbound")
        self.assertEqual(call.status, "pending")
        self.assertIsNotNone(call.created_at)
        self.assertIsNone(call.start_time)
        self.assertIsNone(call.end_time)

    def test_create_outbound_call(self):
        call = Call.objects.create(
            call_id="wacid_outbound_1",
            conversation=self.conversation,
            direction="outbound",
            status="pending",
            from_number="573009999999",
            to_number="573001234567",
            sdp_offer="v=0\no=offer...",
            recording_status="ENABLED",
            recording_purpose="quality assurance",
        )
        self.assertEqual(call.direction, "outbound")
        self.assertEqual(call.recording_status, "ENABLED")
        self.assertEqual(call.recording_purpose, "quality assurance")

    def test_call_status_transitions(self):
        call = Call.objects.create(
            call_id="wacid_transition_1",
            conversation=self.conversation,
            direction="inbound",
            status="pending",
            from_number="573001234567",
            to_number="573009999999",
        )
        call.status = "connected"
        call.start_time = timezone.now()
        call.save()
        call.refresh_from_db()
        self.assertEqual(call.status, "connected")

        call.status = "completed"
        call.end_time = timezone.now()
        call.duration_seconds = 120
        call.save()
        call.refresh_from_db()
        self.assertEqual(call.status, "completed")
        self.assertEqual(call.duration_seconds, 120)

    def test_call_str(self):
        call = Call.objects.create(
            call_id="wacid_str_1",
            conversation=self.conversation,
            direction="inbound",
            status="pending",
            from_number="573001234567",
            to_number="573009999999",
        )
        self.assertIn("wacid_str_1", str(call))
        self.assertIn("inbound", str(call))

    def test_call_ordering(self):
        c1 = Call.objects.create(
            call_id="wacid_ord_1", conversation=self.conversation,
            direction="inbound", status="completed",
            from_number="573001234567", to_number="573009999999",
        )
        c2 = Call.objects.create(
            call_id="wacid_ord_2", conversation=self.conversation,
            direction="inbound", status="completed",
            from_number="573001234567", to_number="573009999999",
        )
        calls = list(Call.objects.all()[:2])
        self.assertEqual(calls[0], c2)
        self.assertEqual(calls[1], c1)

    def test_call_unique_call_id(self):
        Call.objects.create(
            call_id="wacid_unique", conversation=self.conversation,
            direction="inbound", status="pending",
            from_number="573001234567", to_number="573009999999",
        )
        with self.assertRaises(Exception):
            Call.objects.create(
                call_id="wacid_unique", conversation=self.conversation,
                direction="inbound", status="pending",
                from_number="573001234567", to_number="573009999999",
            )

    def test_recording_fields(self):
        call = Call.objects.create(
            call_id="wacid_rec_1", conversation=self.conversation,
            direction="inbound", status="completed",
            from_number="573001234567", to_number="573009999999",
            recording_audio_id="audio_123",
            recording_audio_url="https://graph.facebook.com/v20.0/audio_123",
            recording_audio_sha256="abc123",
            recording_audio_mime_type="audio/ogg",
        )
        self.assertEqual(call.recording_audio_id, "audio_123")
        self.assertEqual(call.recording_audio_mime_type, "audio/ogg")

    def test_call_error_fields(self):
        call = Call.objects.create(
            call_id="wacid_err_1", conversation=self.conversation,
            direction="inbound", status="failed",
            from_number="573001234567", to_number="573009999999",
            error_code=500, error_message="Internal server error",
        )
        self.assertEqual(call.error_code, 500)
        self.assertEqual(call.error_message, "Internal server error")


# ---------------------------------------------------------------------------
# Call serializer tests
# ---------------------------------------------------------------------------

class CallSerializerTests(SimpleTestCase):
    def setUp(self):
        self.group = CityGroup(name="Test City")
        self.conversation = MagicMock()
        self.conversation.contact_name = "Test User"
        self.conversation.id = 1
        self.conversation.contact_phone = "573001234567"
        type(self.conversation).contact_name = PropertyMock(return_value="Test User")
        self.conversation.configure_mock(contact_name="Test User")

    def test_serializer_fields(self):
        call = MagicMock()
        call.id = 1
        call.call_id = "wacid_ser_1"
        call.conversation = self.conversation
        call.conversation_id = 1
        call.direction = "inbound"
        call.status = "completed"
        call.from_number = "573001234567"
        call.to_number = "573009999999"
        call.recipient_bsuid = None
        call.start_time = None
        call.end_time = None
        call.duration_seconds = 60
        call.biz_opaque_callback_data = ""
        call.deeplink_payload = None
        call.cta_payload = None
        call.recording_status = None
        call.recording_purpose = None
        call.recording_announcement_language = None
        call.recording_audio_id = None
        call.recording_audio_url = None
        call.recording_audio_sha256 = None
        call.recording_audio_mime_type = None
        call.sdp_offer = ""
        call.sdp_answer = ""
        call.error_code = None
        call.error_message = None
        call.created_at = datetime.now(dt_timezone.utc)
        call.updated_at = datetime.now(dt_timezone.utc)

        serializer = CallSerializer(call)
        data = serializer.data
        self.assertEqual(data["call_id"], "wacid_ser_1")
        self.assertEqual(data["direction"], "inbound")
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["duration_seconds"], 60)
        self.assertIn("contact_name", data)


# ---------------------------------------------------------------------------
# WhatsApp Calling API helper tests
# ---------------------------------------------------------------------------

class CallingAPIHelperTests(SimpleTestCase):
    @patch("api.views.settings")
    @patch("api.views.urllib.request")
    @patch("api.views.acquire_rate_capacity")
    def test_call_whatsapp_api_success(self, mock_acquire, mock_request, mock_settings):
        mock_settings.WHATSAPP_API_TOKEN = "test_token"
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"calls": [{"id": "wacid_123"}]}'
        mock_request.urlopen.return_value.__enter__.return_value = mock_response

        from api.views import _call_whatsapp_api
        result = _call_whatsapp_api("123", {"action": "test"})
        self.assertEqual(result["calls"][0]["id"], "wacid_123")
        mock_acquire.assert_called_once_with("123")

    @patch("api.views.settings")
    @patch("api.views.urllib.request")
    @patch("api.views.acquire_rate_capacity")
    def test_call_whatsapp_api_http_error(self, mock_acquire, mock_request, mock_settings):
        mock_settings.WHATSAPP_API_TOKEN = "test_token"
        mock_request.urlopen.side_effect = __import__("urllib").error.HTTPError(
            url="", code=400, msg="Bad Request", hdrs={}, fp=None
        )

        from api.views import CallAPIError, _call_whatsapp_api
        with self.assertRaises(CallAPIError):
            _call_whatsapp_api("123", {"action": "test"})

    @patch("api.views.settings")
    @patch("api.views._call_whatsapp_api")
    def test_send_whatsapp_call_action(self, mock_call_api, mock_settings):
        mock_call_api.return_value = {"success": True}
        from api.views import send_whatsapp_call_action
        result = send_whatsapp_call_action(
            "123", "accept", call_id="wacid_1", to="573001234567",
            session={"sdp_type": "answer", "sdp": "v=0"},
            recording={"status": "ENABLED", "purpose": "qa"},
        )
        self.assertTrue(result["success"])
        mock_call_api.assert_called_once()
        args, _ = mock_call_api.call_args
        payload = args[1]
        self.assertEqual(payload["action"], "accept")
        self.assertEqual(payload["call_id"], "wacid_1")
        self.assertEqual(payload["recording"]["status"], "ENABLED")

    @patch("api.views.settings")
    @patch("api.views.send_whatsapp_call_action")
    def test_pre_accept_call(self, mock_send, mock_settings):
        mock_send.return_value = {"success": True}
        mock_settings.WHATSAPP_PHONE_NUMBER_ID = "123"
        from api.views import pre_accept_call
        pre_accept_call("wacid_1", "v=0\nsdp answer")
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        self.assertEqual(args[1], "pre_accept")
        self.assertEqual(kwargs.get("session", {}).get("sdp_type"), "answer")

    @patch("api.views.settings")
    @patch("api.views.send_whatsapp_call_action")
    def test_accept_call_with_recording(self, mock_send, mock_settings):
        mock_send.return_value = {"success": True}
        mock_settings.WHATSAPP_PHONE_NUMBER_ID = "123"
        from api.views import accept_call
        accept_call("wacid_1", "v=0\nanswer", recording={"status": "ENABLED"})
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        self.assertEqual(args[1], "accept")
        self.assertEqual(kwargs.get("recording", {}).get("status"), "ENABLED")

    @patch("api.views.settings")
    @patch("api.views.send_whatsapp_call_action")
    def test_reject_call(self, mock_send, mock_settings):
        mock_settings.WHATSAPP_PHONE_NUMBER_ID = "123"
        from api.views import reject_call
        reject_call("wacid_1")
        mock_send.assert_called_once_with("123", "reject", call_id="wacid_1")

    @patch("api.views.settings")
    @patch("api.views.send_whatsapp_call_action")
    def test_terminate_call(self, mock_send, mock_settings):
        mock_settings.WHATSAPP_PHONE_NUMBER_ID = "123"
        from api.views import terminate_call
        terminate_call("wacid_1")
        mock_send.assert_called_once_with("123", "terminate", call_id="wacid_1")

    @patch("api.views.settings")
    @patch("api.views.send_whatsapp_call_action")
    def test_initiate_call_success(self, mock_send, mock_settings):
        mock_send.return_value = {"calls": [{"id": "wacid_new_1"}]}
        mock_settings.WHATSAPP_PHONE_NUMBER_ID = "123"
        from api.views import initiate_call
        result = initiate_call(
            to_number="573001234567", sdp_offer="v=0\noffer",
            recording={"status": "ENABLED"},
        )
        self.assertEqual(result, "wacid_new_1")
        args, kwargs = mock_send.call_args
        self.assertEqual(args[1], "connect")
        self.assertEqual(kwargs.get("to"), "573001234567")

    @patch("api.views.settings")
    @patch("api.views.send_whatsapp_call_action")
    def test_initiate_call_no_calls_response(self, mock_send, mock_settings):
        mock_send.return_value = {}
        mock_settings.WHATSAPP_PHONE_NUMBER_ID = "123"
        from api.views import initiate_call
        result = initiate_call(to_number="573001234567", sdp_offer="v=0")
        self.assertIsNone(result)

    def test_initiate_call_no_identifier_raises(self):
        from api.views import initiate_call
        with self.assertRaises(ValueError):
            initiate_call(sdp_offer="v=0")


# ---------------------------------------------------------------------------
# _resolve_conversation tests
# ---------------------------------------------------------------------------

class ResolveConversationTests(TestCase):
    def setUp(self):
        self.group = CityGroup.objects.create(name="Test City")
        self.conversation = Conversation.objects.create(
            whatsapp_id="573001234567",
            contact_name="Existing User",
            contact_phone="573001234567",
            group=self.group,
        )

    def test_resolve_by_phone(self):
        from api.views import _resolve_conversation
        metadata = {"display_phone_number": "573009999999"}
        contacts = []
        call_event = {
            "id": "wacid_1",
            "to": "573001234567",
            "from": "573009999999",
        }
        conv = _resolve_conversation(metadata, contacts, call_event)
        self.assertEqual(conv.id, self.conversation.id)

    def test_resolve_by_wa_id_in_contacts(self):
        from api.views import _resolve_conversation
        metadata = {"display_phone_number": "573009999999"}
        contacts = [{"wa_id": "573001234567", "profile": {"name": "Contact"}}]
        call_event = {"id": "wacid_2"}
        conv = _resolve_conversation(metadata, contacts, call_event)
        self.assertEqual(conv.id, self.conversation.id)

    def test_resolve_creates_new(self):
        from api.views import _resolve_conversation
        metadata = {"display_phone_number": "573009999999"}
        contacts = []
        call_event = {"id": "wacid_new", "to": "573005555555"}
        conv = _resolve_conversation(metadata, contacts, call_event)
        self.assertIsNotNone(conv)
        self.assertEqual(conv.whatsapp_id, "573005555555")


# ---------------------------------------------------------------------------
# _handle_call_webhook tests
# ---------------------------------------------------------------------------

class HandleCallWebhookTests(TestCase):
    def setUp(self):
        self.group = CityGroup.objects.create(name="Test City")
        self.conversation = Conversation.objects.create(
            whatsapp_id="573001234567",
            contact_name="Test User",
            contact_phone="573001234567",
            group=self.group,
        )

    @patch("api.views._publish_call_event")
    def test_handle_inbound_connect(self, mock_publish):
        from api.views import _handle_call_webhook
        call_event = {
            "id": "wacid_incoming_1",
            "event": "connect",
            "direction": "USER_INITIATED",
            "from": "573001234567",
            "to": "573009999999",
            "session": {"sdp_type": "offer", "sdp": "v=0\noffer..."},
        }
        _handle_call_webhook(call_event, {"display_phone_number": "573009999999"}, [])
        call = Call.objects.get(call_id="wacid_incoming_1")
        self.assertEqual(call.direction, "inbound")
        self.assertEqual(call.status, "pending")
        self.assertEqual(call.sdp_offer, "v=0\noffer...")
        mock_publish.assert_called_once()

    @patch("api.views._publish_call_event")
    def test_handle_terminate_completed(self, mock_publish):
        call = Call.objects.create(
            call_id="wacid_term_1", conversation=self.conversation,
            direction="inbound", status="connected",
            from_number="573001234567", to_number="573009999999",
        )
        from api.views import _handle_call_webhook
        call_event = {
            "id": "wacid_term_1",
            "event": "terminate",
            "status": ["Completed"],
            "duration": 90,
        }
        _handle_call_webhook(call_event, {"display_phone_number": "573009999999"}, [])
        call.refresh_from_db()
        self.assertEqual(call.status, "completed")
        self.assertEqual(call.duration_seconds, 90)
        mock_publish.assert_called_once()

    @patch("api.views._publish_call_event")
    def test_handle_recording_available(self, mock_publish):
        call = Call.objects.create(
            call_id="wacid_rec_webhook", conversation=self.conversation,
            direction="inbound", status="completed",
            from_number="573001234567", to_number="573009999999",
        )
        from api.views import _handle_call_webhook
        call_event = {
            "id": "wacid_rec_webhook",
            "event": "call_recording_available",
            "call_recording": {
                "audio": {
                    "id": "audio_456",
                    "url": "https://example.com/audio",
                    "sha256": "def456",
                    "mime_type": "audio/ogg",
                }
            },
        }
        _handle_call_webhook(call_event, {}, [])
        call.refresh_from_db()
        self.assertEqual(call.recording_audio_id, "audio_456")
        self.assertEqual(call.recording_audio_url, "https://example.com/audio")
        mock_publish.assert_called_once()


# ---------------------------------------------------------------------------
# _handle_call_status_webhook tests
# ---------------------------------------------------------------------------

class HandleCallStatusWebhookTests(TestCase):
    def setUp(self):
        self.group = CityGroup.objects.create(name="Test City")
        self.conversation = Conversation.objects.create(
            whatsapp_id="573001234567", contact_name="Test",
            contact_phone="573001234567", group=self.group,
        )

    @patch("api.views._publish_call_event")
    def test_status_ringing(self, mock_publish):
        call = Call.objects.create(
            call_id="wacid_stat_1", conversation=self.conversation,
            direction="inbound", status="pending",
            from_number="573001234567", to_number="573009999999",
        )
        from api.views import _handle_call_status_webhook
        _handle_call_status_webhook({"id": "wacid_stat_1", "status": "RINGING"}, {})
        call.refresh_from_db()
        self.assertEqual(call.status, "ringing")
        mock_publish.assert_called_once()

    @patch("api.views._publish_call_event")
    def test_status_accepted(self, mock_publish):
        call = Call.objects.create(
            call_id="wacid_stat_2", conversation=self.conversation,
            direction="outbound", status="ringing",
            from_number="573009999999", to_number="573001234567",
        )
        from api.views import _handle_call_status_webhook
        _handle_call_status_webhook(
            {"id": "wacid_stat_2", "status": "ACCEPTED", "timestamp": "1712345678"}, {}
        )
        call.refresh_from_db()
        self.assertEqual(call.status, "connected")
        self.assertIsNotNone(call.start_time)

    @patch("api.views._publish_call_event")
    def test_status_rejected(self, mock_publish):
        call = Call.objects.create(
            call_id="wacid_stat_3", conversation=self.conversation,
            direction="outbound", status="ringing",
            from_number="573009999999", to_number="573001234567",
        )
        from api.views import _handle_call_status_webhook
        _handle_call_status_webhook({"id": "wacid_stat_3", "status": "REJECTED"}, {})
        call.refresh_from_db()
        self.assertEqual(call.status, "rejected")

    def test_unknown_call_id_skipped(self):
        from api.views import _handle_call_status_webhook
        _handle_call_status_webhook({"id": "nonexistent", "status": "RINGING"}, {})


# ---------------------------------------------------------------------------
# REST endpoint tests (simulated, patched external calls)
# ---------------------------------------------------------------------------

@override_settings(CACHES=_LOCMEM_CACHES)
class CallEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="agent", password="test123", is_staff=True)
        self.token = Token.objects.create(user=self.user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")

        self.group = CityGroup.objects.create(name="Test City")
        self.conversation = Conversation.objects.create(
            whatsapp_id="573001234567", contact_name="Test",
            contact_phone="573001234567", group=self.group,
        )
        self.call = Call.objects.create(
            call_id="wacid_endpoint_1", conversation=self.conversation,
            direction="inbound", status="pending",
            from_number="573001234567", to_number="573009999999",
            sdp_offer="v=0\noffer...",
        )

    def test_call_list(self):
        response = self.client.get("/api/calls/list/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("results", data)
        self.assertIn("cursor", data)
        self.assertIn("has_more", data)

    def test_call_list_with_conversation_filter(self):
        response = self.client.get(
            f"/api/calls/list/?conversation_id={self.conversation.id}"
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["results"]), 1)

    def test_call_list_unauthenticated(self):
        self.client.credentials()
        response = self.client.get("/api/calls/list/")
        self.assertEqual(response.status_code, 401)

    def test_call_active_with_active_call(self):
        response = self.client.get("/api/calls/active/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["active"])

    @patch("api.views.pre_accept_call")
    @patch("api.views.accept_call")
    @patch("api.views._publish_call_event")
    def test_call_answer_success(self, mock_publish, mock_accept, mock_pre_accept):
        response = self.client.post(
            "/api/calls/answer/",
            {"call_id": "wacid_endpoint_1", "sdp": "v=0\nanswer..."},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertIn("recording", data)
        self.assertEqual(data["recording"]["status"], "ENABLED")
        mock_pre_accept.assert_called_once_with("wacid_endpoint_1", "v=0\nanswer...")
        mock_accept.assert_called_once()
        self.call.refresh_from_db()
        self.assertEqual(self.call.recording_status, "ENABLED")
        self.assertEqual(self.call.recording_purpose, "seguridad y calidad")
        self.assertEqual(self.call.recording_announcement_language, "es")

    def test_call_answer_missing_fields(self):
        response = self.client.post(
            "/api/calls/answer/",
            {"call_id": "wacid_endpoint_1"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_call_answer_already_answered(self):
        self.call.status = "connected"
        self.call.save()
        response = self.client.post(
            "/api/calls/answer/",
            {"call_id": "wacid_endpoint_1", "sdp": "v=0\nanswer..."},
            format="json",
        )
        self.assertEqual(response.status_code, 409)

    @patch("api.views.reject_call")
    @patch("api.views._publish_call_event")
    def test_call_reject(self, mock_publish, mock_reject):
        response = self.client.post(
            "/api/calls/reject/",
            {"call_id": "wacid_endpoint_1"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.call.refresh_from_db()
        self.assertEqual(self.call.status, "rejected")

    @patch("api.views.terminate_call")
    @patch("api.views._publish_call_event")
    def test_call_terminate(self, mock_publish, mock_terminate):
        self.call.status = "connected"
        self.call.start_time = timezone.now() - timedelta(seconds=120)
        self.call.save()
        response = self.client.post(
            "/api/calls/terminate/",
            {"call_id": "wacid_endpoint_1"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.call.refresh_from_db()
        self.assertEqual(self.call.status, "completed")
        self.assertGreater(self.call.duration_seconds, 0)

    @patch("api.views.initiate_call")
    @patch("api.views._publish_call_event")
    def test_call_initiate(self, mock_publish, mock_initiate):
        mock_initiate.return_value = "wacid_new_2"
        response = self.client.post(
            "/api/calls/initiate/",
            {"to": "573001234567", "sdp": "v=0\noffer..."},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["call_id"], "wacid_new_2")
        call = Call.objects.get(call_id="wacid_new_2")
        self.assertEqual(call.recording_status, "ENABLED")
        self.assertEqual(call.recording_purpose, "seguridad y calidad")
        self.assertEqual(call.recording_announcement_language, "es")

    def test_call_initiate_missing_fields(self):
        response = self.client.post(
            "/api/calls/initiate/",
            {"sdp": "v=0\noffer..."},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_call_turn_config(self):
        with self.settings(
            TURN_SERVER_URL="turn:turn.example.com:3478",
            TURN_SERVER_USERNAME="test_user",
            TURN_SERVER_CREDENTIAL="test_cred",
        ):
            response = self.client.get("/api/calls/turn-config/")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("iceServers", data)
            self.assertEqual(len(data["iceServers"]), 2)

    def test_call_turn_config_no_turn(self):
        with self.settings(
            TURN_SERVER_URL="turn:localhost:3478",
            TURN_SERVER_USERNAME="",
            TURN_SERVER_CREDENTIAL="",
        ):
            response = self.client.get("/api/calls/turn-config/")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(len(data["iceServers"]), 1)

    @patch("api.views.urllib.request")
    def test_call_settings_get(self, mock_request):
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"calling": {"status": "ENABLED", "call_icon_visibility": "DEFAULT", "callback_permission_status": "ENABLED"}}'
        mock_request.urlopen.return_value.__enter__.return_value = mock_response

        response = self.client.get("/api/calls/settings/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ENABLED")
        self.assertEqual(data["call_icon_visibility"], "DEFAULT")
        self.assertEqual(data["callback_permission_status"], "ENABLED")
        mock_request.Request.assert_called_once()

    @patch("api.views.urllib.request")
    def test_call_settings_get_error(self, mock_request):
        mock_request.urlopen.side_effect = __import__("urllib").error.HTTPError(
            url="", code=400, msg="Bad Request", hdrs={}, fp=None,
        )

        response = self.client.get("/api/calls/settings/")
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("error", data)

    @patch("api.views.urllib.request")
    def test_call_settings_post(self, mock_request):
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"success": true}'
        mock_request.urlopen.return_value.__enter__.return_value = mock_response

        response = self.client.post(
            "/api/calls/settings/",
            {
                "status": "ENABLED",
                "call_icon_visibility": "DISABLE_ALL",
                "callback_permission_status": "DISABLED",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])

        _args, kwargs = mock_request.Request.call_args
        posted_body = json.loads(kwargs["data"].decode())

        self.assertEqual(posted_body["calling"]["status"], "ENABLED")
        self.assertEqual(posted_body["calling"]["call_icon_visibility"], "DISABLE_ALL")
        self.assertEqual(posted_body["calling"]["callback_permission_status"], "DISABLED")

    def test_call_settings_post_no_fields(self):
        response = self.client.post(
            "/api/calls/settings/",
            {"unrelated": "value"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("error", data)

    @patch("api.views.urllib.request")
    def test_call_settings_post_error(self, mock_request):
        mock_request.urlopen.side_effect = __import__("urllib").error.HTTPError(
            url="", code=500, msg="Server Error", hdrs={}, fp=None,
        )

        response = self.client.post(
            "/api/calls/settings/",
            {"status": "ENABLED"},
            format="json",
        )
        self.assertEqual(response.status_code, 500)
        data = response.json()
        self.assertIn("error", data)


# ---------------------------------------------------------------------------
# SSE publish helper tests
# ---------------------------------------------------------------------------

@override_settings(CACHES=_LOCMEM_CACHES)
class PublishCallEventTests(TestCase):
    def setUp(self):
        self.group = CityGroup.objects.create(name="Test City")
        self.conversation = Conversation.objects.create(
            whatsapp_id="573001234567", contact_name="Test",
            contact_phone="573001234567", group=self.group,
        )

    @patch("api.realtime.publish")
    def test_publish_call_event_with_take(self, mock_publish):
        user = User.objects.create_user(username="agent2", password="test")
        ConversationTake.objects.create(
            conversation=self.conversation,
            created_by=user,
            expires_at=timezone.now() + timedelta(minutes=30),
            duration_minutes=30,
        )
        call = Call.objects.create(
            call_id="wacid_pub_1", conversation=self.conversation,
            direction="inbound", status="pending",
            from_number="573001234567", to_number="573009999999",
        )
        from api.views import _publish_call_event
        _publish_call_event(call, "incoming")
        mock_publish.assert_called_once()
        payload = mock_publish.call_args[0][0]
        self.assertEqual(payload["type"], "call.incoming")
        self.assertIn("call", payload)
        self.assertIn("active_take", payload["call"])
        self.assertEqual(
            payload["call"]["active_take"]["created_by_id"], user.id
        )

    @patch("api.realtime.publish")
    def test_publish_call_event_no_take(self, mock_publish):
        call = Call.objects.create(
            call_id="wacid_pub_2", conversation=self.conversation,
            direction="inbound", status="pending",
            from_number="573001234567", to_number="573009999999",
        )
        from api.views import _publish_call_event
        _publish_call_event(call, "connected")
        payload = mock_publish.call_args[0][0]
        self.assertEqual(payload["type"], "call.connected")
        self.assertIsNone(payload["call"]["active_take"])


# ---------------------------------------------------------------------------
# Webhook integration test
# ---------------------------------------------------------------------------

@override_settings(CACHES=_LOCMEM_CACHES)
class WebhookCallProcessingTests(TestCase):
    def setUp(self):
        self.group = CityGroup.objects.create(name="Test City")
        self.conversation = Conversation.objects.create(
            whatsapp_id="573001234567", contact_name="User",
            contact_phone="573001234567", group=self.group,
        )
        self.url = "/webhook/"

    def test_webhook_with_calls_array(self):
        payload = {
            "entry": [{
                "changes": [{
                    "field": "messages",
                    "value": {
                        "messages": [],
                        "calls": [{
                            "id": "wacid_webhook_1",
                            "event": "connect",
                            "direction": "USER_INITIATED",
                            "from": "573001234567",
                            "to": "573009999999",
                            "session": {"sdp_type": "offer", "sdp": "v=0"},
                        }],
                        "contacts": [{
                            "wa_id": "573001234567",
                            "profile": {"name": "User"},
                        }],
                        "metadata": {"display_phone_number": "573009999999"},
                    },
                }],
            }],
        }
        with self.settings(WHATSAPP_APP_SECRET=""):
            response = self.client.post(
                self.url,
                data=json.dumps(payload),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Call.objects.filter(call_id="wacid_webhook_1").exists())

    def test_webhook_with_statuses_array(self):
        call = Call.objects.create(
            call_id="wacid_status_webhook", conversation=self.conversation,
            direction="outbound", status="pending",
            from_number="573009999999", to_number="573001234567",
        )
        payload = {
            "entry": [{
                "changes": [{
                    "field": "messages",
                    "value": {
                        "messages": [],
                        "statuses": [{
                            "id": "wacid_status_webhook",
                            "status": "ACCEPTED",
                            "timestamp": "1712345678",
                        }],
                        "metadata": {"display_phone_number": "573009999999"},
                    },
                }],
            }],
        }
        with self.settings(WHATSAPP_APP_SECRET=""):
            response = self.client.post(
                self.url,
                data=json.dumps(payload),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.status, "connected")
