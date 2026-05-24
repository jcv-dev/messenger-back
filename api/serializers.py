"""
Serializers for WhatsApp Messenger API
"""
from rest_framework import serializers
from django.contrib.auth.models import User
from .models import Conversation, Message, ConversationTag, ConversationNote, ConversationTake, StickerAsset


class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'password', 'is_staff']
        
    def create(self, validated_data):
        password = validated_data.pop('password', None)
        user = super().create(validated_data)
        if password:
            user.set_password(password)
            user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        user = super().update(instance, validated_data)
        if password:
            user.set_password(password)
            user.save()
        return user


class ConversationTagSerializer(serializers.ModelSerializer):
    created_by = UserSerializer(read_only=True)
    is_expired = serializers.SerializerMethodField()
    time_remaining = serializers.SerializerMethodField()

    class Meta:
        model = ConversationTag
        fields = [
            'id', 'tag_name', 'tag_color', 'expiry_type', 'expires_at',
            'created_at', 'created_by', 'is_active',
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
            'created_by', 'is_active', 'is_expired', 'time_remaining'
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
            'created_by', 'is_active', 'is_expired', 'time_remaining'
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

    class Meta:
        model = Message
        fields = [
            'id', 'direction', 'message_type', 'content', 'sender_name',
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
                'media_url': cm.media_url,
                'created_at': cm.created_at,
            }
        except Exception:
            return None


class ConversationSerializer(serializers.ModelSerializer):
    tags = ConversationTagSerializer(many=True, read_only=True)
    notes = ConversationNoteSerializer(many=True, read_only=True)
    active_notes = serializers.SerializerMethodField()
    active_take = serializers.SerializerMethodField()
    active_tags = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = [
            'id', 'whatsapp_id', 'contact_name', 'contact_phone', 'whatsapp_username', 'custom_name', 'last_message',
            'last_message_at', 'status', 'tags', 'active_tags', 'notes',
            'active_notes', 'active_take', 'unread_count',
            'created_at', 'updated_at'
        ]

    def get_unread_count(self, obj):
        if hasattr(obj, '_unread_count') and obj._unread_count is not None:
            return obj._unread_count
        return obj.messages.filter(is_read=False, direction='inbound').count()

    def get_active_tags(self, obj):
        return ConversationTagSerializer(obj.tags.all(), many=True).data

    def get_active_notes(self, obj):
        return ConversationNoteSerializer(obj.notes.all(), many=True).data

    def get_active_take(self, obj):
        take = obj.takes.all().first()
        return ConversationTakeSerializer(take).data if take else None


class ConversationListSerializer(serializers.ModelSerializer):
    """Lighter version for list views"""
    active_tags = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()
    active_take = serializers.SerializerMethodField()
    last_message_sender = serializers.SerializerMethodField()
    last_message_direction = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = [
            'id', 'whatsapp_id', 'contact_name', 'contact_phone', 'whatsapp_username', 'custom_name', 'last_message',
            'last_message_at', 'status', 'active_tags', 'active_take', 'unread_count', 'created_at',
            'last_message_sender', 'last_message_direction',
        ]

    def get_active_take(self, obj):
        take = obj.takes.all().first()
        return ConversationTakeSerializer(take).data if take else None

    def get_active_tags(self, obj):
        return ConversationTagSerializer(obj.tags.all(), many=True).data

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
    duration_minutes = serializers.IntegerField(required=False, default=30, min_value=1)


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


class StickerAssetSerializer(serializers.ModelSerializer):
    created_by = UserSerializer(read_only=True)

    class Meta:
        model = StickerAsset
        fields = ['id', 'name', 'image', 'created_by', 'created_at']
