"""
Models for WhatsApp Messenger API
"""
from django.db import models
from django.db.models import Q
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from zoneinfo import ZoneInfo
from django.db.models.signals import post_save
from django.dispatch import receiver
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
        bog_now = now.astimezone(ZoneInfo('America/Bogota'))
        eod_bog = bog_now.replace(hour=23, minute=59, second=59, microsecond=0)
        return eod_bog.astimezone(ZoneInfo('UTC'))
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
    # ── Ops (Domiitulua) client link ────────────────────────────────────
    ops_client_user_id = models.IntegerField(null=True, blank=True, db_index=True)
    ops_client_linked_at = models.DateTimeField(null=True, blank=True)
    ops_client_match_source = models.CharField(
        max_length=20, blank=True, default='',
        choices=[('auto_phone', 'Auto (teléfono)'), ('manual', 'Manual'), ('order', 'Pedido')],
    )
    ops_client_snapshot = models.JSONField(default=dict, blank=True)
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
    is_forwarded = models.BooleanField(default=False)
    is_frequently_forwarded = models.BooleanField(default=False)
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


class ConversationUserPin(models.Model):
    """User-specific pin for a conversation, separate from group-wide is_pinned."""

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='user_pins')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='pinned_conversations')
    pinned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('conversation', 'user')]
        ordering = ['-pinned_at']
        indexes = [
            models.Index(fields=['user', 'conversation']),
        ]

    def __str__(self):
        return f"{self.user.username} pinned {self.conversation.contact_name}"


class ConversationTake(models.Model):
    """Active claims for a conversation."""

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='takes')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='taken_conversations')
    duration_minutes = models.PositiveIntegerField(default=10)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Taken - {self.conversation.contact_name}"

    def is_expired(self):
        return timezone.now() > self.expires_at

    @classmethod
    def create_take(cls, conversation, created_by=None, duration_minutes=10):
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

    name = models.CharField(max_length=255, blank=True, default='')
    image = models.FileField(upload_to='stickers/%Y/%m/')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sticker_assets')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name or 'Sin título'

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
        ('link_client', 'Link Client'),
        ('unlink_client', 'Unlink Client'),
        ('cancel_order', 'Cancel Order'),
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


class MessageSuggestion(models.Model):
    """Persistent store of agent-sent messages for autocomplete suggestions.
    Lives independently from the Message table (which is cleaned every 7 days)."""

    text = models.TextField(unique=True)
    tags = models.JSONField(default=list)
    agent = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='message_suggestions')
    usage_count = models.PositiveIntegerField(default=1)
    last_used = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-last_used']
        verbose_name = "Message Suggestion"
        verbose_name_plural = "Message Suggestions"
        indexes = [
            GinIndex(fields=['text'], name='msg_suggestion_text_gin_idx', opclasses=['gin_trgm_ops']),
        ]


class BotExemptContact(models.Model):
    """Phone numbers pre-registered as not handled by the bot."""
    SOURCE_CHOICES = [
        ('manual', 'Manual'),
        ('ops_sync', 'Sincronizado'),
    ]

    contact_phone = models.CharField(max_length=50, unique=True)
    contact_name = models.CharField(max_length=255, blank=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='manual')
    ops_courier_id = models.IntegerField(null=True, blank=True, db_index=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.contact_name or '—'} ({self.contact_phone})"

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Bot Exempt Contact"
        verbose_name_plural = "Bot Exempt Contacts"


class IntegrationApiKey(models.Model):
    """API key for server-to-server integrations (ops → Messager).

    Only the SHA-256 hash is stored; the raw key is shown once at creation.
    """
    name = models.CharField(max_length=120)
    key_hash = models.CharField(max_length=64, unique=True)
    prefix = models.CharField(max_length=12)
    scopes = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.prefix}…)"

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Integration API Key"
        verbose_name_plural = "Integration API Keys"


class Order(models.Model):
    """Local mirror of a Domiitulua pedido (batch of one or more stops)."""
    STATUS_CHOICES = [
        ('draft', 'Borrador'), ('pending', 'Enviando'), ('failed', 'Fallido'),
        ('programado', 'Programado'),
        ('disponible', 'Buscando domiciliario'), ('asignado', 'Asignado'),
        ('confirmado', 'Confirmado'), ('en_ruta', 'En camino'),
        ('entregado', 'Entregado'), ('cancelado', 'Cancelado'),
    ]

    conversation = models.ForeignKey(Conversation, related_name='orders', on_delete=models.CASCADE)
    ops_batch_id = models.CharField(max_length=32, null=True, blank=True, db_index=True)
    ops_client_user_id = models.IntegerField(null=True, blank=True, db_index=True)
    # Domiciliario asignado en la creación (modo manual). Se llena con la
    # respuesta de ops y se mantiene al día con los eventos de estado.
    ops_courier_user_id = models.IntegerField(null=True, blank=True, db_index=True)
    courier_name = models.CharField(max_length=255, blank=True, default='')
    courier_code = models.CharField(max_length=12, blank=True, default='')
    client_name = models.CharField(max_length=255, blank=True, default='')
    origin_address = models.TextField(blank=True, default='')
    payment_method = models.CharField(max_length=20, blank=True, default='')
    profile = models.CharField(max_length=20, blank=True, default='')
    tools = models.JSONField(default=list, blank=True)
    acompanante = models.BooleanField(default=False)
    total = models.DecimalField(max_digits=12, decimal_places=0, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft', db_index=True)
    # Pedido programado: instante futuro (aware, America/Bogota) en que ops lo
    # activa; `scheduled_released` marca que un domi pre-asignado se liberó.
    scheduled_for = models.DateTimeField(null=True, blank=True, db_index=True)
    scheduled_released = models.BooleanField(default=False)
    notified_statuses = models.JSONField(default=list, blank=True)
    source = models.CharField(max_length=10, default='agent')  # agent | llm
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    payload = models.JSONField(default=dict, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Pedido {self.ops_batch_id or self.pk} ({self.get_status_display()})"

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Order"
        verbose_name_plural = "Orders"
        indexes = [
            models.Index(fields=['conversation', 'status'], name='order_conv_status_idx'),
        ]


class OrderStop(models.Model):
    """Single parada of an Order.

    Multi-stop pedidos are ops ``comandas``: one ops order with N stops and a
    single ``order_number`` shared by every local stop. Legacy per-stop orders
    keep their own number.
    """
    order = models.ForeignKey(Order, related_name='stops', on_delete=models.CASCADE)
    stop_no = models.PositiveSmallIntegerField(default=1)
    ops_order_number = models.BigIntegerField(null=True, blank=True, db_index=True)
    service_type = models.CharField(max_length=40)
    dest_address = models.TextField(blank=True, default='')
    lat = models.FloatField(null=True, blank=True)
    lng = models.FloatField(null=True, blank=True)
    description = models.CharField(max_length=500, blank=True, default='')
    observation = models.CharField(max_length=500, blank=True, default='')
    price = models.DecimalField(max_digits=12, decimal_places=0, default=0)
    status = models.CharField(max_length=20, default='pending')
    canceled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=500, blank=True, default='')
    payload = models.JSONField(default=dict, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Parada {self.stop_no} · {self.dest_address or self.service_type}"

    class Meta:
        ordering = ['stop_no', 'id']
        verbose_name = "Order Stop"
        verbose_name_plural = "Order Stops"


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
    is_closed = models.BooleanField(default=False, help_text="If True, marks the entire day/date as closed (overrides open/close_time)")
    label = models.CharField(max_length=100, blank=True)
    is_closed = models.BooleanField(default=False, help_text="If True, this block is non-working (break/closure)")

    class Meta:
        ordering = ['day_of_week', 'date']
        constraints = [
            models.CheckConstraint(
                condition=(
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


class TemplateExclusion(models.Model):
    SOURCE_CHOICES = [
        ('manual', 'Manual'),
        ('stop_button', 'Stop Button'),
    ]

    contact_phone = models.CharField(max_length=50, unique=True)
    contact_name = models.CharField(max_length=255, blank=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='manual')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Template Exclusion"
        verbose_name_plural = "Template Exclusions"

    def __str__(self):
        return f"{self.contact_phone} — {self.source}"


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


class AgentTakeRecord(models.Model):
    """Immutable record of a conversation take for historical stats.
    
    Created when an agent takes a conversation (or the bot takes it).
    Persists even after the ConversationTake is deleted.
    """
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='take_records')
    agent = models.ForeignKey(User, on_delete=models.CASCADE, related_name='take_records')
    taken_at = models.DateTimeField()
    released_at = models.DateTimeField(null=True, blank=True)
    first_response_at = models.DateTimeField(null=True, blank=True)
    duration_minutes = models.PositiveIntegerField(default=10)

    def __str__(self):
        return f"TakeRecord: {self.agent.username} → {self.conversation.contact_name} @ {self.taken_at}"

    class Meta:
        ordering = ['-taken_at']
        indexes = [
            models.Index(fields=['agent', 'taken_at'], name='takerecord_agent_taken_idx'),
            models.Index(fields=['conversation', 'taken_at'], name='takerecord_conv_taken_idx'),
        ]
        verbose_name = "Agent Take Record"
        verbose_name_plural = "Agent Take Records"


def create_agent_take_record(conversation, agent, taken_at=None, duration_minutes=10):
    """Create an immutable take record (called when an agent takes a conversation)."""
    from django.utils import timezone
    AgentTakeRecord.objects.create(
        conversation=conversation,
        agent=agent,
        taken_at=taken_at or timezone.now(),
        duration_minutes=duration_minutes,
    )


def release_agent_take_records(conversation=None, agent=None):
    """Mark AgentTakeRecords as released.
    
    If conversation is specified, only release records for that conversation.
    If agent is specified, only release records for that agent.
    If both are None, releases ALL unreleased records (used at bot startup).
    """
    from django.utils import timezone
    qs = AgentTakeRecord.objects.filter(released_at__isnull=True)
    if conversation:
        qs = qs.filter(conversation=conversation)
    if agent:
        qs = qs.filter(agent=agent)
    qs.update(released_at=timezone.now())


def set_first_response(conversation, agent, responded_at=None):
    from django.utils import timezone
    AgentTakeRecord.objects.filter(
        conversation=conversation,
        agent=agent,
        released_at__isnull=True,
        first_response_at__isnull=True,
    ).update(first_response_at=responded_at or timezone.now())


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
