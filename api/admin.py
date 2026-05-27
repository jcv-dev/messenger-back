"""
Admin configuration for API
"""
from django.contrib import admin
from .models import Conversation, Message, ConversationTag, ConversationNote, ConversationTake, StickerAsset, CityGroup, UserProfile


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
    list_display = ['tag_name', 'conversation', 'created_by', 'expires_at']
    list_filter = ['expiry_type', 'created_at']
    search_fields = ['tag_name', 'conversation__contact_name', 'created_by__username']


@admin.register(ConversationNote)
class ConversationNoteAdmin(admin.ModelAdmin):
    list_display = ['conversation', 'created_by', 'expires_at', 'created_at']
    list_filter = ['expiry_type', 'created_at']
    search_fields = ['content', 'conversation__contact_name', 'created_by__username']


@admin.register(ConversationTake)
class ConversationTakeAdmin(admin.ModelAdmin):
    list_display = ['conversation', 'created_by', 'duration_minutes', 'expires_at']
    list_filter = ['created_at']
    search_fields = ['conversation__contact_name', 'created_by__username']


@admin.register(StickerAsset)
class StickerAssetAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_by', 'created_at']
    search_fields = ['name', 'created_by__username']


@admin.register(CityGroup)
class CityGroupAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'is_active', 'created_at']
    prepopulated_fields = {'slug': ('name',)}


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'group']
    list_filter = ['group']
    search_fields = ['user__username', 'user__email']
