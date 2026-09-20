"""
Admin configuration for API
"""
from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render, redirect
from django.urls import path
from django.conf import settings
from django.contrib import messages
from django.http import HttpResponseNotAllowed
from .models import Conversation, Message, ConversationTag, ConversationNote, ConversationTake, ConversationUserPin, StickerAsset, CityGroup, UserProfile, BotExemptContact, TemplateExclusion, Call, IntegrationApiKey, Order, OrderStop, BotConfig

import json
import urllib.request
import urllib.error


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ['contact_name', 'contact_phone', 'status', 'last_message_at']
    list_filter = ['status', 'created_at']
    search_fields = ['contact_name', 'contact_phone']


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ['conversation', 'direction', 'sender', 'created_at', 'is_read']
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


@admin.register(ConversationUserPin)
class ConversationUserPinAdmin(admin.ModelAdmin):
    list_display = ['user', 'conversation', 'pinned_at']
    list_filter = ['pinned_at']
    search_fields = ['user__username', 'conversation__contact_name']


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


@admin.register(BotExemptContact)
class BotExemptContactAdmin(admin.ModelAdmin):
    list_display = ['contact_phone', 'contact_name', 'source', 'ops_courier_id', 'created_by', 'created_at']
    list_filter = ['source']
    search_fields = ['contact_phone', 'contact_name']


@admin.register(BotConfig)
class BotConfigAdmin(admin.ModelAdmin):
    list_display = ['key', 'description', 'updated_at']
    search_fields = ['key', 'description']
    ordering = ['key']


@admin.register(IntegrationApiKey)
class IntegrationApiKeyAdmin(admin.ModelAdmin):
    list_display = ['name', 'prefix', 'is_active', 'last_used_at', 'created_at']
    list_filter = ['is_active']
    search_fields = ['name', 'prefix']
    readonly_fields = ['key_hash', 'prefix', 'last_used_at', 'created_at', 'updated_at']


class OrderStopInline(admin.TabularInline):
    model = OrderStop
    extra = 0
    fields = [
        'stop_no', 'ops_order_number', 'service_type', 'dest_address',
        'status', 'price', 'cancel_reason', 'last_synced_at',
    ]
    readonly_fields = ['last_synced_at']


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ['id', 'ops_batch_id', 'conversation', 'status', 'total', 'client_name', 'source', 'created_at']
    list_filter = ['status', 'source']
    search_fields = ['ops_batch_id', 'client_name', 'conversation__contact_name', 'conversation__contact_phone']
    readonly_fields = ['created_at', 'updated_at', 'last_synced_at']
    inlines = [OrderStopInline]


@admin.register(TemplateExclusion)
class TemplateExclusionAdmin(admin.ModelAdmin):
    list_display = ['contact_phone', 'contact_name', 'source', 'created_at']
    search_fields = ['contact_phone', 'contact_name']
    list_filter = ['source']


@admin.register(Call)
class CallAdmin(admin.ModelAdmin):
    list_display = [
        'call_id', 'conversation', 'direction', 'status',
        'recording_status', 'start_time', 'end_time',
        'duration_seconds', 'created_at',
    ]
    list_filter = ['direction', 'status', 'created_at']
    search_fields = ['call_id', 'conversation__contact_name', 'conversation__contact_phone']
    readonly_fields = [
        'call_id', 'conversation', 'direction', 'status',
        'from_number', 'to_number', 'recipient_bsuid',
        'start_time', 'end_time', 'duration_seconds',
        'biz_opaque_callback_data',
        'recording_status', 'recording_purpose',
        'recording_announcement_language',
        'recording_audio_id', 'recording_audio_url',
        'recording_audio_sha256', 'recording_audio_mime_type', 'recording_local_path',
        'sdp_offer', 'sdp_answer',
        'error_code', 'error_message',
        'created_at', 'updated_at',
    ]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@staff_member_required
def admin_call_settings(request):
    phone_number_id = settings.WHATSAPP_PHONE_NUMBER_ID
    token = settings.WHATSAPP_API_TOKEN
    base_url = f"https://graph.facebook.com/v20.0/{phone_number_id}/settings"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    if request.method == 'POST':
        calling_data = {}
        for field in ('status', 'call_icon_visibility', 'callback_permission_status'):
            val = request.POST.get(field)
            if val:
                calling_data[field] = val

        if calling_data:
            payload = {"calling": calling_data}
            body = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(base_url, data=body, headers=headers, method='POST')
            try:
                with urllib.request.urlopen(req) as resp:
                    resp_data = json.loads(resp.read().decode())
                if resp_data.get('success'):
                    messages.success(request, 'Call settings updated successfully.')
                else:
                    messages.warning(request, f"Meta API response: {resp_data}")
            except urllib.error.HTTPError as e:
                err_body = e.read().decode() if hasattr(e, 'read') else ''
                messages.error(request, f"HTTP {e.code}: {err_body[:500]}")
        return redirect('admin-call-settings')

    if request.method != 'GET':
        return HttpResponseNotAllowed(['GET', 'POST'])

    current_settings = {}
    api_error = None
    req = urllib.request.Request(base_url, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
        current_settings = data.get('calling', {})
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if hasattr(e, 'read') else ''
        api_error = f"HTTP {e.code}: {err_body[:500]}"
    except Exception as e:
        api_error = str(e)

    context = {
        **admin.site.each_context(request),
        'title': 'Call Settings',
        'current_settings': current_settings,
        'api_error': api_error,
        'phone_number_id': phone_number_id,
        'opts': {
            'app_label': 'api',
            'model_name': 'call',
            'verbose_name_plural': 'Call settings',
        },
    }
    return render(request, 'admin/call_settings.html', context)
