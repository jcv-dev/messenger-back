"""
URL configuration for API
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ConversationViewSet, MessageViewSet, UserViewSet, CityGroupViewSet, StickerAssetViewSet, BotExemptContactViewSet, BotScheduleViewSet, BotConfigViewSet, WhatsAppTemplateViewSet, media_proxy, serve_media, static_map, issue_sse_token, call_answer, call_reject, call_terminate, call_initiate, call_list, call_active, call_turn_config, call_settings

router = DefaultRouter()
router.register(r'conversations', ConversationViewSet, basename='conversation')
router.register(r'messages', MessageViewSet, basename='message')
router.register(r'users', UserViewSet, basename='user')
router.register(r'city-groups', CityGroupViewSet, basename='city-group')
router.register(r'stickers', StickerAssetViewSet, basename='sticker')
router.register(r'bot-exempt', BotExemptContactViewSet, basename='bot-exempt')
router.register(r'bot-schedule', BotScheduleViewSet, basename='bot-schedule')
router.register(r'bot-config', BotConfigViewSet, basename='bot-config')
router.register(r'templates', WhatsAppTemplateViewSet, basename='template')

urlpatterns = [
    path('', include(router.urls)),
    path('sse-token/', issue_sse_token, name='issue-sse-token'),
    path('media-proxy/', media_proxy, name='media-proxy'),
    path('media/<path:path>', serve_media, name='serve-media'),
    path('static-map/', static_map, name='static-map'),
    path('calls/answer/', call_answer, name='call-answer'),
    path('calls/reject/', call_reject, name='call-reject'),
    path('calls/terminate/', call_terminate, name='call-terminate'),
    path('calls/initiate/', call_initiate, name='call-initiate'),
    path('calls/list/', call_list, name='call-list'),
    path('calls/active/', call_active, name='call-active'),
    path('calls/turn-config/', call_turn_config, name='call-turn-config'),
    path('calls/settings/', call_settings, name='call-settings'),
]
