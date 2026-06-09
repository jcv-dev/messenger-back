"""Tests for api.bot.limits — inbound rate limiter."""

from unittest.mock import patch, AsyncMock

from django.test import SimpleTestCase, override_settings
from django.conf import settings

from api.bot.limits import check_inbound_rate


class InboundRateLimitTests(SimpleTestCase):
    @patch("api.bot.limits.aioredis.from_url")
    async def test_first_message_allowed(self, mock_redis):
        mock_conn = AsyncMock()
        mock_conn.incr.return_value = 1  # first in window
        mock_redis.return_value = mock_conn

        result = await check_inbound_rate("conv-1")
        self.assertTrue(result)
        mock_conn.expire.assert_called_once()

    @patch("api.bot.limits.aioredis.from_url")
    @override_settings(BOT_INBOUND_RATE_LIMIT=3)
    async def test_under_limit_allowed(self, mock_redis):
        mock_conn = AsyncMock()
        mock_conn.incr.return_value = 3  # exactly at threshold
        mock_redis.return_value = mock_conn

        result = await check_inbound_rate("conv-1")
        self.assertTrue(result)

    @patch("api.bot.limits.aioredis.from_url")
    @override_settings(BOT_INBOUND_RATE_LIMIT=3)
    async def test_over_limit_blocked(self, mock_redis):
        mock_conn = AsyncMock()
        mock_conn.incr.return_value = 4  # exceeded
        mock_redis.return_value = mock_conn

        result = await check_inbound_rate("conv-1")
        self.assertFalse(result)

    @patch("api.bot.limits.aioredis.from_url")
    async def test_redis_down_fails_open(self, mock_redis):
        mock_redis.side_effect = Exception("Redis unreachable")

        result = await check_inbound_rate("conv-1")
        self.assertTrue(result)

    @patch("api.bot.limits.aioredis.from_url")
    @override_settings(BOT_INBOUND_RATE_LIMIT=3)
    async def test_different_conversations_independent(self, mock_redis):
        shared_conn = AsyncMock()
        shared_conn.incr.side_effect = [5, 1]
        mock_redis.return_value = shared_conn

        result_1 = await check_inbound_rate("conv-1")  # 5 > 3 → blocked
        result_2 = await check_inbound_rate("conv-2")  # 1 <= 3 → allowed
        self.assertFalse(result_1)
        self.assertTrue(result_2)
