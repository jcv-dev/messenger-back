"""Per-conversation inbound rate limiter.

Prevents runaway API costs by limiting how many inbound messages per
minute the bot will process for a single conversation.
"""

from __future__ import annotations

import logging
import os
import time

import redis.asyncio as aioredis
from django.conf import settings

logger = logging.getLogger("api.bot.limits")

_WINDOW = int(os.environ.get("BOT_RATE_WINDOW", "60"))
_KEY_TPL = "bot:inbound_rate:{conversation_id}:{window}"
_TTL = _WINDOW + 60  # keep key around a bit longer than the window


def _get_threshold() -> int:
    return getattr(settings, "BOT_INBOUND_RATE_LIMIT", 10)


async def check_inbound_rate(conversation_id: str) -> bool:
    """Check and increment the inbound rate counter.

    Returns ``True`` if under the limit (allow processing).
    Returns ``False`` if over the limit (skip / escalate).
    Fails open (returns ``True``) if Redis is unreachable.
    """
    threshold = _get_threshold()
    window = int(time.time()) // _WINDOW
    key = _KEY_TPL.format(conversation_id=conversation_id, window=window)

    try:
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, _TTL)
        await r.aclose()
    except Exception:
        logger.exception("Inbound rate limiter Redis error — fails open")
        return True  # fails open

    if count > threshold:
        logger.warning(
            "Inbound rate limit exceeded for conv=%s (count=%d, threshold=%d)",
            conversation_id, count, threshold,
        )
        return False

    return True
