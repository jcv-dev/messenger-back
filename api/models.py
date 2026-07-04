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
from django.contrib.postgres.search import SearchVectorField
from django.contrib.postgres.indexes import GinIndex


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
    resolved_by_bot = models.BooleanField(default=False)
    is_pinned = models.BooleanField(default=False, db_index=True)
    pinned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        contact_id = self.contact_phone or self.whatsapp_username or self.whatsapp_id
        return f"{self.contact_name} ({contact_id})"

    class Meta:
        ordering = ['-is_pinned', 'pinned_at', '-last_message_at', '-created_at']
        indexes = [
            models.Index(fields=['status', 'last_message_at'], name='conv_status_lastmsg_idx'),
            models.Index(fields=['whatsapp_id'], name='conv_waid_idx'),
            models.Index(fields=['is_pinned', 'pinned_at'], name='conv_pin_idx'),
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
    search_vector = SearchVectorField(null=True, blank=True)
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
            GinIndex(fields=['search_vector'], name='msg_search_gin_idx'),
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


class AuditLog(models.Model):
    """Security-relevant action log."""
    ACTION_CHOICES = [
        ('take', 'Take'),
        ('release', 'Release'),
        ('pin', 'Pin'),
        ('unpin', 'Unpin'),
        ('send_message', 'Send Message'),
        ('delete_conversation', 'Delete Conversation'),
        ('toggle_status', 'Toggle Status'),
    ]

    actor = models.ForeignKey(User, on_delete=models.CASCADE, related_name='audit_logs')
    conversation = models.ForeignKey(Conversation, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs')
    action = models.CharField(max_length=32, choices=ACTION_CHOICES)
    detail = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self):
        return f"{self.actor.username} {self.action} {self.conversation_id or '—'}"

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['actor', 'created_at'], name='audit_actor_created_idx'),
            models.Index(fields=['conversation', 'created_at'], name='audit_conv_created_idx'),
        ]


class CannedResponse(models.Model):
    """Pre-written responses for quick insertion by agents."""
    title = models.CharField(max_length=120)
    content = models.TextField()
    category = models.CharField(max_length=60, blank=True, default='')
    group = models.ForeignKey(CityGroup, on_delete=models.CASCADE, null=True, blank=True, related_name='canned_responses')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='canned_responses')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.title

    class Meta:
        ordering = ['category', 'title']
        verbose_name = "Canned Response"
        verbose_name_plural = "Canned Responses"


class BotExemptContact(models.Model):
    """Phone numbers pre-registered as not handled by the bot."""
    contact_phone = models.CharField(max_length=50, unique=True)
    contact_name = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.contact_name or '—'} ({self.contact_phone})"

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Bot Exempt Contact"
        verbose_name_plural = "Bot Exempt Contacts"


class BotSchedule(models.Model):
    """Operating hours for the bot. Each row is either a recurring day-of-week
    slot or a specific date override. Times are in UTC-05 (America/Bogota)."""
    day_of_week = models.IntegerField(
        null=True, blank=True,
        choices=[
            (0, 'Lunes'), (1, 'Martes'), (2, 'Miércoles'),
            (3, 'Jueves'), (4, 'Viernes'), (5, 'Sábado'), (6, 'Domingo y Festivos'),
        ],
    )
    date = models.DateField(null=True, blank=True)
    open_time = models.TimeField()
    close_time = models.TimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    label = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ['day_of_week', 'date']
        constraints = [
            models.CheckConstraint(
                check=(
                    Q(day_of_week__isnull=False, date__isnull=True) |
                    Q(day_of_week__isnull=True, date__isnull=False)
                ),
                name='bot_schedule_exactly_one_of_day_or_date',
            ),
        ]

    def __str__(self):
        if self.day_of_week is not None:
            days = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo y Festivos']
            day = days[self.day_of_week]
            hours = f'{self.open_time.strftime("%H:%M")}–{self.close_time.strftime("%H:%M")}' if self.close_time else 'Cerrado'
            return f'{day}: {hours}'
        hours = f'{self.open_time.strftime("%H:%M")}–{self.close_time.strftime("%H:%M")}' if self.close_time else 'Cerrado'
        return f'{self.date} ({self.label or "Excepción"}): {hours}'


class BotConfig(models.Model):
    """Key-value configuration store for bot settings. Managed via admin panel."""
    key = models.CharField(max_length=100, unique=True)
    value = models.JSONField()
    description = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['key']

    def __str__(self):
        return self.key


TEMPLATE_STATUS_CHOICES = [
    ('PENDING', 'Pending'),
    ('APPROVED', 'Approved'),
    ('REJECTED', 'Rejected'),
    ('PAUSED', 'Paused'),
    ('DISABLED', 'Disabled'),
    ('FLAGGED', 'Flagged'),
    ('PENDING_DELETION', 'Pending Deletion'),
    ('DELETED', 'Deleted'),
    ('ARCHIVED', 'Archived'),
    ('UNARCHIVED', 'Unarchived'),
    ('IN_APPEAL', 'In Appeal'),
    ('LIMIT_EXCEEDED', 'Limit Exceeded'),
    ('LOCKED', 'Locked'),
    ('REINSTATED', 'Reinstated'),
]

TEMPLATE_CATEGORY_CHOICES = [
    ('MARKETING', 'Marketing'),
    ('UTILITY', 'Utility'),
    ('AUTHENTICATION', 'Authentication'),
]

TEMPLATE_QUALITY_CHOICES = [
    ('GREEN', 'Green'),
    ('YELLOW', 'Yellow'),
    ('RED', 'Red'),
    ('UNKNOWN', 'Unknown'),
]


class Call(models.Model):
    DIRECTION_CHOICES = [
        ('inbound', 'Inbound'),
        ('outbound', 'Outbound'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('ringing', 'Ringing'),
        ('connected', 'Connected'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('rejected', 'Rejected'),
        ('missed', 'Missed'),
    ]

    call_id = models.CharField(max_length=255, unique=True)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name='calls'
    )
    direction = models.CharField(max_length=20, choices=DIRECTION_CHOICES)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='pending')
    from_number = models.CharField(max_length=20)
    to_number = models.CharField(max_length=20)
    recipient_bsuid = models.CharField(max_length=255, null=True, blank=True)
    start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    biz_opaque_callback_data = models.CharField(max_length=512, null=True, blank=True)
    deeplink_payload = models.CharField(max_length=1024, null=True, blank=True)
    cta_payload = models.CharField(max_length=1024, null=True, blank=True)
    recording_status = models.CharField(max_length=20, null=True, blank=True)
    recording_purpose = models.CharField(max_length=250, null=True, blank=True)
    recording_announcement_language = models.CharField(max_length=20, null=True, blank=True)
    recording_audio_id = models.CharField(max_length=255, null=True, blank=True)
    recording_audio_url = models.URLField(max_length=1024, null=True, blank=True)
    recording_audio_sha256 = models.CharField(max_length=255, null=True, blank=True)
    recording_audio_mime_type = models.CharField(max_length=100, null=True, blank=True)
    recording_local_path = models.CharField(max_length=1024, null=True, blank=True)
    sdp_offer = models.TextField(null=True, blank=True)
    sdp_answer = models.TextField(null=True, blank=True)
    error_code = models.IntegerField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Call {self.call_id} ({self.direction}/{self.status})"

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['conversation', 'created_at']),
        ]


class WhatsAppTemplate(models.Model):
    """WhatsApp message template. Managed via admin UI and synced with Meta Graph API."""
    name = models.CharField(max_length=512)
    language = models.CharField(max_length=10, default='es')
    category = models.CharField(max_length=20, choices=TEMPLATE_CATEGORY_CHOICES, default='MARKETING')
    template_id = models.CharField(max_length=50, null=True, blank=True)
    status = models.CharField(max_length=30, choices=TEMPLATE_STATUS_CHOICES, default='PENDING')
    quality_score = models.CharField(max_length=10, choices=TEMPLATE_QUALITY_CHOICES, null=True, blank=True)
    components = models.JSONField(default=list)
    rejection_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "WhatsApp Template"
        verbose_name_plural = "WhatsApp Templates"
        unique_together = [('name', 'language')]

    def __str__(self):
        return f"{self.name} ({self.language}) — {self.status}"


class AgentPresence(models.Model):
    """Tracks agent online/away/offline status with heartbeat interval."""
    STATUS_CHOICES = [
        ('online', 'Online'),
        ('away', 'Away'),
        ('offline', 'Offline'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='presence')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='offline')
    last_seen = models.DateTimeField(auto_now=True)
    heartbeat_interval = models.PositiveSmallIntegerField(default=30)

    def __str__(self):
        return f"{self.user.username} — {self.status}"

    class Meta:
        ordering = ['user__username']
        verbose_name = "Agent Presence"
        verbose_name_plural = "Agent Presences"


class PushSubscription(models.Model):
    """Web Push subscription for browser push notifications."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='push_subscriptions')
    endpoint = models.URLField(max_length=512)
    p256dh = models.CharField(max_length=256)
    auth = models.CharField(max_length=128)
    browser = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('user', 'endpoint')]
        ordering = ['-created_at']
        verbose_name = "Push Subscription"
        verbose_name_plural = "Push Subscriptions"

    def __str__(self):
        return f"{self.user.username} — {self.browser or 'desconocido'}"
