"""
URL configuration for WhatsApp Messenger
"""
from django.contrib import admin
from django.conf import settings
from django.urls import path, include
from django.conf.urls.static import static
from rest_framework.authtoken.views import obtain_auth_token
from api.views import whatsapp_webhook, realtime_events

urlpatterns = [
    path('manage/', admin.site.urls),
    path('api-auth/', obtain_auth_token, name='api_token_auth'),
    path('api/', include('api.urls')),
    path('api/events/', realtime_events, name='realtime_events'),
    path('webhook/', whatsapp_webhook),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
