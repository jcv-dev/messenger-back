"""
URL configuration for WhatsApp Messenger
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.throttling import UserRateThrottle
from django.http import HttpResponse
from api.views import whatsapp_webhook, realtime_events

def health_check(request):
    return HttpResponse("ok")

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
]

if settings.DEBUG:
    urlpatterns.append(path('manage/', admin.site.urls))
