"""
URL configuration for API
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ConversationViewSet, MessageViewSet, UserViewSet, StickerAssetViewSet, media_proxy, static_map

router = DefaultRouter()
router.register(r'conversations', ConversationViewSet, basename='conversation')
router.register(r'messages', MessageViewSet, basename='message')
router.register(r'users', UserViewSet, basename='user')
router.register(r'stickers', StickerAssetViewSet, basename='sticker')

urlpatterns = [
    path('', include(router.urls)),
    path('media-proxy/', media_proxy, name='media-proxy'),
    path('static-map/', static_map, name='static-map'),
]
