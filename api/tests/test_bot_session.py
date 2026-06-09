"""Tests for api.bot.session — Redis-backed session state."""

from unittest.mock import patch, MagicMock

from django.test import SimpleTestCase

from api.bot.session import save_session, get_session, delete_session, create_session


class SessionTests(SimpleTestCase):
    def setUp(self):
        self.conv_id = "conv-test-1"
        self.session = {
            "state": "llm",
            "mode": "llm",
            "data": {},
            "history": [{"role": "user", "content": "Hola"}],
            "fallback_count": 0,
        }

    @patch("api.bot.session.get_sync_redis")
    def test_save_and_get(self, mock_get_redis):
        mock_r = MagicMock()
        mock_get_redis.return_value = mock_r
        mock_r.hgetall.return_value = {
            b"state": b"llm",
            b"mode": b"llm",
            b"data": b"{}",
            b"history": b'[{"role": "user", "content": "Hola"}]',
            b"fallback_count": b"0",
            b"last_activity": b"1234567890.0",
        }

        save_session(self.conv_id, self.session)
        mock_r.hset.assert_called_once()
        mock_r.expire.assert_called_once()

        loaded = get_session(self.conv_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["state"], "llm")
        self.assertEqual(len(loaded["history"]), 1)

    @patch("api.bot.session.get_sync_redis")
    def test_get_missing_returns_none(self, mock_get_redis):
        mock_r = MagicMock()
        mock_r.hgetall.return_value = {}
        mock_get_redis.return_value = mock_r

        result = get_session("nonexistent")
        self.assertIsNone(result)

    @patch("api.bot.session.get_sync_redis")
    def test_delete(self, mock_get_redis):
        mock_r = MagicMock()
        mock_get_redis.return_value = mock_r

        delete_session(self.conv_id)
        mock_r.delete.assert_called_once_with(f"bot:session:{self.conv_id}")

    @patch("api.bot.session.get_sync_redis")
    def test_history_truncated_on_save(self, mock_get_redis):
        mock_r = MagicMock()
        mock_get_redis.return_value = mock_r

        # Create session with 50 messages
        big_session = {
            "state": "llm",
            "mode": "llm",
            "data": {},
            "history": [{"role": "user", "content": str(i)} for i in range(50)],
            "fallback_count": 0,
        }

        save_session(self.conv_id, big_session)

        # Check that what was saved has only 40 messages
        saved_mapping = mock_r.hset.call_args[1]["mapping"]
        import json
        saved_history = json.loads(saved_mapping["history"])
        self.assertLessEqual(len(saved_history), 40)
        # Most recent entries should be kept
        self.assertEqual(saved_history[-1]["content"], "49")

    @patch("api.bot.session.get_sync_redis")
    def test_create_session_starts_empty(self, mock_get_redis):
        mock_r = MagicMock()
        mock_get_redis.return_value = mock_r

        session = create_session(self.conv_id)
        self.assertEqual(session["state"], "WELCOME")
        self.assertEqual(session["history"], [])
        self.assertEqual(session["fallback_count"], 0)
        self.assertEqual(session["mode"], "greeting")

    @patch("api.bot.session.get_sync_redis")
    def test_fallback_count_persisted(self, mock_get_redis):
        mock_r = MagicMock()
        mock_get_redis.return_value = mock_r
        mock_r.hgetall.return_value = {
            b"state": b"llm",
            b"mode": b"llm",
            b"data": b"{}",
            b"history": b"[]",
            b"fallback_count": b"3",
            b"last_activity": b"1234567890.0",
        }

        loaded = get_session(self.conv_id)
        self.assertEqual(loaded["fallback_count"], 3)
