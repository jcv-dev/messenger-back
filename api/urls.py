"""
URL configuration for API
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ConversationViewSet, MessageViewSet, UserViewSet, CityGroupViewSet, StickerAssetViewSet, BotExemptContactViewSet, BotScheduleViewSet, BotConfigViewSet, WhatsAppTemplateViewSet, TemplateExclusionViewSet, AuditLogViewSet, CannedResponseViewSet, media_proxy, serve_media, static_map, issue_sse_token, call_answer, call_reject, call_terminate, call_initiate, call_list, call_active, call_turn_config, call_settings, call_recordings, presence_heartbeat, presence_list, push_subscribe, push_unsubscribe, export_conversations_csv, agent_stats
from .order_views import (
    order_cancel,
    order_client_addresses,
    order_client_default_address,
    order_client_orders,
    order_client_search,
    order_couriers,
    order_detail,
    order_geocode_details,
    order_geocode_search,
    order_quote,
    order_refresh,
    order_services,
    order_stop_cancel,
    order_tools,
)

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
router.register(r'template-exclusions', TemplateExclusionViewSet, basename='template-exclusion')
router.register(r'audit-logs', AuditLogViewSet, basename='audit-log')
router.register(r'canned-responses', CannedResponseViewSet, basename='canned-response')

urlpatterns = [
    path('conversations/export/', export_conversations_csv, name='conversations-export'),
    path('orders/services/', order_services, name='order-services'),
    path('orders/tools/', order_tools, name='order-tools'),
    path('orders/geocode/search/', order_geocode_search, name='order-geocode-search'),
    path('orders/geocode/details/', order_geocode_details, name='order-geocode-details'),
    path('orders/quote/', order_quote, name='order-quote'),
    path('orders/clients/', order_client_search, name='order-client-search'),
    path('orders/clients/<int:client_id>/addresses/', order_client_addresses, name='order-client-addresses'),
    path(
        'orders/clients/<int:client_id>/addresses/default/',
        order_client_default_address,
        name='order-client-addresses-default',
    ),
    path('orders/clients/<int:client_id>/orders/', order_client_orders, name='order-client-orders'),
    path('orders/couriers/', order_couriers, name='order-couriers'),
    path('orders/<int:order_id>/', order_detail, name='order-detail'),
    path('orders/<int:order_id>/refresh/', order_refresh, name='order-refresh'),
    path('orders/<int:order_id>/cancel/', order_cancel, name='order-cancel'),
    path('orders/<int:order_id>/stops/<int:stop_id>/cancel/', order_stop_cancel, name='order-stop-cancel'),
    path('integrations/', include('api.integrations.urls')),
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
    path('calls/recordings/', call_recordings, name='call-recordings'),
    path('presence/heartbeat/', presence_heartbeat, name='presence-heartbeat'),
    path('presence/', presence_list, name='presence-list'),
    path('agent/stats/', agent_stats, name='agent-stats'),
    path('push-subscribe/', push_subscribe, name='push-subscribe'),
    path('push-unsubscribe/', push_unsubscribe, name='push-unsubscribe'),
]
