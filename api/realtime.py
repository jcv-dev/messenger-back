"""Redis-backed realtime event broadcaster for SSE clients."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from itertools import count
from typing import Any, Callable

import redis.asyncio as aioredis
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist

from .redis_client import get_sync_redis, reset_sync_redis

logger = logging.getLogger(__name__)

REDIS_CHANNEL = "sse:events"
GROUP_CHANNEL_PREFIX = "sse:group"

_next_seq = count(1)

# --- sync publish (called from sync DRF views) ---


def publish(event: dict[str, Any], group_id: int | None = None) -> None:
    seq = next(_next_seq)
    event["_seq"] = seq
    payload = json.dumps(event, default=str)
    try:
        r = get_sync_redis()
        r.publish(REDIS_CHANNEL, payload)
        if group_id:
            r.publish(f"{GROUP_CHANNEL_PREFIX}:{group_id}", payload)
        logger.info("SSE published seq=%s type=%s group=%s", seq, event.get('type'), group_id)
    except Exception:
        reset_sync_redis()
        logger.exception("Failed to publish SSE event seq=%s", seq)


# --- async subscribe/unsubscribe (called from async SSE view) ---

_subscribers: dict[str, dict[str, Any]] = {}
_lock = asyncio.Lock()


async def subscribe(user: Any = None) -> (
    tuple[str, asyncio.Queue[str], Callable[[], bool], Callable[[], None]]
):
    subscriber_id = str(uuid.uuid4())
    event_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)

    redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    pubsub = redis.pubsub()

    # Resolve channels off the event loop: `user.profile` is a lazy DB lookup and
    # running it here raises SynchronousOnlyOperation (previously swallowed,
    # which left every non-staff subscriber with an empty channel list).
    channels = await sync_to_async(_build_subscriber_channels)(user)
    if channels:
        await pubsub.subscribe(*channels)

    state: dict[str, Any] = {
        "queue": event_queue,
        "pubsub": pubsub,
        "redis": redis,
        "missed": False,
        "channels": channels,
    }

    async with _lock:
        _subscribers[subscriber_id] = state

    async def bridge():
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    try:
                        event_queue.put_nowait(message["data"])
                    except asyncio.QueueFull:
                        async with _lock:
                            sub = _subscribers.get(subscriber_id)
                            if sub:
                                sub["missed"] = True
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("SSE bridge error for %s", subscriber_id)

    task = asyncio.create_task(bridge())
    state["task"] = task

    def check_missed() -> bool:
        return state.get("missed", False)

    def clear_missed() -> None:
        state["missed"] = False

    return subscriber_id, event_queue, check_missed, clear_missed


async def unsubscribe(subscriber_id: str) -> None:
    async with _lock:
        state = _subscribers.pop(subscriber_id, None)
    if state:
        state["task"].cancel()
        try:
            await state["pubsub"].unsubscribe()
            await state["pubsub"].close()
            await state["redis"].aclose()
        except Exception:
            logger.exception("Error closing subscriber %s", subscriber_id)


def _build_subscriber_channels(user: Any) -> list[str]:
    """Determine Redis channels this user should subscribe to.

    IMPORTANT: this reads ``user.profile`` lazily, so it must be called through
    ``sync_to_async`` from the async SSE view — a direct call runs the ORM
    inside the event loop and raises ``SynchronousOnlyOperation``.
    """
    channels = []
    if (
        user
        and hasattr(user, 'is_authenticated')
        and user.is_authenticated
    ):
        if user.is_staff:
            channels = [REDIS_CHANNEL]
        else:
            try:
                profile = user.profile
            except ObjectDoesNotExist:
                # No profile row: the REST layer shows this user nothing.
                logger.warning(
                    "SSE: user=%s has no profile; subscribing to no channels",
                    getattr(user, 'username', None),
                )
            except Exception:
                logger.exception(
                    "SSE: could not resolve group for user=%s; subscribing to no channels",
                    getattr(user, 'username', None),
                )
            else:
                if profile and profile.group_id:
                    channels = [f"{GROUP_CHANNEL_PREFIX}:{profile.group_id}"]
                else:
                    # No group => unrestricted visibility (see
                    # ConversationViewSet.get_queryset), so receive everything.
                    channels = [REDIS_CHANNEL]
    else:
        channels = [REDIS_CHANNEL]
    return channels


def get_last_seq() -> int:
    return next(_next_seq) - 1
