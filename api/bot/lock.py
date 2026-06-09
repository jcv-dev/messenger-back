"""Distributed conversation lock backed by Redis.

Prevents concurrent processing of the same conversation by multiple
bot workers or overlapping events.  Uses Redis ``SET NX EX`` for
atomic acquire with auto-expiry.

Usage::

    async with conversation_lock(conversation_id):
        … process message …

If the lock cannot be acquired (another worker or event is processing
this conversation), returns ``False`` and the caller should skip the
event.  Fails open if Redis is unreachable.
"""

from __future__ import annotations

import contextlib
import logging
import os

import redis.asyncio as aioredis
from django.conf import settings

logger = logging.getLogger("api.bot.lock")

_LOCK_TTL = int(os.environ.get("BOT_LOCK_TTL", "60"))
_LOCK_KEY_TPL = "bot:lock:{conversation_id}"


async def _get_async_redis():
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)


async def acquire_conversation_lock(conversation_id: str) -> bool:
    """Try to acquire the distributed lock for *conversation_id*.

    Returns ``True`` if acquired, ``False`` if already locked or Redis
    is unavailable.
    """
    key = _LOCK_KEY_TPL.format(conversation_id=conversation_id)
    try:
        r = await _get_async_redis()
        acquired = await r.set(key, "1", nx=True, ex=_LOCK_TTL)
        await r.aclose()
        return bool(acquired)
    except Exception:
        logger.exception("Failed to acquire conversation lock (fails open)")
        return True  # fails open — let processing proceed


async def release_conversation_lock(conversation_id: str) -> None:
    """Release the distributed lock for *conversation_id*."""
    key = _LOCK_KEY_TPL.format(conversation_id=conversation_id)
    try:
        r = await _get_async_redis()
        await r.delete(key)
        await r.aclose()
    except Exception:
        logger.exception("Failed to release conversation lock")


@contextlib.asynccontextmanager
async def conversation_lock(conversation_id: str):
    """Async context manager that acquires + releases the lock.

    Yields ``True`` if lock was acquired, ``False`` otherwise.
    """
    locked = await acquire_conversation_lock(conversation_id)
    try:
        yield locked
    finally:
        if locked:
            await release_conversation_lock(conversation_id)
