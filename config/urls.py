"""
URL configuration for WhatsApp Messenger
"""
import logging

from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.throttling import UserRateThrottle
from django.http import HttpResponse, JsonResponse
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.authentication import TokenAuthentication
from rest_framework.permissions import IsAdminUser
from api.views import whatsapp_webhook, realtime_events

def health_check(request):
    return HttpResponse("ok")

@api_view(['GET'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAdminUser])
def bot_status(request):
    import time as _time
    from api.bot.lock import _LOCK_TTL
    from api.bot.limits import _WINDOW
    from api.redis_client import get_sync_redis

    METRIC_NAMES = [
        "messages.processed",
        "messages.rate_limited",
        "locks.acquired",
        "locks.failed",
        "llm.calls",
        "llm.retries",
        "llm.failures",
        "tool_calls.succeeded",
        "tool_calls.failed",
        "cancellations",
        "escalations",
        "loop.crashes",
    ]

    now = int(_time.time())
    start = now - 86400
    first_minute = start // 60 * 60
    last_minute = now // 60 * 60
    bucket_count = (last_minute - first_minute) // 60 + 1
    if bucket_count < 1:
        bucket_count = 1

    metrics = {}
    series = {}

    try:
        r = get_sync_redis()
        pipe = r.pipeline()
        for name in METRIC_NAMES:
            pipe.hgetall(f"bot:metrics:{name}")
        results = pipe.execute()
    except Exception:
        logging.getLogger(__name__).exception("Redis error reading bot metrics")
        results = []

    if results:
        for name, raw in zip(METRIC_NAMES, results):
            name_series = [0] * bucket_count
            total = 0
            if raw:
                for ts_str, count_str in raw.items():
                    ts = int(ts_str)
                    if ts >= first_minute:
                        idx = (ts - first_minute) // 60
                        if 0 <= idx < bucket_count:
                            count = int(count_str)
                            name_series[idx] = count
                            total += count
            series[name] = name_series
            metrics[name] = total
    else:
        for name in METRIC_NAMES:
            series[name] = [0] * bucket_count
            metrics[name] = 0

    return JsonResponse({
        "healthy": True,
        "metrics": metrics,
        "series": series,
        "from_ts": first_minute,
        "to_ts": last_minute,
        "bucket_count": bucket_count,
        "config": {
            "lock_ttl": _LOCK_TTL,
            "rate_window": _WINDOW,
            "rate_threshold": getattr(settings, "BOT_INBOUND_RATE_LIMIT", 10),
            "llm_temperature": getattr(settings, "BOT_LLM_TEMPERATURE", 0.25),
            "llm_max_tokens": getattr(settings, "BOT_LLM_MAX_OUTPUT_TOKENS", 1024),
            "llm_retry_count": getattr(settings, "BOT_LLM_RETRY_COUNT", 3),
            "tools_cache_ttl": getattr(settings, "BOT_TOOLS_CACHE_TTL", 300),
            "max_user_message_length": getattr(settings, "BOT_MAX_USER_MESSAGE_LENGTH", 1000),
        },
    })


class LoginRateThrottle(UserRateThrottle):
    rate = '5/min'
    scope = 'login'

class ThrottledObtainAuthToken(ObtainAuthToken):
    throttle_classes = [LoginRateThrottle]

urlpatterns = [
    path('api-auth/', ThrottledObtainAuthToken.as_view(), name='api_token_auth'),
    path('api/', include('api.urls')),
    path('api/events/', realtime_events, name='realtime_events'),
    path('webhook/', whatsapp_webhook),
    path('health', health_check, name='health_check'),
    path('api/bot/status/', bot_status, name='bot-status'),
]

if settings.DEBUG:
    urlpatterns.append(path('manage/', admin.site.urls))
