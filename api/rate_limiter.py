"""Redis-based sliding window rate limiter for WhatsApp API (cross-worker safe)."""

import time
import logging

logger = logging.getLogger(__name__)

from .redis_client import get_sync_redis


def acquire(phone_number_id, threshold=70, max_wait=30):
    """Block until capacity is available under the rate limit threshold.

    Uses a Redis sliding-window counter keyed by ``wa_rate_limit:{id}:{unix_sec}``
    with a 2-second TTL.  ``INCR`` is atomic across all uvicorn workers so the
    combined rate is enforced correctly.

    Fails open after *max_wait* seconds (logs a warning but lets the request
    through) so a Redis outage or sustained traffic spike never permanently
    blocks outbound messages.
    """
    redis = get_sync_redis()
    start = time.time()

    while True:
        now = int(time.time())
        key = f"wa_rate_limit:{phone_number_id}:{now}"

        try:
            count = redis.incr(key)
            redis.expire(key, 2)

            if count <= threshold:
                return

            redis.decr(key)

            elapsed = time.time() - start
            if elapsed > max_wait:
                logger.warning(
                    "Rate limit acquire timed out after %.1fs (id=%s, threshold=%s) — proceeding",
                    elapsed, phone_number_id, threshold,
                )
                return

            time.sleep(0.05)

        except Exception:
            logger.exception("Rate limiter Redis error — proceeding")
            return
