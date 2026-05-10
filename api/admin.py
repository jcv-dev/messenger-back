"""
Admin configuration for API
"""
from django.contrib import admin
from .models import Conversation, Message, ConversationTag, ConversationNote, ConversationTake, StickerAsset


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ['contact_name', 'contact_phone', 'status', 'last_message_at']
    list_filter = ['status', 'created_at']
    search_fields = ['contact_name', 'contact_phone']


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ['conversation', 'direction', 'created_at', 'is_read']
    list_filter = ['direction', 'is_read', 'created_at']
    search_fields = ['content', 'conversation__contact_name']


@admin.register(ConversationTag)
class ConversationTagAdmin(admin.ModelAdmin):
    list_display = ['tag_name', 'conversation', 'created_by', 'expires_at', 'is_active']
    list_filter = ['is_active', 'expiry_type', 'created_at']
    search_fields = ['tag_name', 'conversation__contact_name', 'created_by__username']


@admin.register(ConversationNote)
class ConversationNoteAdmin(admin.ModelAdmin):
    list_display = ['conversation', 'created_by', 'expires_at', 'is_active', 'created_at']
    list_filter = ['is_active', 'expiry_type', 'created_at']
    search_fields = ['content', 'conversation__contact_name', 'created_by__username']


@admin.register(ConversationTake)
class ConversationTakeAdmin(admin.ModelAdmin):
    list_display = ['conversation', 'created_by', 'duration_minutes', 'expires_at', 'is_active']
    list_filter = ['is_active', 'created_at']
    search_fields = ['conversation__contact_name', 'created_by__username']


@admin.register(StickerAsset)
class StickerAssetAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_by', 'created_at']
    search_fields = ['name', 'created_by__username']
