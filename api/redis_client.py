"""Shared synchronous Redis client for all api modules.

All callers share one connection pool by calling ``get_sync_redis()``.
Use ``reset_sync_redis()`` to force reconnection on error.
"""

from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

_client = None


def get_sync_redis():
    global _client
    if _client is None:
        import redis as sync_redis
        _client = sync_redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _client


def reset_sync_redis():
    global _client
    _client = None
