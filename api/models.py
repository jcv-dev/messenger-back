"""
Models for WhatsApp Messenger API
"""
from django.db import models
from django.db.models import Q
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from datetime import timedelta


EXPIRY_CHOICES = [
    ('1h', '1 Hour'),
    ('5h', '5 Hours'),
    ('end_of_day', 'End of Day'),
    ('never', 'Never'),
    ('custom', 'Custom'),
]


def get_expiry_datetime(expiry_type, custom_minutes=None):
    now = timezone.now()

    if expiry_type == '1h':
        return now + timedelta(hours=1)
    if expiry_type == '5h':
        return now + timedelta(hours=5)
    if expiry_type == 'end_of_day':
        return (now + timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)
    if expiry_type == 'never':
        return None
    if expiry_type == 'custom':
        minutes = custom_minutes or 30
        return now + timedelta(minutes=minutes)
    return now + timedelta(hours=1)


class CityGroup(models.Model):
    """City/group for routing conversations to agents."""
    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(max_length=255, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['name']
        verbose_name = "City Group"
        verbose_name_plural = "City Groups"


class UserProfile(models.Model):
    """Profile extending User with city group assignment."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    group = models.ForeignKey(
        CityGroup, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='members',
    )

    def __str__(self):
        return f"{self.user.username} → {self.group.name if self.group else 'No group'}"


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(user=instance)


class Conversation(models.Model):
    """WhatsApp conversation/chat"""
    whatsapp_id = models.CharField(max_length=255, unique=True)
    contact_name = models.CharField(max_length=255)
    contact_phone = models.CharField(max_length=20, null=True, blank=True)
    whatsapp_username = models.CharField(max_length=255, null=True, blank=True)
    custom_name = models.CharField(max_length=255, null=True, blank=True)
    last_message = models.TextField(blank=True)
    last_message_at = models.DateTimeField(null=True, blank=True)
    group = models.ForeignKey(
        CityGroup, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='conversations',
    )
    status = models.CharField(
        max_length=20,
        choices=[('active', 'Active'), ('resolved', 'Resolved'), ('archived', 'Archived')],
        default='active'
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        contact_id = self.contact_phone or self.whatsapp_username or self.whatsapp_id
        return f"{self.contact_name} ({contact_id})"

    class Meta:
        ordering = ['-last_message_at', '-created_at']
        indexes = [
            models.Index(fields=['status', 'last_message_at'], name='conv_status_lastmsg_idx'),
            models.Index(fields=['whatsapp_id'], name='conv_waid_idx'),
        ]


class Message(models.Model):
    """Messages within a conversation"""
    DIRECTION_CHOICES = [
        ('inbound', 'Inbound'),
        ('outbound', 'Outbound'),
    ]

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages')
    direction = models.CharField(max_length=20, choices=DIRECTION_CHOICES)
    message_type = models.CharField(max_length=50, default='text')  # text, image, video, audio, location, reaction, edit, document, sticker
    content = models.TextField(blank=True, default='')
    sender_name = models.CharField(max_length=255, blank=True)
    sender = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='messages')
    whatsapp_message_id = models.CharField(max_length=255, null=True, blank=True)
    media_url = models.URLField(max_length=2000, null=True, blank=True)
    metadata = models.JSONField(null=True, blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)
    context_message = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='replies'
    )

    def __str__(self):
        return f"{self.conversation.contact_name} - {self.content[:50]}"

    class Meta:
        ordering = ['created_at', 'id']
        indexes = [
            models.Index(fields=['conversation', 'created_at'], name='msg_conv_created_idx'),
            models.Index(fields=['conversation', 'is_read', 'direction'], name='msg_unread_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['whatsapp_message_id'],
                name='unique_whatsapp_message_id',
                condition=Q(whatsapp_message_id__isnull=False),
            ),
        ]


class ConversationTag(models.Model):
    """Temporary tags/assignments for conversations"""
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='tags')
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='assigned_tags')
    tag_name = models.CharField(max_length=255)  # e.g., "Billing Issue", "Technical Support"
    tag_color = models.CharField(max_length=20, default='blue')  # Tailwind color classes
    note = models.TextField(blank=True, default='')
    
    expiry_type = models.CharField(max_length=20, choices=EXPIRY_CHOICES, default='1h')
    expires_at = models.DateTimeField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='created_tags')
    
    def __str__(self):
        return f"{self.tag_name} - {self.conversation.contact_name}"

    def is_expired(self):
        """Check if tag has expired"""
        if self.expires_at is None:
            return False
        return timezone.now() > self.expires_at

    @classmethod
    def create_tag(cls, conversation, tag_name, expiry_type='1h', created_by=None, tag_color='blue', custom_expiry_minutes=None):
        """Factory method to create a tag with proper expiry calculation"""
        tag = cls(
            conversation=conversation,
            tag_name=tag_name,
            expiry_type=expiry_type,
            expires_at=get_expiry_datetime(expiry_type, custom_expiry_minutes),
            created_by=created_by,
            tag_color=tag_color,
        )
        tag.save()
        return tag

    class Meta:
        ordering = ['-created_at']


class ConversationNote(models.Model):
    """Notes attached to a conversation."""

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='notes')
    content = models.TextField()
    expiry_type = models.CharField(max_length=20, choices=EXPIRY_CHOICES, default='1h')
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='created_notes')
    
    def __str__(self):
        return f"Note - {self.conversation.contact_name}"

    def is_expired(self):
        if self.expires_at is None:
            return False
        return timezone.now() > self.expires_at

    @classmethod
    def create_note(cls, conversation, content, expiry_type='1h', created_by=None, custom_expiry_minutes=None):
        note = cls(
            conversation=conversation,
            content=content,
            expiry_type=expiry_type,
            expires_at=get_expiry_datetime(expiry_type, custom_expiry_minutes),
            created_by=created_by,
        )
        note.save()
        return note

    class Meta:
        ordering = ['-created_at']


class ConversationTake(models.Model):
    """Active claims for a conversation."""

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='takes')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='taken_conversations')
    duration_minutes = models.PositiveIntegerField(default=30)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Taken - {self.conversation.contact_name}"

    def is_expired(self):
        return timezone.now() > self.expires_at

    @classmethod
    def create_take(cls, conversation, created_by=None, duration_minutes=30):
        take = cls(
            conversation=conversation,
            created_by=created_by,
            duration_minutes=duration_minutes,
            expires_at=timezone.now() + timedelta(minutes=duration_minutes),
        )
        take.save()
        return take

    class Meta:
        ordering = ['-created_at']


class StickerAsset(models.Model):
    """Reusable image asset for stickers and quick replies."""

    name = models.CharField(max_length=255)
    image = models.FileField(upload_to='stickers/%Y/%m/')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sticker_assets')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['-created_at']


class SSEToken(models.Model):
    """Short-lived one-time token for SSE connections."""
    key = models.CharField(max_length=64, unique=True, db_index=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)

    def is_valid(self):
        return not self.used and self.expires_at > timezone.now()
