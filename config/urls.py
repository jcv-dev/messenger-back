"""
URL configuration for WhatsApp Messenger
"""
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
    from api.bot.metrics import snapshot, reset
    from api.bot.lock import _LOCK_TTL
    from api.bot.limits import _WINDOW
    from django.db import connection
    metrics = snapshot()
    return JsonResponse({
        "healthy": True,
        "metrics": metrics,
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
