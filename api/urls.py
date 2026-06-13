"""
URL configuration for API
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ConversationViewSet, MessageViewSet, UserViewSet, CityGroupViewSet, StickerAssetViewSet, BotExemptContactViewSet, BotScheduleViewSet, BotConfigViewSet, media_proxy, serve_media, static_map, issue_sse_token

router = DefaultRouter()
router.register(r'conversations', ConversationViewSet, basename='conversation')
router.register(r'messages', MessageViewSet, basename='message')
router.register(r'users', UserViewSet, basename='user')
router.register(r'city-groups', CityGroupViewSet, basename='city-group')
router.register(r'stickers', StickerAssetViewSet, basename='sticker')
router.register(r'bot-exempt', BotExemptContactViewSet, basename='bot-exempt')
router.register(r'bot-schedule', BotScheduleViewSet, basename='bot-schedule')
router.register(r'bot-config', BotConfigViewSet, basename='bot-config')

urlpatterns = [
    path('', include(router.urls)),
    path('sse-token/', issue_sse_token, name='issue-sse-token'),
    path('media-proxy/', media_proxy, name='media-proxy'),
    path('media/<path:path>', serve_media, name='serve-media'),
    path('static-map/', static_map, name='static-map'),
]
