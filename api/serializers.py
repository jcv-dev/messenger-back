"""
Serializers for WhatsApp Messenger API
"""
import json
import re
import time
import urllib.parse
from rest_framework import serializers
from django.contrib.auth.models import User
from django.utils import timezone
from django.core.signing import Signer, BadSignature
from .models import Conversation, Message, ConversationTag, ConversationNote, ConversationTake, StickerAsset, CityGroup, BotExemptContact, BotSchedule, BotConfig, WhatsAppTemplate, Call, AuditLog, CannedResponse, AgentPresence, PushSubscription

media_signer = Signer(salt='domi-media')
media_proxy_signer = Signer(salt='domi-media-proxy')


def sign_media_url(url):
    """Sign a media URL so it can be served without exposing the auth token.
    Returns a signed URL like /api/media/<path>?sig=... for local files,
    or /api/media-proxy/?url=...&sig=... for WhatsApp CDN URLs.
    Signature includes a Unix timestamp — expires after 1 hour."""
    if not url:
        return url

    if url.startswith('/media/'):
        path = url[len('/media/'):]
        ts = int(time.time() / 60) * 60
        signed = media_signer.sign(f'{path}|{ts}')
        sig_val = signed.rsplit(':', 1)[1]
        return f'/api/media/{path}?sig={sig_val}&t={ts}'

    m = re.match(r'^https?://[^/]+/media/(.+)$', url)
    if m:
        path = m.group(1)
        ts = int(time.time() / 60) * 60
        signed = media_signer.sign(f'{path}|{ts}')
        sig_val = signed.rsplit(':', 1)[1]
        return f'/api/media/{path}?sig={sig_val}&t={ts}'

    # WhatsApp CDN URLs → signed media-proxy URL
    if 'lookaside.fbsbx.com' in url or 'media.whatsapp.net' in url:
        ts = int(time.time() / 60) * 60
        signed = media_proxy_signer.sign(f'{url}|{ts}')
        sig_val = signed.rsplit(media_proxy_signer.sep, 1)[1]
        encoded = urllib.parse.quote(url, safe='')
        return f'/api/media-proxy/?url={encoded}&sig={sig_val}&t={ts}'

    return url


class CityGroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = CityGroup
        fields = ['id', 'name', 'slug']


class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False)
    group = serializers.SerializerMethodField()
    group_id = serializers.IntegerField(write_only=True, required=False)

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'password', 'is_staff', 'group', 'group_id']

    def get_group(self, obj):
        try:
            profile = obj.profile
            if profile and profile.group:
                return CityGroupSerializer(profile.group).data
        except Exception:
            pass
        return None

    def create(self, validated_data):
        group_id = validated_data.pop('group_id', None)
        password = validated_data.pop('password', None)
        user = super().create(validated_data)
        if password:
            user.set_password(password)
            user.save()
        if group_id:
            try:
                profile = user.profile
                profile.group_id = group_id
                profile.save(update_fields=['group_id'])
            except Exception:
                pass
        return user

    def update(self, instance, validated_data):
        group_id = validated_data.pop('group_id', None)
        password = validated_data.pop('password', None)
        user = super().update(instance, validated_data)
        if password:
            user.set_password(password)
            user.save()
        if group_id is not None:
            try:
                profile = user.profile
                profile.group_id = group_id
                profile.save(update_fields=['group_id'])
            except Exception:
                pass
        return user


class ConversationTagSerializer(serializers.ModelSerializer):
    created_by = UserSerializer(read_only=True)
    is_expired = serializers.SerializerMethodField()
    time_remaining = serializers.SerializerMethodField()

    class Meta:
        model = ConversationTag
        fields = [
            'id', 'tag_name', 'tag_color', 'expiry_type', 'expires_at',
            'created_at', 'created_by',
            'is_expired', 'time_remaining'
        ]

    def get_is_expired(self, obj):
        return obj.is_expired()

    def get_time_remaining(self, obj):
        """Return time remaining in seconds"""
        if obj.expires_at is None:
            return None
        remaining = obj.expires_at - timezone.now()
        if remaining.total_seconds() < 0:
            return 0
        return int(remaining.total_seconds())


class ConversationNoteSerializer(serializers.ModelSerializer):
    created_by = UserSerializer(read_only=True)
    is_expired = serializers.SerializerMethodField()
    time_remaining = serializers.SerializerMethodField()

    class Meta:
        model = ConversationNote
        fields = [
            'id', 'content', 'expiry_type', 'expires_at', 'created_at',
            'created_by',
            'is_expired', 'time_remaining'
        ]

    def get_is_expired(self, obj):
        return obj.is_expired()

    def get_time_remaining(self, obj):
        if obj.expires_at is None:
            return None
        remaining = obj.expires_at - timezone.now()
        if remaining.total_seconds() < 0:
            return 0
        return int(remaining.total_seconds())


class ConversationTakeSerializer(serializers.ModelSerializer):
    created_by = UserSerializer(read_only=True)
    is_expired = serializers.SerializerMethodField()
    time_remaining = serializers.SerializerMethodField()

    class Meta:
        model = ConversationTake
        fields = [
            'id', 'duration_minutes', 'expires_at', 'created_at',
            'created_by',
            'is_expired', 'time_remaining'
        ]

    def get_is_expired(self, obj):
        return obj.is_expired()

    def get_time_remaining(self, obj):
        remaining = obj.expires_at - timezone.now()
        if remaining.total_seconds() < 0:
            return 0
        return int(remaining.total_seconds())


class MessageSerializer(serializers.ModelSerializer):
    context_message_preview = serializers.SerializerMethodField()
    context_message_id = serializers.SerializerMethodField()
    media_url = serializers.SerializerMethodField()
    sender_detail = serializers.SerializerMethodField()
    content_display = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            'id', 'direction', 'message_type', 'content', 'content_display',
            'sender_name', 'sender', 'sender_detail',
            'whatsapp_message_id', 'media_url', 'metadata', 'created_at',
            'is_read', 'context_message_id', 'context_message_preview',
        ]

    def get_context_message_id(self, obj):
        return obj.context_message_id

    def get_context_message_preview(self, obj):
        if not obj.context_message_id:
            return None
        try:
            cm = obj.context_message
            return {
                'id': cm.id,
                'content': cm.content,
                'message_type': cm.message_type,
                'sender_name': cm.sender_name,
                'media_url': sign_media_url(cm.media_url),
                'created_at': cm.created_at,
            }
        except Exception:
            return None

    def get_media_url(self, obj):
        return sign_media_url(obj.media_url)

    def get_sender_detail(self, obj):
        if not obj.sender_id:
            return None
        return {
            'id': obj.sender_id,
            'first_name': obj.sender.first_name,
            'username': obj.sender.username,
        }

    def get_content_display(self, obj):
        if obj.message_type != 'template':
            return None
        try:
            payload = json.loads(obj.content)
        except (json.JSONDecodeError, TypeError):
            return None

        name = payload.get('name', '')
        language = payload.get('language', {}).get('code', 'es')
        components = payload.get('components', [])

        params = {}
        for comp in components:
            if comp.get('type') == 'body':
                for p in comp.get('parameters', []):
                    if p.get('type') == 'text':
                        params[p.get('parameter_name', '')] = p.get('text', '')

        try:
            template = WhatsAppTemplate.objects.get(name=name, language=language)
            body_text = ''
            for comp in template.components:
                if comp.get('type') == 'body':
                    body_text = comp.get('text', '')
                    break
            if body_text:
                rendered = re.sub(
                    r'\{\{(\w+)\}\}',
                    lambda m: params.get(m.group(1), m.group(0)),
                    body_text,
                )
                return rendered
        except Exception:
            pass

        return f'[Plantilla: {name}]'


class ConversationSerializer(serializers.ModelSerializer):
    tags = ConversationTagSerializer(many=True, read_only=True)
    notes = ConversationNoteSerializer(many=True, read_only=True)
    active_notes = serializers.SerializerMethodField()
    active_take = serializers.SerializerMethodField()
    active_tags = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()
    group = CityGroupSerializer(read_only=True)
    is_pinned_by_me = serializers.SerializerMethodField()
    pinned_by_me_at = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = [
            'id', 'whatsapp_id', 'contact_name', 'contact_phone', 'whatsapp_username', 'custom_name', 'last_message',
            'last_message_at', 'status', 'resolved_by_bot', 'tags', 'active_tags', 'notes',
            'active_notes', 'active_take', 'unread_count', 'group',
            'is_pinned', 'pinned_at',
            'is_pinned_by_me', 'pinned_by_me_at',
            'created_at', 'updated_at'
        ]

    def get_is_pinned_by_me(self, obj):
        return obj._has_user_pin if hasattr(obj, '_has_user_pin') else False

    def get_pinned_by_me_at(self, obj):
        return getattr(obj, '_user_pin_at', None)

    def get_unread_count(self, obj):
        if hasattr(obj, '_unread_count') and obj._unread_count is not None:
            return obj._unread_count
        return obj.messages.filter(is_read=False, direction='inbound').count()

    def get_active_tags(self, obj):
        now = timezone.now()
        tags = obj.tags.all()
        filtered = [t for t in tags if t.expires_at is None or t.expires_at > now]
        return ConversationTagSerializer(filtered, many=True).data

    def get_active_notes(self, obj):
        now = timezone.now()
        notes = obj.notes.all()
        filtered = [n for n in notes if n.expires_at is None or n.expires_at > now]
        return ConversationNoteSerializer(filtered, many=True).data

    def get_active_take(self, obj):
        now = timezone.now()
        takes = [t for t in obj.takes.all() if t.expires_at > now]
        take = takes[0] if takes else None
        return ConversationTakeSerializer(take).data if take else None


class ConversationListSerializer(serializers.ModelSerializer):
    """Lighter version for list views"""
    active_tags = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()
    active_take = serializers.SerializerMethodField()
    last_message_sender = serializers.SerializerMethodField()
    last_message_direction = serializers.SerializerMethodField()
    group = CityGroupSerializer(read_only=True)
    is_pinned_by_me = serializers.SerializerMethodField()
    pinned_by_me_at = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = [
            'id', 'whatsapp_id', 'contact_name', 'contact_phone', 'whatsapp_username', 'custom_name', 'last_message',
            'last_message_at', 'status', 'resolved_by_bot', 'active_tags', 'active_take', 'unread_count', 'created_at',
            'last_message_sender', 'last_message_direction', 'group',
            'is_pinned', 'pinned_at',
            'is_pinned_by_me', 'pinned_by_me_at',
        ]

    def get_is_pinned_by_me(self, obj):
        return obj._has_user_pin if hasattr(obj, '_has_user_pin') else False

    def get_pinned_by_me_at(self, obj):
        return getattr(obj, '_user_pin_at', None)

    def get_active_take(self, obj):
        now = timezone.now()
        takes = [t for t in obj.takes.all() if t.expires_at > now]
        take = takes[0] if takes else None
        return ConversationTakeSerializer(take).data if take else None

    def get_active_tags(self, obj):
        now = timezone.now()
        tags = obj.tags.all()
        filtered = [t for t in tags if t.expires_at is None or t.expires_at > now]
        return ConversationTagSerializer(filtered, many=True).data

    def get_unread_count(self, obj):
        if hasattr(obj, '_unread_count') and obj._unread_count is not None:
            return obj._unread_count
        return obj.messages.filter(is_read=False, direction='inbound').count()

    def get_last_message_sender(self, obj):
        if hasattr(obj, '_last_msg_sender'):
            return obj._last_msg_sender
        return None

    def get_last_message_direction(self, obj):
        if hasattr(obj, '_last_msg_direction'):
            return obj._last_msg_direction
        return None


class CreateConversationTagSerializer(serializers.Serializer):
    """Serializer for creating tags with expiry calculation"""
    tag_name = serializers.CharField(max_length=255)
    expiry_type = serializers.ChoiceField(choices=['1h', '5h', 'end_of_day', 'never', 'custom'])
    custom_expiry_minutes = serializers.IntegerField(required=False, allow_null=True)
    tag_color = serializers.CharField(max_length=20, default='blue')

    def validate(self, data):
        if data.get('expiry_type') == 'custom' and not data.get('custom_expiry_minutes'):
            raise serializers.ValidationError("custom_expiry_minutes is required for custom expiry type")
        return data


class CreateConversationNoteSerializer(serializers.Serializer):
    content = serializers.CharField(max_length=5000)
    expiry_type = serializers.ChoiceField(choices=['1h', '5h', 'end_of_day', 'never', 'custom'])
    custom_expiry_minutes = serializers.IntegerField(required=False, allow_null=True)

    def validate(self, data):
        if data.get('expiry_type') == 'custom' and not data.get('custom_expiry_minutes'):
            raise serializers.ValidationError("custom_expiry_minutes is required for custom expiry type")
        return data


class TakeConversationSerializer(serializers.Serializer):
    duration_minutes = serializers.IntegerField(required=False, default=10, min_value=1)


class InitiateConversationSerializer(serializers.Serializer):
    contact_phone = serializers.CharField(max_length=20)
    contact_name = serializers.CharField(max_length=255)
    content = serializers.CharField(max_length=5000)
    message_type = serializers.ChoiceField(
        choices=['text', 'image', 'video', 'audio', 'document', 'sticker'],
        default='text',
        required=False
    )

    def validate_contact_phone(self, value):
        cleaned = value.strip().lstrip('+')
        if not cleaned:
            raise serializers.ValidationError("Phone number is required")
        return cleaned


class SetGroupSerializer(serializers.Serializer):
    group_id = serializers.IntegerField()

    def validate_group_id(self, value):
        from .models import CityGroup
        if not CityGroup.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError("Group does not exist or is inactive.")
        return value


class BotExemptContactSerializer(serializers.ModelSerializer):
    created_by = UserSerializer(read_only=True)

    class Meta:
        model = BotExemptContact
        fields = ['id', 'contact_phone', 'contact_name', 'created_by', 'created_at']
        read_only_fields = ['id', 'created_by', 'created_at']

    def validate_contact_phone(self, value):
        cleaned = re.sub(r'\D', '', value)
        if not cleaned.startswith('57'):
            raise serializers.ValidationError("Phone number must start with 57 (Colombia).")
        if len(cleaned) < 10:
            raise serializers.ValidationError("Phone number must be at least 10 digits.")
        return cleaned


class StickerAssetSerializer(serializers.ModelSerializer):
    created_by = UserSerializer(read_only=True)

    class Meta:
        model = StickerAsset
        fields = ['id', 'name', 'image', 'created_by', 'created_at']
        extra_kwargs = {'name': {'required': False}}

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if data.get('image'):
            data['image'] = sign_media_url(data['image'])
        return data


class BotScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = BotSchedule
        fields = ['id', 'day_of_week', 'date', 'open_time', 'close_time', 'is_active', 'label']


class BotConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = BotConfig
        fields = ['id', 'key', 'value', 'description', 'updated_at']


class WhatsAppTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = WhatsAppTemplate
        fields = [
            'id', 'name', 'language', 'category', 'template_id', 'status',
            'quality_score', 'components', 'rejection_reason',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'template_id', 'status', 'quality_score',
                            'rejection_reason', 'created_at', 'updated_at']

    def validate_components(self, value):
        for comp in value:
            if comp.get('type') == 'body':
                text = comp.get('text', '')
                variables = re.findall(r'\{\{(\w+)\}\}', text)
                if variables:
                    params = comp.get('parameters', [])
                    for var in variables:
                        p = next((p for p in params if p.get('name') == var), None)
                        if not p or not p.get('example'):
                            raise serializers.ValidationError(
                                f"El parámetro '{{{{{var}}}}}' necesita un valor de ejemplo."
                            )
        return value


class AuditLogSerializer(serializers.ModelSerializer):
    actor_username = serializers.CharField(source='actor.username', read_only=True)
    action_display = serializers.CharField(source='get_action_display', read_only=True)

    class Meta:
        model = AuditLog
        fields = ['id', 'actor', 'actor_username', 'conversation', 'action', 'action_display', 'detail', 'created_at']


class CannedResponseSerializer(serializers.ModelSerializer):
    group_name = serializers.CharField(source='group.name', read_only=True)
    created_by_username = serializers.CharField(source='created_by.username', read_only=True)

    class Meta:
        model = CannedResponse
        fields = ['id', 'title', 'content', 'category', 'group', 'group_name', 'created_by', 'created_by_username', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at']


class SendTemplateSerializer(serializers.Serializer):
    template_id = serializers.IntegerField()
    parameters = serializers.JSONField(required=False, default=dict)

    def validate_template_id(self, value):
        try:
            template = WhatsAppTemplate.objects.get(id=value)
        except WhatsAppTemplate.DoesNotExist:
            raise serializers.ValidationError("Template not found")
        if template.status != 'APPROVED':
            raise serializers.ValidationError("Template must be APPROVED")
        return value


PARAMETER_SOURCE_CHOICES = ['contact_name', 'custom_name', 'contact_phone', 'conversation_id', 'fixed']


class BulkSendTemplateSerializer(serializers.Serializer):
    template_id = serializers.IntegerField()
    count = serializers.IntegerField(min_value=1, max_value=1000, required=False)
    recipients = serializers.ListField(required=False)
    parameter_sources = serializers.JSONField(required=False, default=dict)
    fixed_values = serializers.JSONField(required=False, default=dict)

    def validate_template_id(self, value):
        try:
            template = WhatsAppTemplate.objects.get(id=value)
        except WhatsAppTemplate.DoesNotExist:
            raise serializers.ValidationError("Template not found")
        if template.status != 'APPROVED':
            raise serializers.ValidationError("Template must be APPROVED")
        return value

    def validate_recipients(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Must be a list")
        if len(value) > 1000:
            raise serializers.ValidationError("Maximum 1000 recipients")
        errors = []
        for i, row in enumerate(value):
            row_errors = {}
            if not isinstance(row, dict):
                row_errors['_'] = 'Must be an object'
            else:
                phone = row.get('phone', '')
                if not phone or not isinstance(phone, str) or not phone.strip():
                    row_errors['phone'] = 'Phone is required'
                if 'parameters' in row and not isinstance(row['parameters'], dict):
                    row_errors['parameters'] = 'Must be an object'
            if row_errors:
                errors.append({str(i): row_errors})
        if errors:
            raise serializers.ValidationError(errors)
        return value

    def validate(self, data):
        has_count = 'count' in data
        has_recipients = 'recipients' in data and data['recipients']
        if has_count and has_recipients:
            raise serializers.ValidationError("Cannot provide both 'count' and 'recipients'")
        if not has_count and not has_recipients:
            data['count'] = 10
        return data

    def validate_parameter_sources(self, value):
        for var_name, source in value.items():
            if source not in PARAMETER_SOURCE_CHOICES:
                raise serializers.ValidationError(
                    f"Invalid source '{source}' for '{var_name}'. "
                    f"Choices: {', '.join(PARAMETER_SOURCE_CHOICES)}"
                )
        return value


class CallSerializer(serializers.ModelSerializer):
    contact_name = serializers.SerializerMethodField()

    class Meta:
        model = Call
        fields = '__all__'

    def get_contact_name(self, obj):
        return obj.conversation.contact_name


class AgentPresenceSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)
    first_name = serializers.CharField(source='user.first_name', read_only=True)

    class Meta:
        model = AgentPresence
        fields = ['id', 'user', 'username', 'first_name', 'status', 'last_seen', 'heartbeat_interval']


class PushSubscriptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PushSubscription
        fields = ['id', 'endpoint', 'p256dh', 'auth', 'browser', 'created_at']
        read_only_fields = ['id', 'created_at']


class MessageSearchSerializer(serializers.Serializer):
    message_id = serializers.IntegerField()
    conversation_id = serializers.IntegerField()
    contact_name = serializers.CharField()
    snippet = serializers.CharField()
    created_at = serializers.DateTimeField()
    rank = serializers.FloatField()
