"""Tests for api.bot.lock — distributed conversation lock."""

from unittest.mock import patch, AsyncMock

from django.test import SimpleTestCase

from api.bot.lock import acquire_conversation_lock, release_conversation_lock


class ConversationLockTests(SimpleTestCase):
    @patch("api.bot.lock.aioredis.from_url")
    async def test_acquire_success(self, mock_redis):
        mock_conn = AsyncMock()
        mock_conn.set.return_value = True
        mock_redis.return_value = mock_conn

        result = await acquire_conversation_lock("conv-123")
        self.assertTrue(result)
        mock_conn.set.assert_called_once_with(
            "bot:lock:conv-123", "1", nx=True, ex=60
        )

    @patch("api.bot.lock.aioredis.from_url")
    async def test_acquire_duplicate(self, mock_redis):
        mock_conn = AsyncMock()
        mock_conn.set.return_value = None  # Redis returns None when key exists
        mock_redis.return_value = mock_conn

        result = await acquire_conversation_lock("conv-123")
        self.assertFalse(result)

    @patch("api.bot.lock.aioredis.from_url")
    async def test_release(self, mock_redis):
        mock_conn = AsyncMock()
        mock_redis.return_value = mock_conn

        await release_conversation_lock("conv-123")
        mock_conn.delete.assert_called_once_with("bot:lock:conv-123")

    @patch("api.bot.lock.aioredis.from_url")
    async def test_acquire_redis_down(self, mock_redis):
        mock_redis.side_effect = Exception("Redis unreachable")

        result = await acquire_conversation_lock("conv-123")
        self.assertTrue(result)  # fails open

    @patch("api.bot.lock.aioredis.from_url")
    async def test_release_redis_down(self, mock_redis):
        mock_redis.side_effect = Exception("Redis unreachable")
        # Should not raise
        await release_conversation_lock("conv-123")
