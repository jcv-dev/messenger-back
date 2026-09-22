"""
Views for WhatsApp Messenger API
"""
import time
import base64
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.filters import SearchFilter
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.parsers import FormParser, MultiPartParser, JSONParser
from django.utils import timezone
from django.contrib.auth.models import User
from django.http import FileResponse, HttpResponse, HttpResponseNotFound, JsonResponse, StreamingHttpResponse
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.cache import cache_page
from django.db import transaction, models
from rest_framework.authentication import TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from datetime import datetime, timezone as dt_timezone, timedelta
from django.shortcuts import get_object_or_404
from django.db.models import Exists, OuterRef, Subquery, Q, Count, Prefetch, Value
from django.core.signing import BadSignature
import threading
import hmac
import hashlib
from concurrent.futures import ThreadPoolExecutor

from uuid import uuid4
from .models import Conversation, Message, ConversationTag, ConversationNote, ConversationTake, ConversationUserPin, StickerAsset, SSEToken, CityGroup, BotExemptContact, BotSchedule, BotConfig, WhatsAppTemplate, TemplateExclusion, Call, AuditLog, CannedResponse, AgentPresence, PushSubscription, AgentTakeRecord, create_agent_take_record, release_agent_take_records, set_first_response
from .serializers import (
    ConversationSerializer, CityGroupSerializer,
    ConversationListSerializer, MessageSerializer, ConversationTagSerializer,
    ConversationNoteSerializer, ConversationTakeSerializer,
    CreateConversationTagSerializer, CreateConversationNoteSerializer,
    BotScheduleSerializer, BotConfigSerializer,
    TakeConversationSerializer, InitiateConversationSerializer,
    UserSerializer, StickerAssetSerializer, BotExemptContactSerializer,
    WhatsAppTemplateSerializer, CallSerializer, AuditLogSerializer,
    CannedResponseSerializer, AgentPresenceSerializer,
    TemplateExclusionSerializer, PushSubscriptionSerializer, MessageSearchSerializer,
    media_signer, sign_media_url,
)
from .redis_client import get_sync_redis
import asyncio
import json
import subprocess
import tempfile
import logging
import csv

from django.core.cache import cache

from .realtime import publish, subscribe, unsubscribe
from .rate_limiter import acquire as acquire_rate_capacity
from .integrations.clients import schedule_auto_link

logger = logging.getLogger('api')


def get_default_group():
    try:
        return CityGroup.objects.get(slug='tulua')
    except CityGroup.DoesNotExist:
        return None

_send_pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix='wa-send')

WHATSAPP_MEDIA_LIMITS = {
    'image': 5 * 1024 * 1024,
    'sticker': 500 * 1024,
    'video': 16 * 1024 * 1024,
    'audio': 16 * 1024 * 1024,
    'document': 100 * 1024 * 1024,
}
_download_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix='wa-dl')

# Outbound send queue.
#
# Every outbound Message walks the same lifecycle stored in
# ``Message.metadata``:
#
#   pending  -> queued, cancellable, ``scheduled_for`` is due now (bot/ops) or
#               ``send_delay_seconds`` ahead (agent undo window)
#   sending  -> claimed by a send-pool worker; the claim carries
#               ``send_claimed_at`` / ``send_claim_id`` / ``send_attempts``
#   sent / failed / cancelled -> terminal
#
# The DB is the durable queue: the sweeper re-dispatches due ``pending`` rows
# and recovers ``sending`` rows whose claiming process died (uvicorn recycles
# workers with --limit-max-requests, deploys/SIGKILL kill in-flight tasks).
# Rows stranded by older code carry no ``send_claimed_at`` and are never
# retried automatically.
_SEND_CLAIM_ERROR = (
    'El servidor se reinició durante el envío y no se pudo confirmar la entrega. '
    'Revisa el estado con el cliente antes de reenviarlo.'
)

_sweeper_started = False
_sweeper_lock = threading.Lock()


def _start_sweeper():
    global _sweeper_started
    with _sweeper_lock:
        if _sweeper_started:
            return
        _sweeper_started = True
    t = threading.Thread(target=_sweeper_loop, daemon=True, name='msg-sweeper')
    t.start()


def _sweeper_loop():
    while True:
        try:
            _process_pending_messages()
        except Exception:
            logger.exception("Message sweeper error")
        try:
            _recover_stale_sends()
        except Exception:
            logger.exception("Stale send recovery error")
        time.sleep(5)


def _parse_send_timestamp(value):
    """Parse an ISO timestamp stored in ``Message.metadata``."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def _outbound_payload(message):
    """Resolve the ``(message_type, content)`` pair the sender expects.

    Messages store human-readable text in ``content`` while the wire payload
    for interactive/location messages lives in ``metadata``.
    """
    meta = message.metadata if isinstance(message.metadata, dict) else {}
    message_type = message.message_type or 'text'
    if message_type == 'location':
        return message_type, meta.get('location') or message.content
    if message_type == 'interactive':
        return message_type, meta.get('interactive') or message.content
    if message_type in ('sticker', 'image', 'video', 'audio', 'document'):
        return message_type, message.media_url or message.content
    return message_type, message.content


def _send_claim_is_current(message_id, claim_id):
    """Whether ``claim_id`` still owns the send for ``message_id``."""
    if not message_id or not claim_id:
        return True
    meta = Message.objects.filter(id=message_id).values_list('metadata', flat=True).first()
    if meta is None:
        return False
    return isinstance(meta, dict) and meta.get('send_claim_id') == claim_id


def _enqueue_outbound_send(message, schedule_delay=None):
    """Persist the durable queue markers for a freshly created outbound message.

    The message stays ``pending`` (and cancellable) until a send-pool worker
    claims it. ``schedule_delay`` defaults to ``send_delay_seconds``; bot and
    ops notifications pass ``0`` so they leave immediately.
    """
    from api.bot.config import get_send_delay_seconds
    if schedule_delay is None:
        schedule_delay = get_send_delay_seconds()
    schedule_delay = max(0, int(schedule_delay))
    meta = dict(message.metadata or {})
    meta['status'] = 'pending'
    meta.setdefault('send_attempts', 0)
    meta['scheduled_for'] = (timezone.now() + timedelta(seconds=schedule_delay)).isoformat()
    message.metadata = meta
    message.save(update_fields=['metadata'])

    _start_sweeper()
    message_id = message.id
    if schedule_delay:
        threading.Timer(schedule_delay, _delayed_send, args=[message_id]).start()
    else:
        transaction.on_commit(lambda mid=message_id: _dispatch_send(mid))
    return message


def _delayed_send(message_id):
    _dispatch_send(message_id)


def _dispatch_send(message_id):
    """Submit a message to the send pool. Idempotent: the worker claims it."""
    try:
        _send_pool.submit(_run_claimed_send, message_id)
    except RuntimeError:
        # Pool already shut down (process exiting): the row stays pending and
        # another process's sweeper re-dispatches it.
        logger.info("Send pool closed; message %s left for the sweeper", message_id)
    except Exception:
        logger.exception("Failed to dispatch outbound send for message %s", message_id)


def _process_pending_messages():
    """Dispatch due ``pending`` messages; workers claim and send them."""
    now = timezone.now().isoformat()
    due_ids = list(Message.objects.filter(
        metadata__status='pending',
        metadata__scheduled_for__lte=now,
    ).values_list('id', flat=True)[:20])
    for message_id in due_ids:
        _dispatch_send(message_id)


def _run_claimed_send(message_id):
    """Claim a queued message and send it. Runs inside the send pool.

    Claiming is atomic and lease-based: only one worker owns a send at a time
    (a second dispatched task sees a fresh claim and skips), a stale claim from
    a dead process is adopted after the lease, and recovery rotates
    ``send_claim_id`` so a zombie worker can no longer send.
    """
    from api.bot.config import get_send_lease_seconds, get_send_max_attempts
    now = timezone.now()
    lease = timedelta(seconds=max(1, int(get_send_lease_seconds())))
    max_attempts = max(1, int(get_send_max_attempts()))
    fail_reason = None
    claim_id = None
    msg = None
    with transaction.atomic():
        # No select_related here: ``context_message`` is nullable and the
        # LEFT OUTER JOIN makes Postgres reject FOR UPDATE.
        msg = (
            Message.objects.select_for_update(skip_locked=True)
            .filter(id=message_id)
            .first()
        )
        if msg is None or msg.whatsapp_message_id:
            return
        meta = msg.metadata if isinstance(msg.metadata, dict) else {}
        status = meta.get('status')
        if status not in (None, 'pending', 'sending'):
            return
        if status != 'sending':
            scheduled_for = _parse_send_timestamp(meta.get('scheduled_for'))
            if scheduled_for is not None and scheduled_for > now:
                return
        claimed_at = _parse_send_timestamp(meta.get('send_claimed_at'))
        if claimed_at is not None and now - claimed_at < lease:
            return  # another worker is already sending this message
        attempts = int(meta.get('send_attempts') or 0) + 1
        if attempts > max_attempts:
            fail_reason = _SEND_CLAIM_ERROR
        else:
            claim_id = uuid4().hex
            meta = dict(meta)
            meta['status'] = 'sending'
            meta['send_attempts'] = attempts
            meta['send_claimed_at'] = now.isoformat()
            meta['send_claim_id'] = claim_id
            msg.metadata = meta
            msg.save(update_fields=['metadata'])
    if fail_reason:
        _mark_send_failed(message_id, msg.conversation_id, fail_reason, 100)
        return
    try:
        _deliver_claimed_send(msg, claim_id)
    except Exception:
        logger.exception("Outbound send crashed for message %s", message_id)
        current = Message.objects.filter(id=message_id).values_list('metadata', flat=True).first()
        if isinstance(current, dict) and current.get('status') == 'sending':
            _mark_send_failed(
                message_id, msg.conversation_id, 'Error inesperado al enviar el mensaje', 100,
            )


def _deliver_claimed_send(msg, claim_id):
    """Run the delivery for a claimed message (outside the claim transaction)."""
    meta = msg.metadata if isinstance(msg.metadata, dict) else {}
    fallback = meta.get('fallback_template')
    context_wamid = None
    if msg.context_message is not None:
        context_wamid = msg.context_message.whatsapp_message_id
    if fallback:
        # Ops status notifications swap in an approved template when Meta
        # rejects the text for a closed 24 h window.
        from api.integrations.notify import _deliver_outbound
        _deliver_outbound(
            msg.id, msg.conversation_id, fallback, context_wamid, claim_id=claim_id,
        )
        return
    message_type, content = _outbound_payload(msg)
    send_whatsapp_outbound(
        message_type, content, msg.conversation.contact_phone,
        msg.id, msg.conversation_id,
        context_wamid=context_wamid,
        claim_id=claim_id,
    )


def _recover_stale_sends():
    """Re-dispatch sends whose claiming process died mid-flight.

    Only rows carrying a ``send_claimed_at`` marker are eligible, so messages
    stranded by older code (no marker) are never retried automatically. A
    message that exhausts its attempts is marked failed instead of silently
    staying in ``sending`` forever.
    """
    from api.bot.config import get_send_lease_seconds, get_send_max_attempts
    lease_seconds = max(1, int(get_send_lease_seconds()))
    max_attempts = max(1, int(get_send_max_attempts()))
    now = timezone.now()
    lease = timedelta(seconds=lease_seconds)
    cutoff = (now - lease).isoformat()
    stale = list(Message.objects.filter(
        metadata__status='sending',
        whatsapp_message_id__isnull=True,
        metadata__send_claimed_at__lt=cutoff,
    ).only('id', 'conversation_id', 'metadata').order_by('id')[:50])

    recovered = 0
    failed = 0
    for candidate in stale:
        fail_id = None
        fail_conv_id = None
        recover_id = None
        with transaction.atomic():
            locked = (
                Message.objects.select_for_update(skip_locked=True)
                .filter(
                    id=candidate.id,
                    metadata__status='sending',
                    whatsapp_message_id__isnull=True,
                )
                .first()
            )
            if locked is None:
                continue
            meta = locked.metadata if isinstance(locked.metadata, dict) else {}
            claimed_at = _parse_send_timestamp(meta.get('send_claimed_at'))
            if claimed_at is None or now - claimed_at < lease:
                continue
            attempts = int(meta.get('send_attempts') or 0)
            if attempts >= max_attempts:
                fail_id = locked.id
                fail_conv_id = locked.conversation_id
            else:
                meta = dict(meta)
                meta['send_claim_id'] = uuid4().hex
                locked.metadata = meta
                locked.save(update_fields=['metadata'])
                recover_id = locked.id
        if fail_id is not None:
            _mark_send_failed(fail_id, fail_conv_id, _SEND_CLAIM_ERROR, 100)
            failed += 1
            logger.error(
                "Send %s marked failed after %s attempts (process restarts)",
                fail_id, max_attempts,
            )
        elif recover_id is not None:
            _dispatch_send(recover_id)
            recovered += 1
    if recovered or failed:
        logger.warning(
            "Stale send recovery: requeued=%s failed=%s (lease=%ss)",
            recovered, failed, lease_seconds,
        )


import uuid
import os
import mimetypes
import urllib.request
import urllib.error
import urllib.parse

def _log_audit(actor, conversation, action, detail=''):
    try:
        AuditLog.objects.create(actor=actor, conversation=conversation, action=action, detail=detail)
    except Exception:
        logger.exception("Failed to create audit log entry")

def _wamid_msg_sig(wamid_str):
    """Extract message-identity bytes from a WAMID as a hex signature.

    WhatsApp uses different WAMID namespaces (customer phone vs business WABA)
    for the same message. The message-identity bytes at the tail of the
    decoded protobuf are identical across namespaces. Taking the last 25
    decoded bytes gives a stable signature for matching.
    """
    if not wamid_str or not wamid_str.startswith('wamid.'):
        return None
    body = wamid_str[len('wamid.'):]
    padding = len(body) % 4
    if padding:
        body += '=' * (4 - padding)
    try:
        decoded = base64.b64decode(body)
        return decoded[-25:].hex()
    except Exception:
        return None


def publish_conversation_update(conversation, message=None, escalated=False):
    try:
        payload = {
            'type': 'conversation.updated',
            'conversation': ConversationListSerializer(conversation).data,
        }
        if message is not None:
            payload['message'] = message
        if escalated:
            payload['escalated'] = True
        # Include typing indicator status
        try:
            redis = get_sync_redis()
            typing_key = f'typing:{conversation.id}'
            if redis.exists(typing_key):
                payload['conversation']['typing'] = True
        except Exception:
            pass
        publish(payload, group_id=conversation.group_id)
    except Exception:
        logger.exception("Failed to publish SSE conversation update")

    # Push notification for inbound messages to the take owner
    if message and message.get('direction') == 'inbound':
        try:
            from .models import PushSubscription
            now_ts = int(time.time())

            # Find who has the take on this conversation
            active_take = ConversationTake.objects.filter(
                conversation=conversation,
                expires_at__gt=timezone.now(),
            ).exclude(created_by__username='bot').first()

            if active_take and active_take.created_by_id:
                redis = get_sync_redis()
                throttle_key = f'push:last:{active_take.created_by_id}:{conversation.id}'
                try:
                    last_push = redis.get(throttle_key)
                    if last_push and now_ts - int(last_push) < 30:
                        return
                except Exception:
                    pass

                subscriptions = PushSubscription.objects.filter(user_id=active_take.created_by_id)
                if subscriptions.exists():
                    import threading
                    from django.conf import settings as dj_settings
                    contact_name = conversation.custom_name or conversation.contact_name
                    msg_preview = (message.get('content') or '')[:120]
                    push_payload = {
                        'title': contact_name,
                        'body': msg_preview,
                        'icon': '/favicon.ico',
                        'badge': '/favicon.ico',
                        'tag': f'conversation-{conversation.id}',
                        'data': {
                            'url': '/',
                            'conversation_id': conversation.id,
                        },
                    }
                    for sub in subscriptions:
                        threading.Thread(
                            target=_send_push_notification,
                            args=(sub, push_payload, throttle_key, now_ts),
                            daemon=True,
                        ).start()
        except Exception:
            logger.exception("Failed to send push notification")


def _send_push_notification(subscription, payload, throttle_key, now_ts):
    """Send a push notification to a single subscription. Ran in daemon thread."""
    try:
        from pywebpush import webpush, WebPushException
        webpush(
            subscription_info={
                'endpoint': subscription.endpoint,
                'keys': {
                    'p256dh': subscription.p256dh,
                    'auth': subscription.auth,
                },
            },
            data=json.dumps(payload),
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={
                'sub': f'mailto:{settings.VAPID_CLAIMS_EMAIL}',
            },
        )
        # Throttle successful push
        try:
            redis = get_sync_redis()
            redis.setex(throttle_key, 30, str(now_ts))
        except Exception:
            pass
    except Exception as e:
        # Remove subscription on 410 Gone
        if hasattr(e, 'status_code') and e.status_code == 410:
            try:
                subscription.delete()
                logger.info("Removed expired push subscription %s", subscription.id)
            except Exception:
                pass
        logger.debug("Push notification failed for sub %s: %s", subscription.id, str(e))

def upload_media_to_whatsapp(file_path, phone_number_id, token):
    url = f"https://graph.facebook.com/v20.0/{phone_number_id}/media"
    boundary = uuid.uuid4().hex
    
    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        mime_type = 'application/octet-stream'
        
    with open(file_path, 'rb') as f:
        file_data = f.read()
        
    filename = os.path.basename(file_path)
    
    body = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"messaging_product\"\r\n\r\n"
        f"whatsapp\r\n"
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode('utf-8') + file_data + f"\r\n--{boundary}--\r\n".encode('utf-8')
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}"
    }
    
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=30) as response:
        res_data = json.loads(response.read().decode())
        return res_data.get('id')


def _convert_audio_to_ogg_opus(audio_path, actual_duration=None):
    """Convert any audio (WebM, MP4, AAC) to OGG Opus for WhatsApp compatibility.

    Uses a PCM intermediate to strip all container metadata, producing clean
    OGG Opus output regardless of broken input timestamps (e.g., Chrome
    MediaRecorder WebM chunks that reset timestamps every 250ms).
    """
    try:
        fd, ogg_path = tempfile.mkstemp(suffix='.ogg', prefix='wa_audio_')
        os.close(fd)
    except OSError:
        return None
    try:
        # First pass: decode to raw PCM to strip ALL container metadata.
        # PCM has no timestamps, so broken WebM cluster timestamps are irrelevant.
        pcm_cmd = [
            'ffmpeg', '-y',
            '-i', audio_path,
            '-f', 's16le', '-ac', '1', '-ar', '48000',
            '-',
        ]
        pcm = subprocess.run(pcm_cmd, capture_output=True, timeout=30)
        if pcm.returncode != 0:
            logger.warning("ffmpeg PCM decode failed: %s",
                           pcm.stderr.decode('utf-8', errors='replace')[:200])
            try:
                os.remove(ogg_path)
            except OSError:
                pass
            return None

        # Second pass: re-encode clean PCM to OGG Opus with exact duration.
        ogg_cmd = [
            'ffmpeg', '-y',
            '-f', 's16le', '-ar', '48000', '-ac', '1',
            '-i', '-',
            '-c:a', 'libopus', '-b:a', '32k',
            '-application', 'voip',
            '-frame_duration', '60',
            '-vn',
        ]
        if actual_duration is not None and actual_duration > 0:
            ogg_cmd.extend(['-af', f'atrim=0:{actual_duration:.1f}'])
        ogg_cmd.append(ogg_path)
        result = subprocess.run(ogg_cmd, input=pcm.stdout, capture_output=True, timeout=30)
        if result.returncode != 0:
            logger.warning("ffmpeg OGG re-encode failed: %s",
                           result.stderr.decode('utf-8', errors='replace')[:200])
            try:
                os.remove(ogg_path)
            except OSError:
                pass
            return None
        return ogg_path
    except Exception as e:
        logger.warning("ffmpeg audio to OGG conversion error: %s", e)
        try:
            os.remove(ogg_path)
        except OSError:
            pass
        return None


def _compress_image(file_path, max_bytes):
    try:
        from PIL import Image
        img = Image.open(file_path)
        for quality in (85, 65, 45, 25):
            fd, tmp = tempfile.mkstemp(suffix='.jpg', prefix='wa_img_')
            os.close(fd)
            try:
                save_img = img.convert('RGB') if img.mode in ('RGBA', 'P', 'LA', 'PA') else img
                save_img.save(tmp, 'JPEG', quality=quality, optimize=True)
                if os.path.getsize(tmp) <= max_bytes:
                    return tmp
            except Exception:
                pass
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        for scale in (0.7, 0.5, 0.3):
            fd, tmp = tempfile.mkstemp(suffix='.jpg', prefix='wa_img_')
            os.close(fd)
            try:
                w, h = img.size
                resized = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
                save_img = resized.convert('RGB') if resized.mode in ('RGBA', 'P', 'LA', 'PA') else resized
                save_img.save(tmp, 'JPEG', quality=75, optimize=True)
                if os.path.getsize(tmp) <= max_bytes:
                    return tmp
            except Exception:
                pass
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    except Exception as e:
        logger.warning("Image compression failed for %s: %s", file_path, e)
    return None


def _compress_video(file_path, max_bytes):
    try:
        fd, tmp = tempfile.mkstemp(suffix='.mp4', prefix='wa_vid_')
        os.close(fd)
        for crf, scale in [(28, 1.0), (32, 0.7), (36, 0.5)]:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
                cmd = [
                    'ffmpeg', '-y',
                    '-i', file_path,
                    '-c:v', 'libx264', '-preset', 'fast',
                    '-crf', str(crf),
                    '-c:a', 'aac', '-b:a', '64k',
                    '-movflags', '+faststart',
                ]
                if scale < 1.0:
                    cmd.extend(['-vf', f'scale=iw*{scale}:ih*{scale}'])
                cmd.append(tmp)
                subprocess.run(cmd, capture_output=True, timeout=120)
                if os.path.exists(tmp) and os.path.getsize(tmp) <= max_bytes:
                    return tmp
            except Exception:
                pass
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception as e:
        logger.warning("Video compression failed for %s: %s", file_path, e)
    return None


def _compress_audio(file_path, max_bytes):
    try:
        pcm_cmd = [
            'ffmpeg', '-y',
            '-i', file_path,
            '-f', 's16le', '-ac', '1', '-ar', '48000',
            '-',
        ]
        pcm = subprocess.run(pcm_cmd, capture_output=True, timeout=30)
        if pcm.returncode != 0:
            logger.warning("Audio PCM decode failed for compression")
            return None
        for bitrate in (24, 16, 12):
            fd, tmp = tempfile.mkstemp(suffix='.ogg', prefix='wa_audio_')
            os.close(fd)
            try:
                ogg_cmd = [
                    'ffmpeg', '-y',
                    '-f', 's16le', '-ar', '48000', '-ac', '1',
                    '-i', '-',
                    '-c:a', 'libopus', '-b:a', f'{bitrate}k',
                    '-application', 'voip',
                    '-frame_duration', '60',
                    '-vn',
                    tmp,
                ]
                result = subprocess.run(ogg_cmd, input=pcm.stdout, capture_output=True, timeout=30)
                if result.returncode == 0 and os.path.getsize(tmp) <= max_bytes:
                    return tmp
            except Exception:
                pass
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    except Exception as e:
        logger.warning("Audio compression failed for %s: %s", file_path, e)
    return None


def _ensure_media_under_limit(file_path, message_type):
    limit = WHATSAPP_MEDIA_LIMITS.get(message_type)
    if limit is None:
        return file_path, None
    if os.path.getsize(file_path) <= limit:
        return file_path, None
    logger.info(
        "Media %s (%d bytes) exceeds WhatsApp limit (%d bytes), compressing...",
        message_type, os.path.getsize(file_path), limit,
    )
    if message_type in ('image', 'sticker'):
        compressed = _compress_image(file_path, limit)
    elif message_type == 'video':
        compressed = _compress_video(file_path, limit)
    elif message_type == 'audio':
        compressed = _compress_audio(file_path, limit)
    else:
        compressed = None
    if compressed:
        try:
            compressed_size = os.path.getsize(compressed)
            logger.info("Media %s compressed to %d bytes", message_type, compressed_size)
        except OSError:
            pass
        return compressed, compressed
    logger.warning(
        "Media %s (%d bytes) exceeds limit and compression failed",
        message_type, os.path.getsize(file_path),
    )
    return None, None


def download_whatsapp_media(media_value, token, media_type):
    if not media_value or not token:
        return None

    try:
        if media_value.startswith('http'):
            fetch_url = media_value
        else:
            graph_url = f"https://graph.facebook.com/v20.0/{media_value}"
            req = urllib.request.Request(graph_url, headers={'Authorization': f'Bearer {token}'})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
                fetch_url = data.get('url', '')
            if not fetch_url:
                return None

        req = urllib.request.Request(fetch_url, headers={'Authorization': f'Bearer {token}'})
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            content_type = response.headers.get('Content-Type', 'application/octet-stream')

        ext = mimetypes.guess_extension(content_type.split(';')[0].strip()) or '.bin'
        filename = f"{uuid.uuid4().hex}{ext}"
        local_dir = os.path.join(settings.MEDIA_ROOT, 'whatsapp', media_type)
        os.makedirs(local_dir, exist_ok=True)
        local_path = os.path.join(local_dir, filename)
        with open(local_path, 'wb') as f:
            f.write(raw)

        return f"{settings.MEDIA_URL}whatsapp/{media_type}/{filename}"
    except Exception as e:
        logger.error("Error downloading WhatsApp media: %s", e)
        return None


def download_media_async(message_id, raw_media, media_type):
    token = settings.WHATSAPP_API_TOKEN
    url = download_whatsapp_media(raw_media, token, media_type)
    if url:
        try:
            msg = Message.objects.get(id=message_id)
            msg.media_url = url
            msg.save(update_fields=['media_url'])
            publish_conversation_update(msg.conversation, MessageSerializer(msg).data)
        except Message.DoesNotExist:
            pass


def _resolve_media_path(content):
    if not content:
        logger.debug("_resolve_media_path: empty content")
        return None
    parsed = urllib.parse.urlparse(content)
    path = parsed.path
    if parsed.scheme and parsed.hostname:
        host = parsed.hostname
        if host not in ('localhost', '127.0.0.1', ''):
            logger.debug("_resolve_media_path: external host %s, skipping local resolution", host)
            return None
    media_url = getattr(settings, 'MEDIA_URL', '/media/')
    if path.startswith('/api/media/'):
        relative = path[len('/api/media/'):].lstrip('/')
    elif path.startswith(media_url):
        relative = path[len(media_url):].lstrip('/')
    else:
        relative = path.lstrip('/')
    file_path = os.path.join(settings.MEDIA_ROOT, relative)
    exists = os.path.exists(file_path)
    logger.debug("_resolve_media_path: path=%s -> file=%s exists=%s", content, file_path, exists)
    return file_path if exists else None


def _mark_send_failed(message_id, conversation_id, error_message, error_code=None):
    if not message_id:
        return
    try:
        failed_msg = Message.objects.get(id=message_id)
        meta = failed_msg.metadata or {}
        meta['send_error'] = error_message
        if error_code is not None:
            meta['send_error_code'] = error_code
        meta['status'] = 'failed'
        failed_msg.metadata = meta
        failed_msg.save(update_fields=['metadata'])
        if conversation_id:
            conv = Conversation.objects.get(id=conversation_id)
            publish_conversation_update(conv, MessageSerializer(failed_msg).data)
    except Exception:
        logger.exception('Failed to update send_error for message %d', message_id)


def _resolve_whatsapp_target(contact_phone, recipient=None, conversation_id=None):
    """Resolve what an outbound send should target: phone or BSUID.

    Username-only WhatsApp contacts have no phone number; Meta identifies them
    with a business-scoped user ID (BSUID, ``CO.956283237534428``) stored as the
    conversation's ``whatsapp_id``. Phone numbers go in the ``to`` field, BSUIDs
    in the top-level ``recipient`` field. When the caller only knows the
    conversation, its ``whatsapp_id`` is used as a fallback so every existing
    send site supports username contacts.

    Returns ``(to, bsuid)``; at most one of them is set.
    """
    for value in (contact_phone, recipient):
        text = str(value or '').strip()
        if not text:
            continue
        digits = text[1:] if text.startswith('+') else text
        if digits.isdigit():
            return digits, None
        return None, text

    if conversation_id:
        wa_id = (
            Conversation.objects.filter(id=conversation_id)
            .values_list('whatsapp_id', flat=True)
            .first()
        )
        text = str(wa_id or '').strip()
        if text:
            digits = text[1:] if text.startswith('+') else text
            if digits.isdigit():
                return digits, None
            return None, text
    return None, None


def send_whatsapp_outbound(message_type, content, contact_phone, message_id=None, conversation_id=None, context_wamid=None, recipient=None, claim_id=None):
    if claim_id and not _send_claim_is_current(message_id, claim_id):
        logger.warning(
            'Outbound send for message %s aborted: claim %s was superseded',
            message_id, claim_id,
        )
        return
    phone_number_id = getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', None) or settings.WHATSAPP_PHONE_NUMBER
    token = settings.WHATSAPP_API_TOKEN
    to, bsuid = _resolve_whatsapp_target(contact_phone, recipient, conversation_id)
    target = to or bsuid
    if not phone_number_id or not token or not target:
        logger.error(
            'WhatsApp outbound send blocked: phone_number_id=%s token_set=%s contact_phone=%r recipient=%r',
            bool(phone_number_id), bool(token), contact_phone, recipient,
        )
        _mark_send_failed(
            message_id, conversation_id,
            'No se pudo enviar: el contacto no tiene número de teléfono ni usuario de WhatsApp',
            100,
        )
        return

    try:
        url = f"{settings.WHATSAPP_GRAPH_BASE_URL}/{phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "type": message_type,
        }
        if to:
            payload["to"] = to
        if bsuid:
            # Username-only contact: target the business-scoped user ID.
            payload["recipient"] = bsuid

        if message_type == 'text':
            payload['text'] = {"body": content}
        elif message_type == 'interactive':
            payload['interactive'] = content
        elif message_type == 'location':
            payload['location'] = content
        elif message_type in ['sticker', 'image', 'video', 'audio', 'document']:
            parsed = urllib.parse.urlparse(content)
            media_id = None

            file_path = _resolve_media_path(content)
            temp_files = []
            if file_path:
                upload_path = file_path
                converted_path = None
                if message_type == 'audio':
                    _, ext = os.path.splitext(file_path)
                    should_convert = ext.lower() in ('.webm', '.mp4', '.m4a', '.aac')
                    actual_duration = None
                    if message_id:
                        try:
                            msg = Message.objects.get(id=message_id)
                            meta = msg.metadata or {}
                            mime = meta.get('mime_type', '')
                            should_convert = should_convert or ('mp4' in mime or 'aac' in mime)
                            actual_duration = meta.get('duration_seconds')
                        except Message.DoesNotExist:
                            pass
                    if should_convert:
                        logger.info("Converting audio for message %s: actual_duration=%s", message_id, actual_duration)
                        converted_path = _convert_audio_to_ogg_opus(file_path, actual_duration=actual_duration)
                        if converted_path:
                            upload_path = converted_path
                            temp_files.append(converted_path)
                
                upload_path, compressed_path = _ensure_media_under_limit(upload_path, message_type)
                if compressed_path:
                    temp_files.append(compressed_path)
                
                try:
                    if upload_path:
                        acquire_rate_capacity(phone_number_id)
                        try:
                            media_id = upload_media_to_whatsapp(upload_path, phone_number_id, token)
                        except urllib.error.HTTPError as e:
                            err_body = e.read().decode() if hasattr(e, 'read') else ''
                            logger.warning("Media upload to WhatsApp failed: HTTP %s %s", e.code, err_body[:200])
                        except Exception:
                            logger.warning("Media upload to WhatsApp failed (network/config error)")
                    else:
                        logger.warning(
                            "Media upload skipped for %s: file exceeds WhatsApp limit and compression failed",
                            message_type,
                        )
                finally:
                    for tmp in temp_files:
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass

            if media_id:
                if message_type == 'audio':
                    is_voice = False
                    if message_id:
                        try:
                            msg = Message.objects.get(id=message_id)
                            is_voice = (msg.metadata or {}).get('voice', False)
                        except Message.DoesNotExist:
                            pass
                    payload['audio'] = {"id": media_id, "voice": is_voice}
                else:
                    media_payload = {"id": media_id}
                    if message_type in ('document', 'image', 'video') and message_id:
                        try:
                            msg = Message.objects.get(id=message_id)
                            if msg.content:
                                media_payload['caption'] = msg.content
                        except Message.DoesNotExist:
                            pass
                    if message_type == 'document' and message_id:
                        try:
                            msg = Message.objects.get(id=message_id)
                            meta = msg.metadata or {}
                            if meta.get('filename'):
                                media_payload['filename'] = meta['filename']
                        except Message.DoesNotExist:
                            pass
                    payload[message_type] = media_payload
            else:
                if message_type == 'audio':
                    if message_id:
                        try:
                            failed_msg = Message.objects.get(id=message_id)
                            meta = failed_msg.metadata or {}
                            meta['send_error'] = 'Audio upload to WhatsApp failed. Cannot send via link fallback.'
                            meta['send_error_code'] = 500
                            failed_msg.metadata = meta
                            failed_msg.save(update_fields=['metadata'])
                            if conversation_id:
                                conv = Conversation.objects.get(id=conversation_id)
                                publish_conversation_update(conv, MessageSerializer(failed_msg).data)
                        except Exception:
                            logger.exception('Failed to update send_error for message %d', message_id)
                    return

                hostname = parsed.hostname
                if hostname is None or hostname in ('localhost', '127.0.0.1', ''):
                    public_host = next((h for h in settings.ALLOWED_HOSTS if h not in ('localhost', '127.0.0.1', '*', '')), None)
                    if public_host:
                        content = urllib.parse.urlunparse(('https', public_host, parsed.path, parsed.params, parsed.query, parsed.fragment))
                    else:
                        logger.warning("Skipping WhatsApp media outbound: no public host available, and upload failed")
                        return
                else:
                    hostname = parsed.hostname
                media_payload = {"link": content}
                if message_type in ('document', 'image', 'video') and message_id:
                    try:
                        msg = Message.objects.get(id=message_id)
                        if msg.content:
                            media_payload['caption'] = msg.content
                    except Message.DoesNotExist:
                        pass
                if message_type == 'document' and message_id:
                    try:
                        msg = Message.objects.get(id=message_id)
                        meta = msg.metadata or {}
                        if meta.get('filename'):
                            media_payload['filename'] = meta['filename']
                    except Message.DoesNotExist:
                        pass
                payload[message_type] = media_payload
        elif message_type == 'template':
            # content is a dict or a JSON string: {name, language, components}
            template = content
            if isinstance(template, str):
                try:
                    template = json.loads(template)
                except (ValueError, TypeError):
                    template = None
            if not isinstance(template, dict) or not template.get('name'):
                logger.error(
                    'WhatsApp template payload invalid for message %s: %r',
                    message_id, str(content)[:200],
                )
                _mark_send_failed(
                    message_id, conversation_id,
                    'Plantilla de WhatsApp inválida', 100,
                )
                return
            payload['type'] = 'template'
            payload['template'] = template
        else:
            payload['type'] = 'text'
            payload['text'] = {"body": content}

        if context_wamid:
            payload['context'] = {"message_id": context_wamid}

        if claim_id and not _send_claim_is_current(message_id, claim_id):
            logger.warning(
                'Outbound send for message %s aborted before POST: claim %s was superseded',
                message_id, claim_id,
            )
            return
        acquire_rate_capacity(phone_number_id)

        body = json.dumps(payload).encode('utf-8')
        logger.info('WhatsApp outbound -> %s [%s]', target, message_type)
        req = urllib.request.Request(url, data=body, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=30) as response:
            resp_body = response.read().decode()
            resp_data = {}
            try:
                resp_data = json.loads(resp_body) or {}
            except Exception:
                logger.warning(
                    'WhatsApp outbound unparseable response for message %s: %r',
                    message_id, resp_body[:300],
                )
            logger.info(
                'WhatsApp outbound response -> %s [%s]: %s',
                target, message_type, resp_body[:600],
            )
            if message_id:
                err = resp_data.get('error')
                if err:
                    logger.error('WhatsApp API error in 200 response: %s', resp_body[:600])
                    _mark_send_failed(
                        message_id, conversation_id,
                        err.get('message') or err.get('type') or 'WhatsApp API error',
                        err.get('code'),
                    )
                else:
                    wamid = ''
                    messages = resp_data.get('messages')
                    if messages and isinstance(messages, list):
                        wamid = messages[0].get('id', '') or ''
                    if wamid:
                        sent_msg = Message.objects.get(id=message_id)
                        sent_msg.whatsapp_message_id = wamid
                        sent_msg.metadata['status'] = 'sent'
                        sent_msg.save(update_fields=['whatsapp_message_id', 'metadata'])
                        if conversation_id:
                            try:
                                conv = Conversation.objects.get(id=conversation_id)
                                sent_msg = Message.objects.get(id=message_id)
                                publish_conversation_update(conv, MessageSerializer(sent_msg).data)
                            except Exception:
                                logger.exception('Failed to publish update after wamid for message %d', message_id)
                    else:
                        logger.warning(
                            'WhatsApp outbound accepted without message id for message %s: %s',
                            message_id, resp_body[:600],
                        )
                        _mark_send_failed(
                            message_id, conversation_id,
                            'WhatsApp accepted the request but returned no message id',
                        )
    except urllib.error.HTTPError as e:
        body = e.read().decode() if hasattr(e, 'read') else ''
        logger.error('WhatsApp API HTTP %s: %s', e.code, body)
        err_data = {}
        try:
            parsed = json.loads(body)
            err_data = parsed.get('error', {})
        except Exception:
            pass
        _mark_send_failed(
            message_id, conversation_id,
            err_data.get('message', body[:200]) or body[:200],
            err_data.get('code', e.code),
        )
    except Exception:
        logger.exception("Error sending WhatsApp message")
        _mark_send_failed(message_id, conversation_id, 'Network error sending message')


# --- WhatsApp Calling API helpers ---

CALL_ERROR_MESSAGES = {
    138006: "El destinatario no tiene habilitados los permisos para recibir llamadas.",
    138007: "Llamada rechazada por el destinatario.",
    138008: "El destinatario no contestó la llamada.",
    368: "Se ha excedido el límite de llamadas. Intente más tarde.",
    100: "Error de autenticación con WhatsApp. Verifique la configuración del token.",
}


class CallAPIError(Exception):
    def __init__(self, message, error_code=None, error_subcode=None, original_message=None, http_status=None):
        super().__init__(message)
        self.error_code = error_code
        self.error_subcode = error_subcode
        self.original_message = original_message
        self.http_status = http_status


def _parse_whatsapp_error(e):
    err_data = {}
    try:
        err_body = e.read().decode() if hasattr(e, 'read') else '{}'
        parsed = json.loads(err_body)
        err_data = parsed.get('error', {})
    except Exception:
        pass
    return err_data


def _call_whatsapp_api(phone_number_id, payload):
    token = settings.WHATSAPP_API_TOKEN
    url = f"https://graph.facebook.com/v20.0/{phone_number_id}/calls"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = json.dumps(payload).encode('utf-8')

    acquire_rate_capacity(phone_number_id)

    try:
        req = urllib.request.Request(url, data=body, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        err_data = _parse_whatsapp_error(e)
        error_code = err_data.get('code')
        error_subcode = err_data.get('error_subcode')
        error_message = err_data.get('message', '')
        logger.error("WhatsApp /calls API error %s: %s", e.code, error_message[:500])
        friendly = CALL_ERROR_MESSAGES.get(error_code)
        if not friendly:
            friendly = f"Error al conectar con WhatsApp ({e.code})"
        raise CallAPIError(
            friendly,
            error_code=error_code,
            error_subcode=error_subcode,
            original_message=error_message,
            http_status=e.code,
        )


def send_whatsapp_call_action(phone_number_id, action, call_id=None, to=None,
                               recipient=None, session=None, biz_data=None,
                               recording=None):
    payload = {"messaging_product": "whatsapp", "action": action}
    if call_id:
        payload["call_id"] = call_id
    if to:
        payload["to"] = to
    if recipient:
        payload["recipient"] = recipient
    if session:
        payload["session"] = session
    if biz_data:
        payload["biz_opaque_callback_data"] = biz_data
    if recording:
        payload["recording"] = recording
    return _call_whatsapp_api(phone_number_id, payload)


def pre_accept_call(call_id, sdp_answer):
    phone_number_id = settings.WHATSAPP_PHONE_NUMBER_ID
    session = {"sdp_type": "answer", "sdp": sdp_answer}
    return send_whatsapp_call_action(
        phone_number_id, "pre_accept", call_id=call_id, session=session,
    )


def accept_call(call_id, sdp_answer, recording=None):
    phone_number_id = settings.WHATSAPP_PHONE_NUMBER_ID
    session = {"sdp_type": "answer", "sdp": sdp_answer}
    return send_whatsapp_call_action(
        phone_number_id, "accept", call_id=call_id, session=session,
        recording=recording,
    )


def reject_call(call_id):
    phone_number_id = settings.WHATSAPP_PHONE_NUMBER_ID
    return send_whatsapp_call_action(
        phone_number_id, "reject", call_id=call_id
    )


def terminate_call(call_id):
    phone_number_id = settings.WHATSAPP_PHONE_NUMBER_ID
    return send_whatsapp_call_action(
        phone_number_id, "terminate", call_id=call_id
    )


def initiate_call(to_number=None, recipient_bsuid=None, sdp_offer=None,
                  biz_data=None, recording=None):
    if not to_number and not recipient_bsuid:
        raise ValueError("Either to_number or recipient_bsuid is required")
    phone_number_id = settings.WHATSAPP_PHONE_NUMBER_ID
    session = {"sdp_type": "offer", "sdp": sdp_offer}
    response = send_whatsapp_call_action(
        phone_number_id, "connect", to=to_number, recipient=recipient_bsuid,
        session=session, biz_data=biz_data, recording=recording,
    )
    calls = response.get("calls", [])
    if calls:
        return calls[0].get("id")
    return None


class ConversationViewSet(viewsets.ModelViewSet):
    """ViewSet for managing conversations"""
    permission_classes = [IsAuthenticated]
    # Per-action DRF throttle scope (the ``orders/draft`` action sets
    # ``order_draft``). DRF validates ``@action`` kwargs against the class, so
    # the attribute must exist even when no action overrides it.
    throttle_scope = None

    def get_permissions(self):
        if self.action in ('destroy', 'remove_expired_tags', 'send_template'):
            return [IsAuthenticated(), IsAdminUser()]
        return super().get_permissions()

    def get_queryset(self):
        """Return conversations for the user's group (staff sees all)"""
        qs = Conversation.objects.select_related('group')
        now = timezone.now()
        last_msg = Message.objects.filter(conversation=OuterRef('pk')).order_by('-created_at')

        user = self.request.user

        # Annotate user pin BEFORE visibility filter so it can be referenced
        if user.is_authenticated:
            user_pin = ConversationUserPin.objects.filter(
                conversation=OuterRef('pk'),
                user=user,
            )
            qs = qs.annotate(
                _has_user_pin=Exists(user_pin),
                _user_pin_at=Subquery(user_pin.values('pinned_at')[:1]),
            )
        else:
            qs = qs.annotate(
                _has_user_pin=Value(False, output_field=models.BooleanField()),
                _user_pin_at=Value(None, output_field=models.DateTimeField()),
            )

        if user.is_authenticated and not user.is_staff:
            try:
                profile = user.profile
                if profile and profile.group_id:
                    qs = qs.filter(group_id=profile.group_id)
            except Exception:
                qs = qs.none()

        # Annotate other-human-take for ALL users for list views
        if user.is_authenticated and self.action in ('list', 'active_conversations', 'mark_all_read'):
            other_human_takes = ConversationTake.objects.filter(
                conversation=OuterRef('pk'),
                expires_at__gt=now,
            ).exclude(created_by__isnull=True).exclude(created_by=user).exclude(created_by__username='bot')
            qs = qs.annotate(
                _has_other_human_take=Exists(other_human_takes)
            )

            before = qs.count()

            # For non-staff: hide conversations taken by another human, unless personally pinned
            if not user.is_staff:
                qs = qs.filter(
                    Q(_has_other_human_take=False)
                    | Q(_has_user_pin=True)
                )

                removed = before - qs.count()
                if removed > 0:
                    logger.info(
                        "VisibilityFilter user=%s action=%s removed_by_other_human_take=%d",
                        user.username, self.action, removed,
                    )

        qs = qs.order_by(
            '-is_pinned', 'pinned_at', '-_has_user_pin', '-last_message_at', '-created_at'
        )

        return qs.annotate(
            _last_msg_sender=Subquery(last_msg.values('sender_name')[:1]),
            _last_msg_direction=Subquery(last_msg.values('direction')[:1]),
            _unread_count=Count('messages', filter=Q(messages__is_read=False, messages__direction='inbound'), distinct=True),
        ).prefetch_related(
            Prefetch('tags', queryset=ConversationTag.objects.select_related('created_by__profile__group').filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))),
            Prefetch('notes', queryset=ConversationNote.objects.select_related('created_by__profile__group').filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))),
            Prefetch('takes', queryset=ConversationTake.objects.select_related('created_by__profile__group').filter(expires_at__gt=now)),
        )

    def get_serializer_class(self):
        if self.action == 'list':
            return ConversationListSerializer
        return ConversationSerializer

    @action(detail=True, methods=['post'])
    def add_tag(self, request, pk=None):
        """Add a tag to a conversation"""
        conversation = self.get_object()
        serializer = CreateConversationTagSerializer(data=request.data)

        if serializer.is_valid():
            tag = ConversationTag.create_tag(
                conversation=conversation,
                tag_name=serializer.validated_data['tag_name'],
                expiry_type=serializer.validated_data['expiry_type'],
                created_by=request.user,
                tag_color=serializer.validated_data.get('tag_color', 'blue'),
                custom_expiry_minutes=serializer.validated_data.get('custom_expiry_minutes'),
            )

            tag_serializer = ConversationTagSerializer(tag)
            publish_conversation_update(conversation)
            return Response(tag_serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def add_note(self, request, pk=None):
        """Add a note to a conversation"""
        conversation = self.get_object()
        serializer = CreateConversationNoteSerializer(data=request.data)

        if serializer.is_valid():
            note = ConversationNote.create_note(
                conversation=conversation,
                content=serializer.validated_data['content'],
                expiry_type=serializer.validated_data['expiry_type'],
                created_by=request.user,
                custom_expiry_minutes=serializer.validated_data.get('custom_expiry_minutes'),
            )
            note_serializer = ConversationNoteSerializer(note)
            publish_conversation_update(conversation)
            return Response(note_serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def remove_note(self, request, pk=None):
        """Delete a note from a conversation"""
        conversation = self.get_object()
        note_id = request.data.get('note_id')

        try:
            note = ConversationNote.objects.get(id=note_id, conversation=conversation)
            note.delete()
            publish_conversation_update(conversation)
            return Response(status=status.HTTP_204_NO_CONTENT)
        except ConversationNote.DoesNotExist:
            return Response({'error': 'Note not found'}, status=status.HTTP_404_NOT_FOUND)

    @transaction.atomic
    @action(detail=True, methods=['post'])
    def take_conversation(self, request, pk=None):
        """Take a conversation for a set amount of time"""
        conversation = self.get_object()
        serializer = TakeConversationSerializer(data=request.data)

        if serializer.is_valid():
            existing = ConversationTake.objects.filter(
                conversation=conversation,
                expires_at__gt=timezone.now(),
            ).first()
            if existing and existing.created_by != request.user and not request.user.is_staff and existing.created_by.username != 'bot':
                return Response(
                    {'error': 'Esta conversación ya está tomada por otro usuario'},
                    status=status.HTTP_409_CONFLICT,
                )

            if conversation.resolved_by_bot:
                conversation.resolved_by_bot = False
                conversation.save(update_fields=['resolved_by_bot'])
            release_agent_take_records(conversation)
            ConversationTake.objects.filter(conversation=conversation).delete()
            duration_minutes = serializer.validated_data.get('duration_minutes', 10)
            take = ConversationTake.create_take(
                conversation=conversation,
                created_by=request.user,
                duration_minutes=duration_minutes,
            )
            create_agent_take_record(conversation, request.user, duration_minutes=duration_minutes)
            take_serializer = ConversationTakeSerializer(take)
            # Clear prefetch cache so SSE serializes fresh takes, not stale prefetch
            if hasattr(conversation, '_prefetched_objects_cache'):
                conversation._prefetched_objects_cache.pop('takes', None)
            publish_conversation_update(conversation)
            _log_audit(request.user, conversation, 'take')
            return Response(take_serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def release_conversation(self, request, pk=None):
        """Release the active conversation claim"""
        conversation = self.get_object()
        active_take = ConversationTake.objects.filter(
            conversation=conversation,
            expires_at__gt=timezone.now(),
        ).first()
        if active_take:
            if active_take.created_by != request.user and not request.user.is_staff and active_take.created_by.username != 'bot':
                return Response(
                    {'error': 'Solo el propietario o un administrador pueden liberar'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            release_agent_take_records(conversation, agent=active_take.created_by)
            active_take.delete()
            try:
                get_sync_redis().setex(f"bot:escalated:{conversation.id}", 600, "1")
            except Exception:
                pass
        if hasattr(conversation, '_prefetched_objects_cache'):
            conversation._prefetched_objects_cache.pop('takes', None)
        publish_conversation_update(conversation)
        _log_audit(request.user, conversation, 'release')
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'])
    def hand_over_to_bot(self, request, pk=None):
        """Release the conversation back to the bot — clears all bot-blocking conditions.

        Does NOT remove Domii tags (exemptions stay intact).
        """
        conversation = self.get_object()

        # 1. Release any human take
        release_agent_take_records(conversation)
        ConversationTake.objects.filter(conversation=conversation).delete()

        try:
            import redis as sync_redis
            r = sync_redis.from_url(settings.REDIS_URL, decode_responses=True)
            pipe = r.pipeline()
            # 2. Delete escalation cooldown
            pipe.delete(f"bot:escalated:{conversation.id}")
            # 3. Delete bot session (forces fresh start)
            pipe.delete(f"bot:session:{conversation.id}")
            # 4. Delete outside-hours replied flags
            for key in r.scan_iter(f"bot:outside_hours_replied:{conversation.id}:*"):
                pipe.delete(key)
            pipe.execute()
            r.close()
        except Exception:
            pass

        if hasattr(conversation, '_prefetched_objects_cache'):
            conversation._prefetched_objects_cache.pop('takes', None)
        publish_conversation_update(conversation)
        return Response(status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def mark_read(self, request, pk=None):
        """Mark all unread inbound messages in a conversation as read"""
        conversation = self.get_object()
        unread_messages = conversation.messages.filter(is_read=False, direction='inbound')
        count = unread_messages.update(is_read=True)
        if count > 0:
            if hasattr(conversation, '_unread_count'):
                del conversation._unread_count
            publish_conversation_update(conversation)
        return Response({'marked_read': count}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'])
    def mark_all_read(self, request):
        """Mark unread inbound messages as read for the given conversation ids,
        or for every conversation visible to the current user when none are given."""
        conversation_ids = request.data.get('conversation_ids') or []
        if conversation_ids:
            qs = self.get_queryset().filter(id__in=conversation_ids)
        else:
            qs = self.get_queryset()
        conversations = list(qs)

        marked_read = 0
        affected = []
        for conversation in conversations:
            unread_messages = Message.objects.filter(
                conversation=conversation, is_read=False, direction='inbound'
            )
            count = unread_messages.update(is_read=True)
            marked_read += count
            if count > 0:
                if hasattr(conversation, '_unread_count'):
                    del conversation._unread_count
                affected.append(conversation.id)
                publish_conversation_update(conversation)

        return Response(
            {'marked_read': marked_read, 'conversations': affected},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'])
    def set_custom_name(self, request, pk=None):
        """Set a custom display name for this contact"""
        conversation = self.get_object()
        custom_name = request.data.get('custom_name', '').strip() or None
        conversation.custom_name = custom_name
        conversation.save(update_fields=['custom_name', 'updated_at'])

        Conversation.objects.filter(whatsapp_id=conversation.whatsapp_id, custom_name__isnull=True).update(
            custom_name=custom_name
        )

        publish_conversation_update(conversation)
        return Response({'custom_name': custom_name}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def set_group(self, request, pk=None):
        """Move a conversation to a different group."""
        from .serializers import SetGroupSerializer
        conversation = self.get_object()
        old_group_id = conversation.group_id
        serializer = SetGroupSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        new_group_id = serializer.validated_data['group_id']
        if old_group_id == new_group_id:
            return Response({'group': conversation.group_id}, status=status.HTTP_200_OK)

        conversation.group_id = new_group_id
        conversation.save(update_fields=['group_id', 'updated_at'])

        if hasattr(conversation, '_prefetched_objects_cache'):
            conversation._prefetched_objects_cache.pop('takes', None)
        publish_conversation_update(conversation)

        return Response(ConversationSerializer(conversation).data)

    @action(detail=True, methods=['post'], url_path='link-client')
    def link_client(self, request, pk=None):
        """Link or unlink this conversation to an ops (Domiitulua) client.

        Body ``{"ops_client_user_id": 88}`` links (optionally with a
        ``client`` snapshot for the card); ``{}`` or ``{"ops_client_user_id":
        null}`` unlinks.
        """
        from .integrations import ops as ops_client
        from .integrations.clients import (
            MATCH_SOURCE_MANUAL, fetch_client_by_phone, link_conversation,
            unlink_conversation,
        )

        conversation = self.get_object()
        raw_id = request.data.get('ops_client_user_id')

        if raw_id in (None, '', False):
            if conversation.ops_client_user_id:
                unlink_conversation(conversation)
                _log_audit(request.user, conversation, 'unlink_client')
                publish_conversation_update(conversation)
            return Response(ConversationSerializer(conversation).data)

        try:
            client_id = int(raw_id)
        except (TypeError, ValueError):
            return Response(
                {'error': 'ops_client_user_id debe ser un entero.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if client_id <= 0:
            return Response(
                {'error': 'ops_client_user_id inválido.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        snapshot = request.data.get('client')
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        if not snapshot and conversation.contact_phone and ops_client.is_configured():
            # Best effort: enrich from the conversation phone when it matches.
            resolved = fetch_client_by_phone(conversation.contact_phone, timeout=5)
            if resolved and resolved['id'] == client_id:
                snapshot = resolved

        link_conversation(
            conversation, client_id, snapshot=snapshot, source=MATCH_SOURCE_MANUAL,
        )
        _log_audit(request.user, conversation, 'link_client', str(client_id))

        # An ops batch created before the link never matched an event, so it
        # stays invisible in "Pedidos activos" until its next status change.
        # Adopt the client's active orders now (best effort: never fails the link).
        try:
            from .integrations.adoption import sync_active_orders
            sync_active_orders(conversation)
        except Exception:
            logger.exception('Failed to backfill active orders after link')

        publish_conversation_update(conversation)
        return Response(ConversationSerializer(conversation).data)

    @action(detail=True, methods=['get', 'post'], url_path='orders')
    def orders(self, request, pk=None):
        """List or create pedidos for this conversation (plan §4.3)."""
        from .order_views import create_conversation_order, list_conversation_orders

        conversation = self.get_object()
        if request.method == 'POST':
            return create_conversation_order(request, conversation)
        return list_conversation_orders(request, conversation)

    @action(
        detail=True,
        methods=['post'],
        url_path='orders/draft',
        throttle_classes=[ScopedRateThrottle],
        throttle_scope='order_draft',
    )
    def orders_draft(self, request, pk=None):
        """LLM order draft from a message onward (plan §4.3/§6.2, Phase 6)."""
        from .order_views import draft_conversation_order

        return draft_conversation_order(request, self.get_object())

    @action(detail=True, methods=['post'])
    def toggle_pin(self, request, pk=None):
        """Toggle pinned state. type=group (group-wide, visible to all in the group)
        or type=personal (default, user-specific)."""
        conversation = self.get_object()
        pin_type = request.data.get('type', 'personal')

        if pin_type == 'group':
            conversation.is_pinned = not conversation.is_pinned
            conversation.pinned_at = timezone.now() if conversation.is_pinned else None
            conversation.save(update_fields=['is_pinned', 'pinned_at'])
            _log_audit(request.user, conversation, 'pin' if conversation.is_pinned else 'unpin', 'group')
        else:
            user_pin, created = ConversationUserPin.objects.get_or_create(
                conversation=conversation, user=request.user,
            )
            if not created:
                user_pin.delete()
                _log_audit(request.user, conversation, 'unpin', 'personal')
            else:
                _log_audit(request.user, conversation, 'pin', 'personal')

        # Set annotations on instance so serializer can find them
        user_pin_exists = ConversationUserPin.objects.filter(
            conversation=conversation, user=request.user,
        ).exists()
        conversation._has_user_pin = user_pin_exists
        if user_pin_exists:
            pin_obj = ConversationUserPin.objects.get(conversation=conversation, user=request.user)
            conversation._user_pin_at = pin_obj.pinned_at
        else:
            conversation._user_pin_at = None

        if hasattr(conversation, '_prefetched_objects_cache'):
            conversation._prefetched_objects_cache.pop('takes', None)
        publish_conversation_update(conversation)
        serializer = self.get_serializer(conversation)
        return Response(serializer.data)

    def destroy(self, request, *args, **kwargs):
        conversation = self.get_object()
        _log_audit(request.user, conversation, 'delete_conversation')
        return super().destroy(request, *args, **kwargs)

    @action(detail=False, methods=['post'])
    def remove_expired_tags(self, request):
        """Delete expired tags, notes, and takes"""
        now = timezone.now()
        tags_count = ConversationTag.objects.filter(expires_at__lt=now).delete()[0]
        notes_count = ConversationNote.objects.filter(expires_at__lt=now).delete()[0]
        AgentTakeRecord.objects.filter(released_at__isnull=True, taken_at__lt=now).update(released_at=now)
        takes_count = ConversationTake.objects.filter(expires_at__lt=now).delete()[0]
        return Response({'deleted_count': tags_count + notes_count + takes_count})

    @action(detail=True, methods=['post'])
    def remove_tag(self, request, pk=None):
        """Remove a tag from a conversation"""
        conversation = self.get_object()
        tag_id = request.data.get('tag_id')

        try:
            tag = ConversationTag.objects.get(id=tag_id, conversation=conversation)
            tag.delete()
            publish_conversation_update(conversation)
            return Response(status=status.HTTP_204_NO_CONTENT)
        except ConversationTag.DoesNotExist:
            return Response({'error': 'Tag not found'}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=['get', 'post'])
    @transaction.atomic
    def messages(self, request, pk=None):
        """Get or create messages for a conversation"""
        conversation = self.get_object()

        if request.method == 'GET':
            before = request.query_params.get('before')
            limit = min(int(request.query_params.get('limit', 50)), 200)

            queryset = conversation.messages.select_related('context_message').filter(
                Q(metadata__status__isnull=True) | ~Q(metadata__status='cancelled')
            )

            if before:
                try:
                    before_msg = Message.objects.get(id=before, conversation=conversation)
                    queryset = queryset.filter(
                        Q(created_at__lt=before_msg.created_at) |
                        Q(created_at=before_msg.created_at, id__lt=before_msg.id)
                    )
                except Message.DoesNotExist:
                    pass

            queryset = queryset.order_by('-created_at', '-id')
            msg_page = list(queryset[:limit + 1])
            has_more = len(msg_page) > limit
            msg_page = msg_page[:limit]
            msg_page.reverse()
            cursor = msg_page[0].id if msg_page and has_more else None

            serializer = MessageSerializer(msg_page, many=True)
            return Response({
                'results': serializer.data,
                'cursor': cursor,
                'has_more': has_more,
            })

        elif request.method == 'POST':
            data = request.data
            direction = data.get('direction', 'outbound')
            message_type = data.get('message_type', 'text')
            content = data.get('content') or ''
            context_message_id = data.get('context_message_id')

            context_msg = None
            if context_message_id:
                try:
                    context_msg = Message.objects.get(id=context_message_id, conversation=conversation)
                except Message.DoesNotExist:
                    pass

            uploaded_file = request.FILES.get('file')
            if uploaded_file:
                import uuid, os
                ext = os.path.splitext(uploaded_file.name)[1] or '.bin'
                safe_name = f"{uuid.uuid4().hex}{ext}"
                upload_subdir = {
                    'image': 'images',
                    'video': 'videos',
                    'audio': 'audio',
                    'document': 'documents',
                }.get(message_type, 'videos')
                upload_dir = os.path.join(settings.MEDIA_ROOT, 'uploads', upload_subdir)
                os.makedirs(upload_dir, exist_ok=True)
                file_path = os.path.join(upload_dir, safe_name)
                with open(file_path, 'wb+') as dest:
                    for chunk in uploaded_file.chunks():
                        dest.write(chunk)
                content = f"{settings.MEDIA_URL}uploads/{upload_subdir}/{safe_name}"

            metadata = data.get('metadata', {})
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except (json.JSONDecodeError, TypeError):
                    metadata = {}

            message = Message.objects.create(
                conversation=conversation,
                direction=direction,
                message_type=message_type,
                content=content,
                sender_name=data.get('sender_name', request.user.get_full_name() or request.user.username),
                sender=request.user if direction == 'outbound' else None,
                context_message=context_msg,
                metadata=metadata,
            )

            if message_type in ('image', 'sticker', 'video', 'audio', 'document') and content:
                parsed = urllib.parse.urlparse(content)
                if parsed.scheme or parsed.path.startswith('/media/') or parsed.path.startswith('/api/media/'):
                    message.media_url = content
                    message.content = ''
                    message.save(update_fields=['media_url', 'content'])

            if message_type == 'text':
                conversation.last_message = content
            elif message_type == 'image':
                conversation.last_message = '[Image]'
            elif message_type == 'video':
                conversation.last_message = '[Video]'
            elif message_type == 'audio':
                meta = data.get('metadata', {})
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except (json.JSONDecodeError, TypeError):
                        meta = {}
                conversation.last_message = '[Voice message]' if meta.get('voice') else '[Audio]'
            elif message_type == 'sticker':
                conversation.last_message = '[Sticker]'
            elif message_type == 'document':
                conversation.last_message = '[Document]'
            else:
                conversation.last_message = f'[{message_type.capitalize()}]'
            conversation.last_message_at = timezone.now()

            if direction == 'outbound' and message_type not in ('edit', 'reaction'):
                _enqueue_outbound_send(message)

            if direction == 'outbound' and message_type not in ('edit', 'reaction'):
                set_first_response(conversation, request.user)

            conversation.save(update_fields=['last_message', 'last_message_at', 'updated_at'])

            conversation._last_msg_direction = direction
            if hasattr(conversation, '_prefetched_objects_cache'):
                conversation._prefetched_objects_cache.pop('takes', None)
            serializer = MessageSerializer(message)
            publish_conversation_update(conversation, serializer.data)
            if direction == 'outbound':
                _log_audit(request.user, conversation, 'send_message', content[:200])

                if message_type == 'text' and content:
                    from .suggestions import index_message
                    convo_tags = list(
                        ConversationTag.objects.filter(
                            conversation=conversation,
                        ).filter(
                            Q(expires_at__gt=timezone.now()) | Q(expires_at__isnull=True),
                        ).values_list('tag_name', flat=True)
                    )
                    index_message(content, convo_tags, request.user.id)

            return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'])
    def active_conversations(self, request):
        """Get active conversations with cursor-based pagination"""
        user = request.user
        queryset = self.get_queryset().filter(status='active').annotate(
            msg_count=Count('messages')
        ).filter(msg_count__gt=0)

        before = request.query_params.get('before')
        limit = min(int(request.query_params.get('limit', 100)), 500)

        if request.query_params.get('only_unread') == 'true':
            queryset = queryset.filter(messages__is_read=False, messages__direction='inbound').distinct()

        if before:
            queryset = queryset.filter(last_message_at__lt=before)

        queryset = queryset.order_by('-is_pinned', 'pinned_at', '-_has_user_pin', '-last_message_at', '-created_at')
        conv_page = list(queryset[:limit + 1])
        has_more = len(conv_page) > limit
        conv_page = conv_page[:limit]

        cursor = conv_page[-1].last_message_at.isoformat() if conv_page and has_more else None

        if not user.is_staff:
            logger.debug(
                "ActiveConversations user=%s returned=%d total_in_group=%s",
                user.username, len(conv_page),
                queryset.model.objects.filter(
                    group_id=getattr(getattr(user, 'profile', None), 'group_id', None),
                    status='active'
                ).annotate(
                    mc=Count('messages')
                ).filter(mc__gt=0).count(),
            )

        serializer = ConversationListSerializer(conv_page, many=True)
        return Response({
            'results': serializer.data,
            'cursor': cursor,
            'has_more': has_more,
        })

    @action(detail=True, methods=['get'])
    def metadata(self, request, pk=None):
        """Get conversation metadata without messages (tags, notes, takes, contact info)"""
        conversation = self.get_object()
        now = timezone.now()
        conversation.tags.filter(expires_at__lt=now, expires_at__isnull=False).delete()
        conversation.notes.filter(expires_at__lt=now, expires_at__isnull=False).delete()
        AgentTakeRecord.objects.filter(
            conversation=conversation,
            released_at__isnull=True,
            taken_at__lt=now,
        ).update(released_at=now)
        conversation.takes.filter(expires_at__lt=now).delete()
        serializer = ConversationSerializer(conversation)
        data = serializer.data
        data.pop('messages', None)
        return Response(data)

    @action(detail=False, methods=['post'])
    @transaction.atomic
    def initiate(self, request):
        """Start a new conversation and send the first outbound message"""
        ser = InitiateConversationSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        contact_phone = ser.validated_data['contact_phone']
        contact_name = ser.validated_data['contact_name']
        content = ser.validated_data['content']
        message_type = ser.validated_data.get('message_type', 'text')

        conversation, created = Conversation.objects.get_or_create(
            whatsapp_id=contact_phone,
            defaults={
                'contact_name': contact_name,
                'contact_phone': contact_phone,
                'group': get_default_group(),
            }
        )

        if not created:
            if conversation.contact_name != contact_name:
                conversation.contact_name = contact_name
            if conversation.contact_phone != contact_phone:
                conversation.contact_phone = contact_phone
            if conversation.status != 'active':
                conversation.status = 'active'
            if conversation.resolved_by_bot:
                conversation.resolved_by_bot = False
            conversation.save(update_fields=['contact_name', 'contact_phone', 'status', 'resolved_by_bot', 'updated_at'])

        message = Message.objects.create(
            conversation=conversation,
            direction='outbound',
            message_type=message_type,
            content=content,
            sender_name=request.user.get_full_name() or request.user.username,
            sender=request.user,
        )

        if message_type in ('image', 'sticker', 'video', 'audio', 'document') and content:
            parsed = urllib.parse.urlparse(content)
            if parsed.scheme or parsed.path.startswith('/media/'):
                message.media_url = content
                message.content = ''
                message.save(update_fields=['media_url', 'content'])

        if message_type == 'text':
            conversation.last_message = content
        elif message_type == 'image':
            conversation.last_message = '[Image]'
        elif message_type == 'video':
            conversation.last_message = '[Video]'
        elif message_type == 'audio':
            conversation.last_message = '[Voice message]'
        elif message_type == 'sticker':
            conversation.last_message = '[Sticker]'
        elif message_type == 'document':
            conversation.last_message = '[Document]'
        else:
            conversation.last_message = f'[{message_type.capitalize()}]'
        conversation.last_message_at = timezone.now()
        conversation.save()

        conversation._last_msg_direction = 'outbound'

        if message_type not in ('edit', 'reaction'):
            _enqueue_outbound_send(message)

        publish_conversation_update(conversation)

        return Response(ConversationSerializer(conversation).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'])
    def search(self, request):
        """Search conversations by name, phone, ID, tags, or notes (scoped to user's group)."""
        q = request.query_params.get('q', '').strip()
        if not q:
            return Response({'results': []})
        now = timezone.now()

        base_qs = Conversation.objects.select_related('group').filter(
            Q(contact_name__icontains=q) |
            Q(custom_name__icontains=q) |
            Q(contact_phone__icontains=q) |
            Q(whatsapp_id__icontains=q) |
            Q(whatsapp_username__icontains=q) |
            Q(tags__tag_name__icontains=q) |
            Q(notes__content__icontains=q)
        )

        # Only return conversations that have at least one message
        base_qs = base_qs.annotate(
            _msg_count=Count('messages')
        ).filter(_msg_count__gt=0)

        user = request.user
        if user.is_authenticated and not user.is_staff:
            try:
                profile = user.profile
                if profile and profile.group_id:
                    base_qs = base_qs.filter(group_id=profile.group_id)
            except Exception:
                return Response({'results': []})

            # Annotate personal pin so it can be referenced in the filter
            user_pin = ConversationUserPin.objects.filter(
                conversation=OuterRef('pk'),
                user=user,
            )
            base_qs = base_qs.annotate(
                _has_user_pin=Exists(user_pin),
            )

            # Exclude conversations taken by another human (non-bot, non-self),
            # unless the user personally pinned them
            other_human_takes = ConversationTake.objects.filter(
                conversation=OuterRef('pk'),
                expires_at__gt=now,
            ).exclude(created_by=user).exclude(created_by__username='bot')
            base_qs = base_qs.annotate(
                _has_other_human_take=Exists(other_human_takes)
            ).filter(
                Q(_has_other_human_take=False) | Q(_has_user_pin=True)
            )

        queryset = base_qs.distinct().order_by('-last_message_at', '-created_at').prefetch_related(
            Prefetch('tags', queryset=ConversationTag.objects.select_related('created_by__profile__group').filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))),
            Prefetch('notes', queryset=ConversationNote.objects.select_related('created_by__profile__group').filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))),
            Prefetch('takes', queryset=ConversationTake.objects.select_related('created_by__profile__group').filter(expires_at__gt=now)),
        )[:50]

        serializer = ConversationListSerializer(queryset, many=True)
        return Response({'results': serializer.data})

    @action(detail=True, methods=['post'])
    @transaction.atomic
    def send_template(self, request, pk=None):
        """Send a WhatsApp template to this conversation (admin only)."""

        from .serializers import SendTemplateSerializer
        serializer = SendTemplateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        template = WhatsAppTemplate.objects.get(id=serializer.validated_data['template_id'])
        parameters = serializer.validated_data.get('parameters', {})
        conversation = self.get_object()

        if not any(_resolve_whatsapp_target(conversation.contact_phone, conversation_id=conversation.id)):
            return Response({'error': 'No contact phone'}, status=status.HTTP_400_BAD_REQUEST)

        exclusion_key = conversation.contact_phone or conversation.whatsapp_id
        if template.category == 'MARKETING' and TemplateExclusion.objects.filter(contact_phone=exclusion_key).exists():
            return Response({'status': 'skipped', 'reason': 'El contacto solicitó no recibir más promos'})

        from .integrations.notify import build_template_payload
        payload = build_template_payload(template, parameters)

        message = Message.objects.create(
            conversation=conversation,
            direction='outbound',
            message_type='template',
            content=json.dumps(payload),
            sender_name=request.user.get_full_name() or request.user.username,
            sender=request.user,
        )

        conversation.last_message = f'[{template.name}]'
        conversation.last_message_at = timezone.now()
        conversation.save()

        conversation._last_msg_direction = 'outbound'
        if hasattr(conversation, '_prefetched_objects_cache'):
            conversation._prefetched_objects_cache.pop('takes', None)

        _enqueue_outbound_send(message)

        publish_conversation_update(conversation)
        return Response(MessageSerializer(message).data, status=status.HTTP_201_CREATED)


class MessageViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for reading messages"""
    serializer_class = MessageSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = Message.objects.select_related('conversation', 'context_message').filter(
            Q(metadata__status__isnull=True) | ~Q(metadata__status='cancelled')
        )

        if user.is_authenticated and not user.is_staff:
            from django.utils import timezone as tz
            now = tz.now()
            try:
                profile = user.profile
                if profile and profile.group_id:
                    qs = qs.filter(conversation__group_id=profile.group_id)
                else:
                    return Message.objects.none()
            except Exception:
                return Message.objects.none()
            if self.action == 'list':
                other_takes = ConversationTake.objects.filter(
                    conversation=OuterRef('conversation'),
                    expires_at__gt=now,
                ).exclude(created_by=user).exclude(created_by__username='bot')
                qs = qs.annotate(
                    _msg_other_take=Exists(other_takes)
                ).filter(_msg_other_take=False)

        return qs

    @action(detail=False, methods=['get'])
    def search(self, request):
        """Full-text message search. Returns grouped results with snippets."""
        q = request.query_params.get('q', '').strip()
        if not q:
            return Response({'error': 'Query parameter "q" is required'}, status=status.HTTP_400_BAD_REQUEST)

        limit = min(int(request.query_params.get('limit', 50)), 50)
        user = request.user
        now = timezone.now()

        from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector, TrigramSimilarity
        search_query = SearchQuery(q, config='spanish', search_type='websearch')
        vector = SearchVector('content', weight='A', config='spanish')

        qs = Message.objects.select_related('conversation').annotate(
            fts_rank=SearchRank(vector, search_query),
            trigram_sim=TrigramSimilarity('content', q),
        ).filter(
            Q(search_vector=search_query) | Q(trigram_sim__gt=0.15)
        )

        if not user.is_staff:
            try:
                profile = user.profile
                if profile and profile.group_id:
                    qs = qs.filter(conversation__group_id=profile.group_id)
                else:
                    return Response({'results': [], 'total': 0, 'has_more': False})
            except Exception:
                return Response({'results': [], 'total': 0, 'has_more': False})
            other_takes = ConversationTake.objects.filter(
                conversation=OuterRef('conversation'),
                expires_at__gt=now,
            ).exclude(created_by=user).exclude(created_by__username='bot')
            qs = qs.annotate(_msg_other_take=Exists(other_takes)).filter(_msg_other_take=False)

        qs = qs.order_by('-fts_rank', '-trigram_sim')[:limit]

        results = []
        for msg in qs:
            contact_name = msg.conversation.custom_name or msg.conversation.contact_name
            snippet = msg.content[:200] if msg.content else ''
            rank_val = float(
                msg.fts_rank or 0
            ) + float(msg.trigram_sim or 0) * 0.5
            results.append({
                'message_id': msg.id,
                'conversation_id': msg.conversation_id,
                'contact_name': contact_name,
                'snippet': snippet,
                'created_at': msg.created_at,
                'rank': round(rank_val, 4),
            })

        return Response({
            'results': results,
            'total': len(results),
            'has_more': False,
        })

    @action(detail=False, methods=['post'])
    def suggestions(self, request):
        from .suggestions import search as suggestion_search

        conversation_id = request.data.get('conversation_id')
        partial_text = (request.data.get('partial_text') or '').strip()

        if not partial_text or len(partial_text) < 2:
            return Response({'suggestion': None, 'alternatives': []})

        tags = []
        if conversation_id:
            tags = list(
                ConversationTag.objects.filter(
                    conversation_id=conversation_id,
                    expires_at__gt=timezone.now(),
                ).values_list('tag_name', flat=True)
            )

        result = suggestion_search(partial_text, tags)
        if result is None:
            return Response({'suggestion': None, 'alternatives': []})
        return Response(result)

    @action(detail=True, methods=['patch'])
    def cancel(self, request, pk=None):
        message = self.get_object()
        if message.direction != 'outbound':
            return Response({'detail': 'Only outbound messages can be cancelled'}, status=status.HTTP_400_BAD_REQUEST)
        if message.metadata.get('status') != 'pending':
            return Response({'detail': 'Message is no longer cancellable'}, status=status.HTTP_409_CONFLICT)
        message.metadata['status'] = 'cancelled'
        message.save(update_fields=['metadata'])
        publish_conversation_update(message.conversation, MessageSerializer(message).data)
        return Response({'detail': 'Message cancelled'})

    @staticmethod
    def _extract_location_metadata(metadata):
        if not metadata:
            return {}
        loc = metadata.get('location')
        if loc and 'latitude' in loc and 'longitude' in loc:
            return loc
        lat = metadata.get('latitude')
        lng = metadata.get('longitude')
        if lat is not None and lng is not None:
            return {
                'latitude': lat,
                'longitude': lng,
                'name': metadata.get('name', ''),
                'address': metadata.get('address', ''),
            }
        return {}

    @action(detail=True, methods=['post'])
    def forward(self, request, pk=None):
        """Forward a message to one or more conversations."""
        original = self.get_object()
        conversation_ids = request.data.get('conversation_ids', [])
        if not conversation_ids:
            return Response({'detail': 'conversation_ids is required'}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(conversation_ids, list):
            return Response({'detail': 'conversation_ids must be a list'}, status=status.HTTP_400_BAD_REQUEST)

        forwarded_type = original.message_type
        if forwarded_type in ('reaction', 'edit', 'interactive', 'button', 'template'):
            return Response({'detail': 'This message type cannot be forwarded'}, status=status.HTTP_400_BAD_REQUEST)

        base_content = f"*Reenviado*\n\n{original.content}" if original.content else "*Reenviado*"

        forwardable_types = ('text', 'image', 'video', 'audio', 'document', 'sticker', 'location')
        if forwarded_type not in forwardable_types:
            return Response({'detail': f'Cannot forward message type {forwarded_type}'}, status=status.HTTP_400_BAD_REQUEST)

        conv_qs = Conversation.objects.filter(id__in=conversation_ids)
        user = request.user
        if not user.is_staff:
            from django.utils import timezone as tz
            now = tz.now()
            try:
                profile = user.profile
                if profile and profile.group_id:
                    conv_qs = conv_qs.filter(group_id=profile.group_id)
                else:
                    return Response({'detail': 'No group access'}, status=status.HTTP_403_FORBIDDEN)
            except Exception:
                return Response({'detail': 'No group access'}, status=status.HTTP_403_FORBIDDEN)
            other_takes = ConversationTake.objects.filter(
                conversation=OuterRef('id'),
                expires_at__gt=now,
            ).exclude(created_by=user).exclude(created_by__username='bot')
            conv_qs = conv_qs.annotate(
                _other_take=Exists(other_takes)
            ).filter(_other_take=False)

        created_messages = []
        errors = []

        for conv in conv_qs:
            try:
                with transaction.atomic():
                    new_kwargs = {
                        'conversation': conv,
                        'direction': 'outbound',
                        'message_type': forwarded_type,
                        'sender_name': user.get_full_name() or user.username,
                        'sender': user,
                        'is_forwarded': True,
                        'context_message': original,
                    }

                    if forwarded_type == 'text':
                        new_kwargs['content'] = base_content
                    elif forwarded_type == 'location':
                        loc = self._extract_location_metadata(original.metadata)
                        new_kwargs['content'] = base_content
                        new_kwargs['metadata'] = {'location': loc} if loc else {}
                    else:
                        new_kwargs['content'] = base_content
                        if original.media_url:
                            new_kwargs['media_url'] = original.media_url
                        if original.metadata:
                            meta = dict(original.metadata)
                            new_kwargs['metadata'] = meta

                    new_msg = Message.objects.create(**new_kwargs)

                    conv.last_message = {
                        'text': base_content[:255],
                        'image': '[Image]',
                        'video': '[Video]',
                        'audio': '[Audio]',
                        'sticker': '[Sticker]',
                        'document': '[Document]',
                        'location': '[Location]',
                    }.get(forwarded_type, '[Forwarded]')
                    conv.last_message_at = timezone.now()
                    conv.save(update_fields=['last_message', 'last_message_at', 'updated_at'])

                    _enqueue_outbound_send(new_msg)

                    serializer = MessageSerializer(new_msg)
                    created_messages.append(serializer.data)
                    publish_conversation_update(conv, serializer.data)

                    _log_audit(user, conv, 'forward_message',
                               f'Reenviado desde conversación {original.conversation_id} '
                               f'(msg {original.id}, type={forwarded_type})')
            except Exception as e:
                logger.exception('Error forwarding message %d to conversation %d', original.id, conv.id)
                errors.append({'conversation_id': conv.id, 'error': str(e)})

        return Response({
            'forwarded': len(created_messages),
            'messages': created_messages,
            'errors': errors if errors else None,
        })


from rest_framework.pagination import PageNumberPagination


class UserPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 500


class UserViewSet(viewsets.ModelViewSet):
    """ViewSet for managing user information"""
    queryset = User.objects.select_related('profile__group').all().order_by('id')
    serializer_class = UserSerializer
    pagination_class = UserPagination
    filter_backends = [SearchFilter]
    search_fields = ['username', 'first_name', 'last_name', 'email']

    def get_permissions(self):
        from rest_framework.permissions import IsAdminUser, IsAuthenticated
        if self.action in ['create', 'update', 'partial_update', 'destroy', 'list', 'retrieve']:
            return [IsAuthenticated(), IsAdminUser()]
        return [IsAuthenticated()]

    @action(detail=False, methods=['get'])
    def current_user(self, request):
        """Get current user information"""
        user = User.objects.select_related('profile__group').get(pk=request.user.pk)
        serializer = self.get_serializer(user)
        return Response(serializer.data)


class CityGroupViewSet(viewsets.ModelViewSet):
    """Manage city groups. Admins can create/update/delete."""
    queryset = CityGroup.objects.filter(is_active=True)
    serializer_class = CityGroupSerializer

    def get_permissions(self):
        from rest_framework.permissions import IsAdminUser
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            return [IsAuthenticated(), IsAdminUser()]
        return [IsAuthenticated()]


class BotExemptContactViewSet(viewsets.ModelViewSet):
    """Manage bot-exempt phone numbers. Write operations require admin."""
    queryset = BotExemptContact.objects.select_related('created_by').all()
    serializer_class = BotExemptContactSerializer

    def get_permissions(self):
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            return [IsAuthenticated(), IsAdminUser()]
        return [IsAuthenticated()]

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def perform_destroy(self, instance):
        from .models import Conversation, ConversationTag
        conversations = Conversation.objects.filter(contact_phone=instance.contact_phone)
        ConversationTag.objects.filter(
            conversation__in=conversations,
            tag_name="Domii",
            expires_at__isnull=True,
        ).delete()
        instance.delete()


class TemplateExclusionViewSet(viewsets.ModelViewSet):
    """Manage template exclusion list. Write operations require admin."""
    queryset = TemplateExclusion.objects.select_related('created_by').all()
    serializer_class = TemplateExclusionSerializer

    def get_permissions(self):
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            return [IsAuthenticated(), IsAdminUser()]
        return [IsAuthenticated()]

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class StickerAssetViewSet(viewsets.ModelViewSet):
    """Store and serve reusable sticker images."""

    serializer_class = StickerAssetSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_permissions(self):
        return [IsAuthenticated()]

    def get_queryset(self):
        return StickerAsset.objects.all()

    @action(detail=False, methods=['post'], parser_classes=[FormParser, MultiPartParser])
    def bulk_upload(self, request):
        files = request.FILES.getlist('images')
        if not files:
            return Response({'error': 'No se proporcionaron archivos'}, status=status.HTTP_400_BAD_REQUEST)

        name = request.data.get('name', '')
        stickers = []

        for f in files:
            sticker = StickerAsset.objects.create(
                name=name,
                image=f,
                created_by=request.user,
            )
            stickers.append(sticker)

        serializer = self.get_serializer(stickers, many=True)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'])
    def save_from_message(self, request):
        message_id = request.data.get('message_id')
        if not message_id:
            return Response({'error': 'message_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            message = Message.objects.select_related('conversation').get(id=message_id)
        except Message.DoesNotExist:
            return Response({'error': 'Message not found'}, status=status.HTTP_404_NOT_FOUND)

        user = request.user
        if not user.is_staff:
            try:
                profile = user.profile
                if not profile or not profile.group_id or message.conversation.group_id != profile.group_id:
                    return Response({'error': 'Message not found'}, status=status.HTTP_404_NOT_FOUND)
            except Exception:
                return Response({'error': 'Message not found'}, status=status.HTTP_404_NOT_FOUND)

        if message.message_type != 'sticker':
            return Response({'error': 'Message is not a sticker'}, status=status.HTTP_400_BAD_REQUEST)

        if not message.media_url:
            return Response({'error': 'No media available for this message'}, status=status.HTTP_400_BAD_REQUEST)

        file_path = _resolve_media_path(message.media_url)
        if not file_path:
            return Response({'error': 'Media file not yet downloaded, try again later'}, status=status.HTTP_400_BAD_REQUEST)

        from django.core.files.base import ContentFile
        import os

        with open(file_path, 'rb') as f:
            content = f.read()
        sticker = StickerAsset(created_by=user)
        sticker.image.save(os.path.basename(file_path), ContentFile(content), save=True)

        serializer = self.get_serializer(sticker)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save()

    def perform_destroy(self, instance):
        if instance.image:
            try:
                if os.path.isfile(instance.image.path):
                    os.remove(instance.image.path)
            except Exception:
                pass
        instance.delete()


class BotScheduleViewSet(viewsets.ModelViewSet):
    """Manage bot operating hours. Admin-only for write operations."""
    queryset = BotSchedule.objects.all()
    serializer_class = BotScheduleSerializer

    def get_permissions(self):
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            return [IsAuthenticated(), IsAdminUser()]
        return [IsAuthenticated()]


class BotConfigViewSet(viewsets.ReadOnlyModelViewSet):
    """View bot configuration settings."""
    queryset = BotConfig.objects.all()
    serializer_class = BotConfigSerializer

    def get_permissions(self):
        return [IsAuthenticated(), IsAdminUser()]

    @action(detail=True, methods=['patch'])
    def update_value(self, request, pk=None):
        config = self.get_object()
        value = request.data.get('value')
        if value is None:
            return Response({'detail': 'value is required'}, status=status.HTTP_400_BAD_REQUEST)
        config.value = value
        config.save(update_fields=['value'])
        # Apply the change immediately instead of waiting for the 30 s TTL.
        try:
            from .bot.config import _clear_cache

            _clear_cache()
        except Exception:
            pass
        return Response(BotConfigSerializer(config).data)


class WhatsAppTemplateViewSet(viewsets.ModelViewSet):
    """Manage WhatsApp message templates. Admins can create, list, sync, delete."""
    queryset = WhatsAppTemplate.objects.all()
    serializer_class = WhatsAppTemplateSerializer
    permission_classes = [IsAuthenticated, IsAdminUser]

    def create(self, request, *args, **kwargs):
        """Submit the template to Meta first, then persist it.

        A rejection never leaves a misleading row behind: the parsed Meta
        error is returned with a 400 (or 502 when Meta is unreachable) and
        the client keeps its payload to retry.
        """
        from . import whatsapp_templates
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            result = whatsapp_templates.create_template(
                name=data['name'],
                language=data['language'],
                category=data['category'],
                components=data['components'],
            )
        except whatsapp_templates.MetaTemplateError as exc:
            logger.warning(
                "Template %r (%s) rejected by Meta: %s %s",
                data['name'], data['language'], exc.message, exc.details,
            )
            return Response(
                {'detail': exc.message, 'meta_error': exc.details},
                status=exc.status_code,
            )

        template = serializer.save(
            template_id=str(result.get('id', '')),
            status=result.get('status', 'PENDING'),
        )
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def perform_destroy(self, instance):
        from . import whatsapp_templates
        if instance.name:
            whatsapp_templates.delete_template(name=instance.name)
        instance.delete()

    @action(detail=True, methods=['post'])
    def sync_status(self, request, pk=None):
        """Re-fetch template status and rejection reason from Meta."""
        from . import whatsapp_templates
        template = self.get_object()
        if template.template_id:
            result = whatsapp_templates.get_template(template.template_id)
            if result:
                template.status = result.get('status', template.status)
                template.rejection_reason = result.get('rejected_reason') or ''
                if result.get('quality_score'):
                    template.quality_score = result['quality_score']
                template.save(update_fields=['status', 'rejection_reason', 'quality_score'])
                return Response(WhatsAppTemplateSerializer(template).data)
            return Response({'error': 'Failed to sync with Meta'}, status=400)
        return Response({'error': 'No template_id to sync'}, status=400)

    @action(detail=False, methods=['post'])
    def sync_all(self, request):
        """Sync status and rejection reason of all templates from Meta."""
        from . import whatsapp_templates
        remote = whatsapp_templates.list_templates()
        updated = 0
        for rt in remote:
            try:
                template = WhatsAppTemplate.objects.get(name=rt.get('name', ''), language=rt.get('language', 'es'))
                old_status = template.status
                old_reason = template.rejection_reason
                template.status = rt.get('status', template.status)
                template.rejection_reason = rt.get('rejected_reason') or ''
                if old_status != template.status or old_reason != template.rejection_reason:
                    template.save(update_fields=['status', 'rejection_reason'])
                    updated += 1
            except WhatsAppTemplate.DoesNotExist:
                pass
        return Response({'synced': len(remote), 'updated': updated})

    @action(detail=False, methods=['post'], parser_classes=[FormParser, MultiPartParser])
    def upload_media(self, request):
        """Upload an image to Meta's phone-number media endpoint for use when SENDING"""
        uploaded_file = request.FILES.get('file')
        if not uploaded_file:
            return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)

        ext = os.path.splitext(uploaded_file.name)[1].lower()
        if ext not in ('.jpg', '.jpeg', '.png'):
            return Response({'error': 'Solo se permiten imágenes JPG o PNG'}, status=400)

        if uploaded_file.size > 5 * 1024 * 1024:
            return Response({'error': 'La imagen no debe superar los 5 MB'}, status=400)

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            for chunk in uploaded_file.chunks():
                tmp.write(chunk)
            tmp_path = tmp.name

        phone_number_id = getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', None) or settings.WHATSAPP_PHONE_NUMBER
        token = settings.WHATSAPP_API_TOKEN

        try:
            media_id = upload_media_to_whatsapp(tmp_path, phone_number_id, token)
            if media_id:
                return Response({'media_id': media_id, 'handle': media_id})
            return Response({'error': 'Error al subir la imagen a Meta'}, status=500)
        except Exception as e:
            return Response({'error': str(e)}, status=500)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    @action(detail=False, methods=['post'], parser_classes=[FormParser, MultiPartParser])
    def upload_template_header(self, request):
        """Upload an image to Meta's business account for use as a template CREATION header handle."""
        from . import whatsapp_templates
        uploaded_file = request.FILES.get('file')
        if not uploaded_file:
            return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)

        ext = os.path.splitext(uploaded_file.name)[1].lower()
        if ext not in ('.jpg', '.jpeg', '.png'):
            return Response({'error': 'Solo se permiten imágenes JPG o PNG'}, status=400)

        if uploaded_file.size > 5 * 1024 * 1024:
            return Response({'error': 'La imagen no debe superar los 5 MB'}, status=400)

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            for chunk in uploaded_file.chunks():
                tmp.write(chunk)
            tmp_path = tmp.name

        try:
            handle = whatsapp_templates.upload_template_media(tmp_path)
            if handle:
                return Response({'handle': handle})
            return Response({'error': 'Error al subir la imagen a Meta'}, status=500)
        except Exception as e:
            return Response({'error': str(e)}, status=500)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    @action(detail=False, methods=['post'])
    def bulk_send(self, request):
        """Send a template to conversations (admin only).
        Two modes: 'count' (last N conversations) or 'recipients' (CSV upload).
        """
        from .serializers import BulkSendTemplateSerializer
        serializer = BulkSendTemplateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        template = WhatsAppTemplate.objects.get(id=serializer.validated_data['template_id'])
        recipients = serializer.validated_data.get('recipients')
        parameter_sources = serializer.validated_data.get('parameter_sources', {})
        fixed_values = serializer.validated_data.get('fixed_values', {})
        header_media_id = request.data.get('header_media_id')
        is_marketing = template.category == 'MARKETING'

        def _build_components(resolved_params):
            components = []
            for comp in template.components:
                comp_type = comp.get('type')
                if comp_type == 'body':
                    params = []
                    if resolved_params:
                        for var_name, value in resolved_params.items():
                            params.append({
                                'type': 'text',
                                'parameter_name': var_name,
                                'text': value,
                            })
                    components.append({'type': 'body', 'parameters': params})
                elif comp_type == 'header' and comp.get('format') in ('image', 'video', 'document'):
                    if header_media_id:
                        components.append({
                            'type': 'header',
                            'parameters': [{'type': comp['format'], comp['format']: {'id': header_media_id}}],
                        })
                elif comp_type == 'buttons':
                    btn_components = []
                    for i, btn in enumerate(comp.get('buttons', [])):
                        if btn['type'] == 'url':
                            url_val = fixed_values.get(f'btn_{i}')
                            if url_val:
                                btn_components.append({
                                    'type': 'url',
                                    'text': btn['text'],
                                    'url': url_val,
                                })
                    if btn_components:
                        components.append({
                            'type': 'button', 'sub_type': 'url',
                            'index': '0', 'parameters': btn_components,
                        })
            return components

        def _send_to_conversation(conv, resolved_params):
            components = _build_components(resolved_params)
            payload = {
                'name': template.name,
                'language': {'code': template.language},
                'components': components,
            }
            message = Message.objects.create(
                conversation=conv,
                direction='outbound',
                message_type='template',
                content=json.dumps(payload),
                sender_name='Bot',
                sender=None,
            )
            conv.last_message = f'[{template.name}]'
            conv.last_message_at = timezone.now()
            conv.save(update_fields=['last_message', 'last_message_at'])
            conv._last_msg_direction = 'outbound'
            _enqueue_outbound_send(message)
            publish_conversation_update(conv)
            return 1

        def _normalize_phone(raw):
            cleaned = raw.strip()
            if cleaned.startswith('+'):
                cleaned = cleaned[1:]
            cleaned = ''.join(c for c in cleaned if c.isdigit())
            if cleaned.startswith('57') and len(cleaned) == 12:
                return cleaned
            if len(cleaned) == 10:
                return '57' + cleaned
            return None

        # ── Recipients (CSV) mode ──
        if recipients:
            queued = 0
            created = 0
            skipped = 0
            invalid = 0
            errors = []
            excluded_phones = set(TemplateExclusion.objects.values_list('contact_phone', flat=True)) if is_marketing else set()
            for idx, entry in enumerate(recipients):
                raw_phone = entry.get('phone', '').strip()
                row_params = entry.get('parameters', {})
                if not raw_phone:
                    invalid += 1
                    errors.append({'row': idx, 'phone': raw_phone, 'error': 'Teléfono vacío'})
                    continue
                phone = _normalize_phone(raw_phone)
                if not phone:
                    invalid += 1
                    errors.append({'row': idx, 'phone': raw_phone, 'error': 'Formato de teléfono inválido'})
                    continue
                if phone in excluded_phones:
                    skipped += 1
                    continue
                conv, is_new = Conversation.objects.get_or_create(
                    contact_phone=phone,
                    defaults={
                        'whatsapp_id': phone,
                        'contact_name': phone,
                        'group': get_default_group(),
                        'status': 'active',
                    },
                )
                if is_new:
                    created += 1
                try:
                    _send_to_conversation(conv, row_params)
                    queued += 1
                except Exception as e:
                    logger.exception('Error sending CSV row %d to %s', idx, phone)
                    invalid += 1
                    errors.append({'row': idx, 'phone': phone, 'error': str(e)})
            return Response({
                'queued': queued,
                'created': created,
                'skipped': skipped,
                'invalid': invalid,
                'total': len(recipients),
                'errors': errors,
                'template_name': template.name,
            })

        # ── Count (last N conversations) mode ──
        count = serializer.validated_data['count']

        def _resolve_params(conv):
            resolved = {}
            for var_name, source in parameter_sources.items():
                if source == 'contact_name':
                    resolved[var_name] = conv.contact_name or ''
                elif source == 'custom_name':
                    resolved[var_name] = conv.custom_name or conv.contact_name or ''
                elif source == 'contact_phone':
                    resolved[var_name] = conv.contact_phone or ''
                elif source == 'conversation_id':
                    resolved[var_name] = str(conv.id)
                elif source == 'fixed':
                    resolved[var_name] = fixed_values.get(var_name, '')
            return resolved

        excluded_phones = set(TemplateExclusion.objects.values_list('contact_phone', flat=True)) if is_marketing else set()

        base_qs = Conversation.objects.exclude(
            contact_phone__isnull=True,
        ).exclude(contact_phone='').order_by('-last_message_at')

        queued = 0
        skipped = 0
        offset = 0
        batch_size = max(count * 2, 100)

        while queued < count:
            batch = list(base_qs[offset:offset + batch_size])
            if not batch:
                break
            for conv in batch:
                if conv.contact_phone in excluded_phones:
                    skipped += 1
                    continue
                resolved_params = _resolve_params(conv)
                queued += _send_to_conversation(conv, resolved_params)
                if queued >= count:
                    break
            offset += batch_size

        return Response({
            'queued': queued,
            'skipped': skipped,
            'template_name': template.name,
            'total': queued + skipped,
        })

    @action(detail=False, methods=['get'])
    def approved(self, request):
        """Return only APPROVED templates (for sending UI)."""
        queryset = self.queryset.filter(status='APPROVED')
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """View audit log entries (admin only)."""
    queryset = AuditLog.objects.select_related('actor').all()
    serializer_class = AuditLogSerializer
    permission_classes = [IsAuthenticated, IsAdminUser]
    pagination_class = UserPagination

    def get_queryset(self):
        qs = super().get_queryset()
        conversation = self.request.query_params.get('conversation')
        actor = self.request.query_params.get('actor')
        action = self.request.query_params.get('action')
        gte = self.request.query_params.get('created_at__gte')
        lte = self.request.query_params.get('created_at__lte')
        if conversation:
            qs = qs.filter(conversation_id=conversation)
        if actor:
            qs = qs.filter(actor_id=actor)
        if action:
            qs = qs.filter(action=action)
        if gte:
            qs = qs.filter(created_at__gte=gte)
        if lte:
            qs = qs.filter(created_at__lte=lte)
        return qs


class CannedResponseViewSet(viewsets.ModelViewSet):
    """Manage canned responses. Regular users see own group + global; staff see all."""
    serializer_class = CannedResponseSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = UserPagination

    def get_queryset(self):
        user = self.request.user
        if user.is_staff:
            return CannedResponse.objects.select_related('group', 'created_by').all()
        try:
            profile = user.profile
            group_id = profile.group_id if profile else None
            if group_id:
                return CannedResponse.objects.select_related('group', 'created_by').filter(
                    Q(group_id=group_id) | Q(group__isnull=True)
                )
        except Exception:
            pass
        return CannedResponse.objects.select_related('group', 'created_by').filter(group__isnull=True)

    def perform_create(self, serializer):
        user = self.request.user
        group_id = None
        if not user.is_staff:
            try:
                group_id = user.profile.group_id
            except Exception:
                pass
        serializer.save(created_by=user, group_id=group_id)

    def get_permissions(self):
        if self.action in ('destroy',):
            return [IsAuthenticated(), IsAdminUser()]
        return [IsAuthenticated()]


# ── Agent Presence ────────────────────────────────────────────────────────


PRESENCE_REDIS_PREFIX = 'agent:presence'
PRESENCE_TTL = 120


def _get_presence_redis_key(user_id):
    return f'{PRESENCE_REDIS_PREFIX}:{user_id}'


@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def presence_heartbeat(request):
    """Update agent presence: refresh Redis TTL, update DB on status change."""
    status_value = request.data.get('status', 'online')
    if status_value not in ('online', 'away', 'offline'):
        status_value = 'online'

    redis = get_sync_redis()
    try:
        key = _get_presence_redis_key(request.user.id)
        redis.setex(key, PRESENCE_TTL, status_value)
    except Exception:
        logger.exception("Presence heartbeat Redis error")

    if status_value == 'offline':
        AgentPresence.objects.update_or_create(
            user=request.user,
            defaults={'status': 'offline'},
        )
    else:
        AgentPresence.objects.update_or_create(
            user=request.user,
            defaults={'status': status_value, 'heartbeat_interval': 30},
        )

    # Publish presence update via SSE (scoped to user's group for non-staff)
    from .realtime import publish
    group_id = None
    if not request.user.is_staff:
        try:
            group_id = request.user.profile.group_id
        except Exception:
            pass
    publish({
        'type': 'presence.update',
        'user_id': request.user.id,
        'username': request.user.username,
        'status': status_value,
    }, group_id=group_id)

    return JsonResponse({'status': 'ok'})


@api_view(['GET'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def presence_list(request):
    """List all agents in the user's group with their presence status."""
    user = request.user
    redis = get_sync_redis()

    # Get all presence keys from Redis
    presences = {}
    try:
        cursor = 0
        while True:
            cursor, keys = redis.scan(cursor, match=f'{PRESENCE_REDIS_PREFIX}:*')
            for key in keys:
                try:
                    uid = int(key.split(':')[-1])
                    status_val = redis.get(key)
                    if status_val:
                        presences[uid] = status_val.decode() if isinstance(status_val, bytes) else status_val
                except (ValueError, TypeError):
                    pass
            if cursor == 0:
                break
    except Exception:
        logger.exception("Presence list Redis error")

    # Query users — staff sees all, non-staff sees own group
    from django.contrib.auth.models import User
    users_qs = User.objects.select_related('profile__group', 'presence')
    if not user.is_staff:
        try:
            group_id = user.profile.group_id
            users_qs = users_qs.filter(profile__group_id=group_id)
        except Exception:
            users_qs = users_qs.filter(pk=user.pk)

    results = []
    for u in users_qs:
        status = presences.get(u.id, 'offline')
        last_seen = None
        try:
            if hasattr(u, 'presence') and u.presence:
                last_seen = u.presence.last_seen
        except Exception:
            pass
        results.append({
            'user_id': u.id,
            'username': u.username,
            'first_name': u.first_name,
            'status': status,
            'last_seen': last_seen,
        })

    return JsonResponse(results, safe=False)


# ── Push Subscriptions ───────────────────────────────────────────────────


@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def push_subscribe(request):
    """Create or update a push notification subscription."""
    endpoint = request.data.get('endpoint', '').strip()
    p256dh = request.data.get('keys', {}).get('p256dh', '')
    auth = request.data.get('keys', {}).get('auth', '')
    browser = request.data.get('browser', '')

    if not endpoint or not p256dh or not auth:
        return JsonResponse({'error': 'endpoint, keys.p256dh, and keys.auth are required'}, status=400)

    PushSubscription.objects.update_or_create(
        user=request.user,
        endpoint=endpoint,
        defaults={'p256dh': p256dh, 'auth': auth, 'browser': browser},
    )
    return JsonResponse({'status': 'subscribed'})


@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def push_unsubscribe(request):
    """Remove a push notification subscription."""
    endpoint = request.data.get('endpoint', '')
    if endpoint:
        PushSubscription.objects.filter(user=request.user, endpoint=endpoint).delete()
    else:
        PushSubscription.objects.filter(user=request.user).delete()
    return JsonResponse({'status': 'unsubscribed'})


# ── Template webhook handlers ─────────────────────────────────────────────


def _handle_template_status_webhook(value: dict):
    """Handle message_template_status_update webhook from Meta."""
    event = value.get('event', '')
    template_id = str(value.get('message_template_id', ''))
    reason = value.get('reason', '')
    template_name = value.get('message_template_name', '')
    template_lang = value.get('message_template_language', 'es')

    try:
        template = WhatsAppTemplate.objects.get(name=template_name, language=template_lang)
    except WhatsAppTemplate.DoesNotExist:
        logger.info("Template status webhook for unknown template %s (%s)", template_name, template_lang)
        return

    old_status = template.status
    template.status = event

    if event == 'REJECTED' and reason:
        template.rejection_reason = reason
        rejection_info = value.get('rejection_info', {})
        if rejection_info:
            parts = []
            if rejection_info.get('reason'):
                parts.append(rejection_info['reason'])
            if rejection_info.get('recommendation'):
                parts.append(rejection_info['recommendation'])
            if parts:
                template.rejection_reason = ' | '.join(parts)

    if event == 'APPROVED' and value.get('message_template_category'):
        template.category = value['message_template_category']

    template.template_id = template_id or template.template_id
    template.save()

    if old_status != event:
        try:
            from api.realtime import publish
            publish({'type': 'template.updated', 'template_id': template.id, 'status': event})
        except Exception:
            pass

    logger.info("Template %s status updated: %s -> %s", template.name, old_status, event)


def _handle_template_quality_webhook(value: dict):
    """Handle message_template_quality_update webhook from Meta."""
    template_id = str(value.get('message_template_id', ''))
    new_score = value.get('new_quality_score', '')
    template_name = value.get('message_template_name', '')
    template_lang = value.get('message_template_language', 'es')

    try:
        template = WhatsAppTemplate.objects.get(name=template_name, language=template_lang)
    except WhatsAppTemplate.DoesNotExist:
        logger.info("Template quality webhook for unknown template %s (%s)", template_name, template_lang)
        return

    template.quality_score = new_score
    template.save(update_fields=['quality_score'])
    logger.info("Template %s quality score: %s", template.name, new_score)


def _handle_template_category_webhook(value: dict):
    """Handle template_category_update webhook from Meta."""
    new_category = value.get('new_category', '')
    previous_category = value.get('previous_category', '')
    template_name = value.get('message_template_name', '')
    template_lang = value.get('message_template_language', 'es')

    try:
        template = WhatsAppTemplate.objects.get(name=template_name, language=template_lang)
    except WhatsAppTemplate.DoesNotExist:
        logger.info("Template category webhook for unknown template %s (%s)", template_name, template_lang)
        return

    if new_category:
        template.category = new_category
        template.save(update_fields=['category'])
    logger.info("Template %s category: %s -> %s", template.name, previous_category, new_category)


def _handle_template_components_webhook(value: dict):
    """Handle message_template_components_update webhook from Meta."""
    template_name = value.get('message_template_name', '')
    template_lang = value.get('message_template_language', 'es')
    template_id = str(value.get('message_template_id', ''))

    try:
        template = WhatsAppTemplate.objects.get(name=template_name, language=template_lang)
    except WhatsAppTemplate.DoesNotExist:
        logger.info("Template components webhook for unknown template %s (%s)", template_name, template_lang)
        return

    body_text = value.get('message_template_element', '')
    header_text = value.get('message_template_title', '')
    footer_text = value.get('message_template_footer', '')
    buttons_data = value.get('message_template_buttons', [])

    new_components = []
    if header_text:
        new_components.append({'type': 'header', 'format': 'text', 'text': header_text})
    if body_text:
        new_components.append({'type': 'body', 'text': body_text})
    if footer_text:
        new_components.append({'type': 'footer', 'text': footer_text})
    if buttons_data:
        buttons = []
        for b in buttons_data:
            btn = {'type': b.get('message_template_button_type', '').lower(), 'text': b.get('message_template_button_text', '')}
            if b.get('message_template_button_url'):
                btn['url'] = b['message_template_button_url']
            if b.get('message_template_button_phone_number'):
                btn['phone_number'] = b['message_template_button_phone_number']
            buttons.append(btn)
        if buttons:
            new_components.append({'type': 'buttons', 'buttons': buttons})

    if new_components and new_components != template.components:
        from .serializers import _ensure_stop_button
        if template.category == 'MARKETING':
            new_components = _ensure_stop_button(new_components)
        template.components = new_components
        template.template_id = template_id or template.template_id
        template.save(update_fields=['components', 'template_id'])
        logger.info("Template %s components updated from webhook", template.name)


# --- Call webhook handlers ---

def _resolve_conversation(metadata, contacts, call_event):
    business_number = metadata.get('display_phone_number', '')
    to_number = call_event.get('to', '')
    from_number = call_event.get('from', '')
    from_user_id = call_event.get('from_user_id', '')
    to_user_id = call_event.get('to_user_id', '')
    call_id = call_event.get('id', '')

    user_phone = ''
    if to_number and to_number != business_number:
        user_phone = to_number
    elif from_number and from_number != business_number:
        user_phone = from_number

    user_bsuid = from_user_id or to_user_id or ''

    identifiers = [user_phone, user_bsuid]
    for c in contacts:
        wa = c.get('wa_id', '')
        uid = c.get('user_id', '')
        if wa and wa not in (business_number, ''):
            identifiers.append(wa)
        if uid and uid not in (business_number, ''):
            identifiers.append(uid)

    for ident in identifiers:
        if not ident:
            continue
        conv = Conversation.objects.filter(whatsapp_id=ident).first()
        if conv:
            return conv

    wa_id = user_phone or user_bsuid or call_id[:20]
    contact_name = ''
    for c in contacts:
        cid = c.get('wa_id', '') or c.get('user_id', '')
        if cid and cid in identifiers:
            contact_name = c.get('profile', {}).get('name', wa_id)
            break

    return Conversation.objects.create(
        whatsapp_id=wa_id,
        contact_name=contact_name or wa_id,
        contact_phone=user_phone if user_phone and user_phone.isdigit() else None,
        group=get_default_group(),
    )


def _serialize_call_for_sse(call):
    now = timezone.now()
    active_take = ConversationTake.objects.filter(
        conversation=call.conversation,
        expires_at__gt=now,
    ).select_related('created_by').first()

    take_info = None
    if active_take and active_take.created_by:
        take_info = {
            'created_by_id': active_take.created_by.id,
            'created_by_username': active_take.created_by.username,
            'created_by_first_name': active_take.created_by.first_name,
        }

    data = CallSerializer(call).data
    data['active_take'] = take_info
    return data


def _publish_call_event(call, event_type):
    try:
        payload = {
            'type': f'call.{event_type}',
            'call': _serialize_call_for_sse(call),
        }
        group_id = call.conversation.group_id if call.conversation_id else None
        from api.realtime import publish
        publish(payload, group_id=group_id)
    except Exception:
        logger.exception("Failed to publish call SSE event type=%s", event_type)


def _handle_call_webhook(call_event, metadata, contacts):
    call_id = call_event.get('id')
    event_type = call_event.get('event')
    direction = call_event.get('direction')

    if not call_id:
        return

    conversation = _resolve_conversation(metadata, contacts, call_event)
    deeplink = call_event.get('deeplink_payload', '')
    cta = call_event.get('cta_payload', '')

    if event_type == 'connect':
        session = call_event.get('session', {})
        sdp_content = session.get('sdp', '')
        sdp_type = session.get('sdp_type', '')
        direction_db = 'inbound' if direction == 'USER_INITIATED' else 'outbound'
        biz_data = call_event.get('biz_opaque_callback_data', '')

        if direction_db == 'inbound':
            call = Call.objects.create(
                call_id=call_id,
                conversation=conversation,
                direction='inbound',
                status='pending',
                from_number=call_event.get('from', ''),
                to_number=call_event.get('to', ''),
                sdp_offer=sdp_content,
                biz_opaque_callback_data=biz_data,
                deeplink_payload=deeplink,
                cta_payload=cta,
            )
            _publish_call_event(call, 'incoming')
        else:
            call = Call.objects.filter(call_id=call_id).first()
            if call:
                call.sdp_answer = sdp_content
                call.status = 'ringing'
                call.deeplink_payload = deeplink or call.deeplink_payload
                call.cta_payload = cta or call.cta_payload
                call.save()
                _publish_call_event(call, 'outgoing_accepted')

    elif event_type == 'terminate':
        call = Call.objects.filter(call_id=call_id).first()
        if not call:
            return

        status_value = call_event.get('status', [])
        start_time_ts = call_event.get('start_time')
        end_time_ts = call_event.get('end_time')
        duration_val = call_event.get('duration')
        errors = call_event.get('errors', [])

        if isinstance(status_value, list):
            status_value = status_value[0] if status_value else 'completed'

        if status_value == 'Completed':
            call.status = 'completed'
        elif status_value == 'Failed':
            call.status = 'failed'
        else:
            call.status = 'completed' if call.status == 'connected' else 'failed'

        if start_time_ts:
            try:
                call.start_time = datetime.fromtimestamp(
                    int(start_time_ts), tz=dt_timezone.utc
                )
            except (ValueError, OSError):
                pass
        if end_time_ts:
            try:
                call.end_time = datetime.fromtimestamp(
                    int(end_time_ts), tz=dt_timezone.utc
                )
            except (ValueError, OSError):
                pass
        call.duration_seconds = duration_val

        if deeplink:
            call.deeplink_payload = deeplink
        if cta:
            call.cta_payload = cta

        if errors:
            first_err = errors[0] if isinstance(errors, list) else errors
            call.error_code = first_err.get('code')
            call.error_message = first_err.get('message')

        call.save()
        _publish_call_event(call, 'terminated')

    elif event_type == 'call_recording_available':
        logger.info("call_recording_available webhook received for call %s", call_id)
        call = Call.objects.filter(call_id=call_id).first()
        if not call:
            logger.warning("call_recording_available: Call %s not found in DB", call_id)
            return

        rec = call_event.get('call_recording', {})
        audio = rec.get('audio', {})
        call.recording_audio_id = audio.get('id')
        call.recording_audio_url = audio.get('url')
        call.recording_audio_sha256 = audio.get('sha256')
        call.recording_audio_mime_type = audio.get('mime_type')

        logger.info(
            "call_recording_available %s: audio_id=%s mime_type=%s sha256=%s url=%s",
            call_id, audio.get('id'), audio.get('mime_type'), audio.get('sha256'),
            audio.get('url', '')[:80],
        )

        call.save()
        _publish_call_event(call, 'recording_available')

        cdn_url = audio.get('url')
        if cdn_url and settings.WHATSAPP_API_TOKEN:
            try:
                token = settings.WHATSAPP_API_TOKEN
                logger.info("call_recording_available %s: downloading from CDN...", call_id)
                req = urllib.request.Request(cdn_url, headers={'Authorization': f'Bearer {token}'})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                file_size = len(raw)
                ext = '.ogg'
                filename = f"{uuid.uuid4().hex}{ext}"
                local_dir = os.path.join(settings.MEDIA_ROOT, 'recordings')
                os.makedirs(local_dir, exist_ok=True)
                local_path = os.path.join(local_dir, filename)
                with open(local_path, 'wb') as f:
                    f.write(raw)
                call.recording_local_path = f"{settings.MEDIA_URL}recordings/{filename}"
                call.save(update_fields=['recording_local_path'])
                logger.info(
                    "call_recording_available %s: saved %d bytes to %s",
                    call_id, file_size, call.recording_local_path,
                )
            except Exception as e:
                logger.error("Failed to download call recording %s: %s", call_id, e, exc_info=True)


def _handle_call_status_webhook(status_event, metadata):
    call_id = status_event.get('id')
    status_value = status_event.get('status')

    if not call_id or not status_value:
        return

    call = Call.objects.filter(call_id=call_id).first()
    if not call:
        return

    status_map = {
        'RINGING': 'ringing',
        'ACCEPTED': 'connected',
        'REJECTED': 'rejected',
    }
    new_status = status_map.get(status_value, call.status)

    if new_status == 'connected' and not call.start_time:
        ts = status_event.get('timestamp')
        if ts:
            try:
                call.start_time = datetime.fromtimestamp(int(ts), tz=dt_timezone.utc)
            except (ValueError, OSError):
                pass

    call.status = new_status
    call.save()

    event_type_map = {
        'RINGING': 'ringing',
        'ACCEPTED': 'connected',
        'REJECTED': 'rejected',
    }
    _publish_call_event(call, event_type_map.get(status_value, 'updated'))


_DELIVERY_STATUS_RANK = {'sent': 1, 'delivered': 2, 'read': 3, 'played': 4}
_DELIVERY_STATUSES = frozenset(('sent', 'delivered', 'read', 'played'))


def _status_timestamp(status_event):
    ts = status_event.get('timestamp')
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=dt_timezone.utc).isoformat()
    except (ValueError, OSError, TypeError):
        return None


def _handle_message_status_webhook(status_event):
    """Record Meta message delivery statuses (sent/delivered/read/played/failed).

    Inspect-only: nothing is ever sent back to Meta, so the customer never
    learns whether the business read their message. Webhook order is not
    guaranteed (Meta can emit sent/delivered/read out of order), so statuses
    only move forward: a late `sent` can never downgrade a `read`. `failed`
    is terminal unless the message was already read. `deleted` and `warning`
    are recorded without touching the delivery status.
    """
    wamid = status_event.get('id', '')
    status_value = status_event.get('status', '')
    errors = status_event.get('errors', [])
    recipient = (
        status_event.get('recipient_id')
        or status_event.get('recipient_user_id')
        or ''
    )
    logger.info(
        'WhatsApp message status: id=%s status=%s recipient=%s errors=%s',
        wamid, status_value, recipient, errors,
    )
    if not wamid:
        return
    if status_value not in ('sent', 'delivered', 'read', 'played', 'failed', 'deleted', 'warning'):
        return

    msg = (
        Message.objects.filter(whatsapp_message_id=wamid)
        .select_related('conversation')
        .first()
    )
    if not msg:
        logger.warning('WhatsApp message status for unknown wamid: %s %s', wamid, status_value)
        return

    known_targets = {
        str(msg.conversation.contact_phone or ''),
        str(getattr(msg.conversation, 'whatsapp_id', '') or ''),
    }
    if recipient and str(recipient) not in known_targets:
        logger.warning(
            'Status recipient mismatch: msg=%s wamid=%s status_recipient=%s conv_phone=%s conv_whatsapp_id=%s',
            msg.id, wamid, recipient, msg.conversation.contact_phone,
            getattr(msg.conversation, 'whatsapp_id', ''),
        )

    meta = dict(msg.metadata or {})
    current = meta.get('delivery_status')
    current_rank = _DELIVERY_STATUS_RANK.get(current, 0)

    if status_value in _DELIVERY_STATUSES:
        if current == 'failed':
            logger.info('Ignoring %s for msg=%s: message already failed', status_value, msg.id)
            return
        if _DELIVERY_STATUS_RANK[status_value] <= current_rank:
            logger.info(
                'Ignoring out-of-order status %s for msg=%s (current=%s)',
                status_value, msg.id, current,
            )
            return
        meta['delivery_status'] = status_value
        stamp = _status_timestamp(status_event)
        if stamp:
            meta[status_value + '_at'] = stamp
    elif status_value == 'failed':
        if current_rank >= _DELIVERY_STATUS_RANK['read']:
            logger.info('Ignoring failed status for msg=%s: already %s', msg.id, current)
            return
        meta['delivery_status'] = 'failed'
        meta['status'] = 'failed'
        meta['send_errors'] = errors
        first = errors[0] if errors else {}
        meta['send_error'] = first.get('message') or first.get('title') or 'WhatsApp delivery failed'
        meta['send_error_code'] = first.get('code')
        stamp = _status_timestamp(status_event)
        if stamp:
            meta['failed_at'] = stamp
    elif status_value == 'deleted':
        meta['deleted'] = True
        stamp = _status_timestamp(status_event)
        if stamp:
            meta['deleted_at'] = stamp
    else:  # warning
        meta['delivery_warning'] = errors or True
        stamp = _status_timestamp(status_event)
        if stamp:
            meta['warning_at'] = stamp

    if meta == (msg.metadata or {}):
        return

    msg.metadata = meta
    msg.save(update_fields=['metadata'])
    publish_conversation_update(msg.conversation, MessageSerializer(msg).data)


# Webhook endpoint for WhatsApp (verification + incoming messages)
def _remove_template_exclusion(wa_id, conversation, contact_name, via):
    """Remove a contact from the template exclusion list and log it."""
    deleted, _ = TemplateExclusion.objects.filter(contact_phone=wa_id).delete()
    if deleted:
        logger.info("Template exclusion removed for %s %s", wa_id, via)
        audit_actor = User.objects.filter(username='bot').first() or User.objects.filter(is_staff=True).first()
        if audit_actor:
            AuditLog.objects.create(
                actor=audit_actor,
                conversation=conversation,
                action='toggle_status',
                detail=f"Reactivado en plantillas {via}: {wa_id}",
            )
    return deleted


@csrf_exempt
def whatsapp_webhook(request):
    # Verification (GET)
    if request.method == 'GET':
        mode = request.GET.get('hub.mode') or request.GET.get('mode')
        verify_token = request.GET.get('hub.verify_token') or request.GET.get('verify_token')
        challenge = request.GET.get('hub.challenge') or request.GET.get('challenge')

        if not challenge:
            return HttpResponse(status=400)

        expected_token = (settings.WEBHOOK_TOKEN or '')
        if len(expected_token) >= 2 and expected_token[0] == expected_token[-1] and expected_token[0] in '"\'':
            expected_token = expected_token[1:-1]
        if mode == 'subscribe' and expected_token and verify_token == expected_token:
            return HttpResponse(challenge)

        return HttpResponse(status=403)

    # Incoming messages (POST)
    if request.method == 'POST':
        raw_body = request.body
        webhook_start = time.time()
        try:
            payload = json.loads(raw_body.decode('utf-8'))
        except Exception:
            return HttpResponse(status=400)

        # Verify HMAC-SHA256 signature
        app_secret = getattr(settings, 'WHATSAPP_APP_SECRET', '')
        if app_secret:
            signature = request.META.get('HTTP_X_HUB_SIGNATURE_256', '')
            if not signature.startswith('sha256='):
                logger.warning('Webhook POST rejected: missing or invalid signature header')
                return HttpResponse(status=403)
            expected_sig = hmac.new(
                app_secret.encode('utf-8'),
                raw_body,
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected_sig, signature[len('sha256='):]):
                logger.warning('Webhook POST rejected: HMAC signature mismatch')
                return HttpResponse(status=403)

        try:
            entries = payload.get('entry', [])
            changes = entries[0].get('changes', []) if entries else []
            value = changes[0].get('value', {}) if changes else payload.get('value', {})
            field = changes[0].get('field', '') if changes else ''

            # --- Template webhooks ---
            if field == 'message_template_status_update':
                _handle_template_status_webhook(value)
                return JsonResponse({'status': 'template_status_handled'}, status=200)
            if field == 'message_template_quality_update':
                _handle_template_quality_webhook(value)
                return JsonResponse({'status': 'template_quality_handled'}, status=200)
            if field == 'template_category_update':
                _handle_template_category_webhook(value)
                return JsonResponse({'status': 'template_category_handled'}, status=200)
            if field == 'message_template_components_update':
                _handle_template_components_webhook(value)
                return JsonResponse({'status': 'template_components_handled'}, status=200)

            messages = value.get('messages', [])
            calls = value.get('calls', [])
            statuses = value.get('statuses', [])
            contacts = value.get('contacts', [])

            # Process call events before early-return on empty messages
            for call_event in calls:
                _handle_call_webhook(call_event, value.get('metadata', {}), contacts)

            for status_event in statuses:
                _handle_call_status_webhook(status_event, value.get('metadata', {}))
                _handle_message_status_webhook(status_event)
                # Handle typing indicator
                s = status_event.get('status', '')
                if s == 'typing':
                    try:
                        conv_id = status_event.get('conversation', {}).get('id')
                        if conv_id:
                            conv = Conversation.objects.filter(whatsapp_id=conv_id).first()
                            if conv:
                                redis = get_sync_redis()
                                typing_key = f'typing:{conv.id}'
                                try:
                                    redis.setex(typing_key, 8, '1')
                                except Exception:
                                    pass
                                from .realtime import publish
                                publish({
                                    'type': 'conversation.typing',
                                    'conversation_id': conv.id,
                                    'typing': True,
                                }, group_id=conv.group_id)
                    except Exception:
                        logger.exception("Error handling typing indicator")

            if not messages:
                return JsonResponse({'status': 'no_message'}, status=200)

            for msg in messages:
                msg_type = msg.get('type', 'text')
                sender = msg.get('from') or msg.get('from_user_id')
                msg_id = msg.get('id')

                sender_contact = next((c for c in contacts if c.get('wa_id') == sender or c.get('user_id') == sender), None)
                contact_info = sender_contact or (contacts[0] if contacts else {})
                profile = contact_info.get('profile', {})

                contact_name = profile.get('name', sender)
                raw_username = contact_info.get('username') or profile.get('username') or ''
                whatsapp_username = raw_username.lstrip('@') or None
                wa_id = contact_info.get('wa_id') or contact_info.get('user_id') or sender

                conversation, created = Conversation.objects.get_or_create(
                    whatsapp_id=wa_id,
                    defaults={
                        'contact_name': contact_name,
                        'contact_phone': wa_id if wa_id and wa_id.isdigit() else None,
                        'whatsapp_username': whatsapp_username,
                        'group': get_default_group(),
                    }
                )

                if not created:
                    updated = False
                    if contact_name and conversation.contact_name != contact_name:
                        conversation.contact_name = contact_name
                        updated = True
                    if whatsapp_username and conversation.whatsapp_username != whatsapp_username:
                        conversation.whatsapp_username = whatsapp_username
                        updated = True
                    if wa_id and wa_id.isdigit() and conversation.contact_phone != wa_id:
                        conversation.contact_phone = wa_id
                        updated = True
                    if updated:
                        conversation.save(update_fields=['contact_name', 'whatsapp_username', 'contact_phone', 'updated_at'])

                if not conversation.custom_name:
                    existing_custom = Conversation.objects.filter(
                        whatsapp_id=wa_id
                    ).exclude(custom_name__isnull=True).exclude(custom_name='').values_list('custom_name', flat=True).first()
                    if existing_custom:
                        conversation.custom_name = existing_custom
                        conversation.save(update_fields=['custom_name'])

                # Auto-link to an ops client for unknown phones (daemon thread,
                # never blocks the webhook).
                if not conversation.ops_client_user_id and wa_id and wa_id.isdigit():
                    schedule_auto_link(conversation.id, wa_id)

                if BotExemptContact.objects.filter(contact_phone=wa_id).exists():
                    if not conversation.tags.filter(tag_name="Domii", expires_at__isnull=True).exists():
                        try:
                            bot_user = User.objects.filter(username="bot").first()
                            ConversationTag.create_tag(
                                conversation=conversation,
                                tag_name="Domii",
                                expiry_type="never",
                                created_by=bot_user,
                                tag_color="gray",
                            )
                        except Exception:
                            logger.exception("Failed to auto-tag Domii exempt contact")

                content = ''
                media_url = None
                meta = {}
                last_msg_text = ''
                raw_media = None
                media_type_for_download = None

                if msg_type == 'text':
                    if 'text' in msg and isinstance(msg['text'], dict):
                        content = msg['text'].get('body', '')
                    elif isinstance(msg.get('text'), str):
                        content = msg['text']
                    last_msg_text = content

                elif msg_type == 'image':
                    img = msg.get('image', {})
                    content = img.get('caption', '')
                    raw_media = img.get('url') or img.get('id') or ''
                    media_type_for_download = 'image'
                    media_url = raw_media if raw_media.startswith('http') else ''
                    meta = {
                        'mime_type': img.get('mime_type', ''),
                        'sha256': img.get('sha256', ''),
                        'media_id': img.get('id', ''),
                    }
                    last_msg_text = content or 'Image'

                elif msg_type == 'video':
                    vid = msg.get('video', {})
                    content = vid.get('caption', '')
                    raw_media = vid.get('url') or vid.get('id') or ''
                    media_type_for_download = 'video'
                    media_url = raw_media if raw_media.startswith('http') else ''
                    meta = {
                        'mime_type': vid.get('mime_type', ''),
                        'sha256': vid.get('sha256', ''),
                        'media_id': vid.get('id', ''),
                    }
                    last_msg_text = content or 'Video'

                elif msg_type == 'sticker':
                    stk = msg.get('sticker', {})
                    raw_media = stk.get('url') or stk.get('id') or ''
                    media_type_for_download = 'sticker'
                    media_url = raw_media if raw_media.startswith('http') else ''
                    meta = {
                        'mime_type': stk.get('mime_type', ''),
                        'sha256': stk.get('sha256', ''),
                        'media_id': stk.get('id', ''),
                    }
                    last_msg_text = 'Sticker'

                elif msg_type == 'document':
                    doc = msg.get('document', {})
                    content = doc.get('caption', '')
                    raw_media = doc.get('url') or doc.get('id') or ''
                    media_type_for_download = 'document'
                    media_url = raw_media if raw_media.startswith('http') else ''
                    meta = {
                        'mime_type': doc.get('mime_type', ''),
                        'sha256': doc.get('sha256', ''),
                        'media_id': doc.get('id', ''),
                        'filename': doc.get('filename', ''),
                    }
                    last_msg_text = content or 'Document'

                elif msg_type == 'audio':
                    aud = msg.get('audio', {})
                    raw_media = aud.get('url') or aud.get('id') or ''
                    media_type_for_download = 'audio'
                    media_url = raw_media if raw_media.startswith('http') else ''
                    meta = {
                        'mime_type': aud.get('mime_type', ''),
                        'sha256': aud.get('sha256', ''),
                        'media_id': aud.get('id', ''),
                        'voice': aud.get('voice', False),
                    }
                    last_msg_text = 'Mensaje de voz' if meta.get('voice') else 'Audio'

                elif msg_type == 'location':
                    loc = msg.get('location', {})
                    content = loc.get('name') or loc.get('address', '')
                    meta = {
                        'latitude': loc.get('latitude'),
                        'longitude': loc.get('longitude'),
                        'name': loc.get('name', ''),
                        'address': loc.get('address', ''),
                        'url': loc.get('url', ''),
                    }
                    last_msg_text = content or 'Location'

                elif msg_type == 'reaction':
                    rxn = msg.get('reaction', {})
                    emoji = rxn.get('emoji')
                    target_wamid = rxn.get('message_id', '')
                    meta = {
                        'message_id': target_wamid,
                        'emoji': emoji,
                    }
                    if target_wamid:
                        target_msg = Message.objects.filter(
                            conversation=conversation,
                            whatsapp_message_id=target_wamid
                        ).first()
                        if target_msg:
                            meta['target_message_id'] = target_msg.id
                    last_msg_text = f'Reaccionó {emoji}' if emoji else 'Reacción eliminada'

                elif msg_type == 'edit':
                    edt = msg.get('edit', {})
                    meta = {
                        'original_message_id': edt.get('original_message_id', ''),
                        'new_message': edt.get('message', {}),
                    }
                    nm = edt.get('message', {})
                    nm_type = nm.get('type', '')
                    if nm_type == 'text' and isinstance(nm.get('text'), dict):
                        last_msg_text = nm['text'].get('body', '') or 'Edited message'
                    elif nm_type in ('image', 'video'):
                        last_msg_text = nm.get(nm_type, {}).get('caption', '') or f'Edited {nm_type}'
                    else:
                        last_msg_text = 'Edited message'

                elif msg_type == 'interactive':
                    inter = msg.get('interactive', {})
                    itype = inter.get('type', '')
                    if itype == 'list_reply':
                        lr = inter.get('list_reply', {})
                        content = lr.get('id', '')
                        meta = {'interactive_type': 'list_reply', 'interactive_reply': lr}
                        last_msg_text = lr.get('title', content)
                    elif itype == 'button_reply':
                        br = inter.get('button_reply', {})
                        content = br.get('id', '')
                        meta = {'interactive_type': 'button_reply', 'interactive_reply': br}
                        last_msg_text = br.get('title', content)
                    else:
                        content = ''
                        meta = {'interactive_type': itype}
                        last_msg_text = 'Interactive'

                elif msg_type == 'button':
                    button = msg.get('button', {})
                    content = button.get('text', '') or button.get('payload', '')
                    meta = {
                        'interactive_type': 'button_reply',
                        'interactive_reply': button,
                    }
                    last_msg_text = content or 'Button'

                else:
                    content = msg.get('text', {}).get('body', '') if isinstance(msg.get('text'), dict) else msg.get('text', '')
                    last_msg_text = content or msg_type

                if hasattr(content, '__iter__') and not isinstance(content, str):
                    content = str(content)

                context_message_obj = None
                if 'context' in msg:
                    meta['context'] = msg['context']
                    ctx = msg['context']
                    if ctx.get('forwarded'):
                        meta['is_forwarded'] = True
                        if ctx.get('frequently_forwarded'):
                            meta['is_frequently_forwarded'] = True
                    ctx_wamid = msg['context'].get('id', '')
                    if ctx_wamid:
                        ctx_msg = Message.objects.filter(
                            conversation=conversation,
                            whatsapp_message_id=ctx_wamid,
                        ).first()
                        if ctx_msg:
                            context_message_obj = ctx_msg
                        else:
                            logger.info('Context lookup failed: message %s has context.id %s but no matching message in conversation %s', msg_id, ctx_wamid, conversation.id)
                            ctx_sig = _wamid_msg_sig(ctx_wamid)
                            if ctx_sig:
                                ctx_msg = Message.objects.filter(
                                    conversation=conversation,
                                    metadata___msg_sig=ctx_sig,
                                ).first()
                                if not ctx_msg:
                                    # Backfill: compute signatures on-the-fly for recent messages
                                    for bf_msg in Message.objects.filter(
                                        conversation=conversation,
                                        whatsapp_message_id__isnull=False,
                                    ).order_by('-created_at')[:50]:
                                        bf_sig = _wamid_msg_sig(bf_msg.whatsapp_message_id)
                                        if bf_sig == ctx_sig:
                                            if not bf_msg.metadata:
                                                bf_msg.metadata = {}
                                            bf_msg.metadata['_msg_sig'] = bf_sig
                                            bf_msg.save(update_fields=['metadata'])
                                            ctx_msg = bf_msg
                                            break
                                if ctx_msg:
                                    logger.info('Context resolved by WAMID signature: message %s matched to message %s', msg_id, ctx_msg.id)
                                    context_message_obj = ctx_msg

                if msg_id:
                    meta['_msg_sig'] = _wamid_msg_sig(msg_id)
                    dedup_key = f"wamid_dedup:{msg_id}"
                    if not cache.add(dedup_key, True, 86400):
                        logger.info('Skipping duplicate message %s (redis cache)', msg_id)
                        continue
                    if Message.objects.filter(whatsapp_message_id=msg_id).exists():
                        logger.info('Skipping duplicate message %s', msg_id)
                        continue

                message = Message.objects.create(
                    conversation=conversation,
                    direction='inbound',
                    message_type=msg_type,
                    content=content,
                    sender_name=conversation.contact_name,
                    whatsapp_message_id=msg_id,
                    media_url=media_url,
                    metadata=meta,
                    context_message=context_message_obj,
                )

                if meta.get('is_forwarded'):
                    update_kwargs = {'is_forwarded': True}
                    if meta.get('is_frequently_forwarded'):
                        update_kwargs['is_frequently_forwarded'] = True
                    Message.objects.filter(id=message.id).update(**update_kwargs)
                    message.refresh_from_db()

                msg_ts = None
                wa_ts = msg.get('timestamp')
                if wa_ts:
                    try:
                        from datetime import datetime, timezone as dt_timezone
                        msg_ts = datetime.fromtimestamp(int(wa_ts), tz=dt_timezone.utc)
                        Message.objects.filter(id=message.id).update(created_at=msg_ts)
                        message.refresh_from_db()
                    except (ValueError, OSError):
                        pass

                if msg_ts and (conversation.last_message_at is None or msg_ts > conversation.last_message_at):
                    conversation.last_message = last_msg_text
                    conversation.last_message_at = msg_ts
                    conversation.save()

                conversation._last_msg_direction = 'inbound'

                elapsed = time.time() - webhook_start
                logger.info("Webhook msg %s: %.3fs from receipt to SSE publish (last_msg=%s)", message.id, elapsed, last_msg_text)
                publish_conversation_update(conversation, MessageSerializer(message).data)

                if msg_type in ('button', 'interactive') and last_msg_text:
                    from api.bot.constants import (
                        STOP_TEMPLATE_BUTTON_TEXT, STOP_TEMPLATE_CONFIRMATION_TEXT,
                        REACTIVATE_BUTTON_TEXT, REACTIVATE_BUTTON_ID,
                        REACTIVATE_PROMPT_TEXT, REACTIVATE_CONFIRMATION_TEXT,
                    )
                    if last_msg_text.strip() == STOP_TEMPLATE_BUTTON_TEXT:
                        _, created = TemplateExclusion.objects.get_or_create(
                            contact_phone=wa_id,
                            defaults={'contact_name': contact_name, 'source': 'stop_button'},
                        )
                        if created:
                            logger.info("Template exclusion added for %s via stop button", wa_id)
                            audit_actor = User.objects.filter(username='bot').first() or User.objects.filter(is_staff=True).first()
                            if audit_actor:
                                AuditLog.objects.create(
                                    actor=audit_actor,
                                    conversation=conversation,
                                    action='toggle_status',
                                    detail=f"Auto-excluido de plantillas vía botón: {wa_id}",
                                )
                            confirm_msg = Message.objects.create(
                                conversation=conversation,
                                direction='outbound',
                                message_type='text',
                                content=STOP_TEMPLATE_CONFIRMATION_TEXT,
                                sender_name='Bot',
                            )
                            conversation.last_message = STOP_TEMPLATE_CONFIRMATION_TEXT
                            conversation.last_message_at = timezone.now()
                            conversation.save(update_fields=['last_message', 'last_message_at'])
                            conversation._last_msg_direction = 'outbound'
                            send_whatsapp_outbound('text', STOP_TEMPLATE_CONFIRMATION_TEXT, wa_id, message_id=confirm_msg.id)
                            publish_conversation_update(conversation, MessageSerializer(confirm_msg).data)
                            reactivate_payload = {
                                'type': 'button',
                                'body': {
                                    'text': REACTIVATE_PROMPT_TEXT,
                                },
                                'action': {
                                    'buttons': [{
                                        'type': 'reply',
                                        'reply': {'id': REACTIVATE_BUTTON_ID, 'title': REACTIVATE_BUTTON_TEXT},
                                    }],
                                },
                            }
                            react_msg = Message.objects.create(
                                conversation=conversation,
                                direction='outbound',
                                message_type='interactive',
                                content=REACTIVATE_PROMPT_TEXT,
                                sender_name='Bot',
                                metadata={'interactive': reactivate_payload},
                            )
                            conversation.last_message = REACTIVATE_PROMPT_TEXT
                            conversation.last_message_at = timezone.now()
                            conversation.save(update_fields=['last_message', 'last_message_at'])
                            send_whatsapp_outbound('interactive', reactivate_payload, wa_id, message_id=react_msg.id)
                            publish_conversation_update(conversation, MessageSerializer(react_msg).data)
                    elif last_msg_text.strip() == REACTIVATE_BUTTON_TEXT:
                        _remove_template_exclusion(wa_id, conversation, contact_name, 'via reactivate button')
                        react_confirm = Message.objects.create(
                            conversation=conversation,
                            direction='outbound',
                            message_type='text',
                            content=REACTIVATE_CONFIRMATION_TEXT,
                            sender_name='Bot',
                        )
                        conversation.last_message = REACTIVATE_CONFIRMATION_TEXT
                        conversation.last_message_at = timezone.now()
                        conversation.save(update_fields=['last_message', 'last_message_at'])
                        conversation._last_msg_direction = 'outbound'
                        send_whatsapp_outbound('text', REACTIVATE_CONFIRMATION_TEXT, wa_id, message_id=react_confirm.id)
                        publish_conversation_update(conversation, MessageSerializer(react_confirm).data)
                elif msg_type == 'text' and content and ' '.join(content.strip().upper().split()) == 'REACTIVAR PROMOS':
                    from api.bot.constants import REACTIVATE_CONFIRMATION_TEXT
                    _remove_template_exclusion(wa_id, conversation, contact_name, 'via keyword')
                    react_confirm = Message.objects.create(
                        conversation=conversation,
                        direction='outbound',
                        message_type='text',
                        content=REACTIVATE_CONFIRMATION_TEXT,
                        sender_name='Bot',
                    )
                    conversation.last_message = REACTIVATE_CONFIRMATION_TEXT
                    conversation.last_message_at = timezone.now()
                    conversation.save(update_fields=['last_message', 'last_message_at'])
                    conversation._last_msg_direction = 'outbound'
                    send_whatsapp_outbound('text', REACTIVATE_CONFIRMATION_TEXT, wa_id, message_id=react_confirm.id)
                    publish_conversation_update(conversation, MessageSerializer(react_confirm).data)

                if raw_media and media_type_for_download:
                    _download_pool.submit(download_media_async, message.id, raw_media, media_type_for_download)

            return JsonResponse({'status': 'received'}, status=200)
        except Exception:
            elapsed = time.time() - webhook_start
            logger.exception("Webhook error after %.3fs processing incoming message", elapsed)
            return JsonResponse({'status': 'error'}, status=200)

    return HttpResponse(status=405)


@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def issue_sse_token(request):
    """Issue a short-lived one-time token for SSE connections."""
    from django.utils import timezone
    from datetime import timedelta
    token = SSEToken.objects.create(
        key=uuid4().hex,
        user=request.user,
        expires_at=timezone.now() + timedelta(seconds=30),
    )
    return JsonResponse({'sse_token': token.key})


@csrf_exempt
async def realtime_events(request):
    if request.method != 'GET':
        return HttpResponse(status=405)

    sse_token_key = request.GET.get('sse_token')
    if not sse_token_key:
        return HttpResponse(status=401)

    try:
        sse_token = await SSEToken.objects.select_related('user', 'user__profile').aget(key=sse_token_key)
        if not sse_token.is_valid():
            return HttpResponse(status=401)
        sse_token.used = True
        await sse_token.asave(update_fields=['used'])
    except SSEToken.DoesNotExist:
        return HttpResponse(status=401)

    subscriber_id, event_queue, check_missed, clear_missed = await subscribe(user=sse_token.user)

    async def event_stream():
        try:
            yield 'retry: 3000\n\n'
            while True:
                try:
                    if check_missed():
                        clear_missed()
                        yield 'event: reload\ndata: {"message": "Se perdieron eventos — recargando datos..."}\n\n'
                    event = await asyncio.wait_for(event_queue.get(), timeout=15)
                    yield f'data: {event}\n\n'
                except asyncio.TimeoutError:
                    if check_missed():
                        clear_missed()
                        yield 'event: reload\ndata: {"message": "Se perdieron eventos — recargando datos..."}\n\n'
                    yield ': keep-alive\n\n'
        finally:
            await unsubscribe(subscriber_id)

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    origin = request.headers.get('Origin', '')
    if origin in settings.CORS_ALLOWED_ORIGINS:
        response['Access-Control-Allow-Origin'] = origin
    return response


@csrf_exempt
def serve_media(request, path):
    """Serve media files via ?sig= signed URL only. Signature expires after 1 hour."""
    sig = request.GET.get('sig')
    ts = request.GET.get('t')
    if not sig or not ts:
        return HttpResponseNotFound()
    try:
        media_signer.unsign(f'{path}|{ts}:{sig}')
        age = int(time.time()) - int(ts)
        if age < 0 or age > 3600:
            return HttpResponseNotFound()
    except (BadSignature, ValueError):
        return HttpResponseNotFound()

    if '..' in path or path.startswith('/'):
        return HttpResponseNotFound()

    file_path = os.path.normpath(os.path.join(settings.MEDIA_ROOT, path))

    if not file_path.startswith(os.path.normpath(settings.MEDIA_ROOT)):
        return HttpResponseNotFound()

    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        return HttpResponseNotFound()

    content_type, _ = mimetypes.guess_type(file_path)
    if content_type == 'video/webm' and '/audio/' in path:
        content_type = 'audio/webm'
    if content_type is None:
        MIME_OVERRIDES = {
            '.pdf': 'application/pdf',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
            '.mp4': 'video/mp4',
            '.mp3': 'audio/mpeg',
            '.wav': 'audio/wav',
            '.ogg': 'audio/ogg',
            '.doc': 'application/msword',
            '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            '.xls': 'application/vnd.ms-excel',
            '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        }
        _, ext = os.path.splitext(file_path)
        content_type = MIME_OVERRIDES.get(ext.lower(), 'application/octet-stream')

    filename = os.path.basename(file_path)

    if content_type.startswith(('image/', 'video/', 'audio/')) or content_type == 'application/pdf':
        disposition = 'inline'
    else:
        disposition = 'attachment'

    if settings.DEBUG:
        response = FileResponse(open(file_path, 'rb'), content_type=content_type)
        response['Content-Disposition'] = f'{disposition}; filename="{filename}"'
        response['Cache-Control'] = 'private, max-age=86400, immutable'
        return response

    response = HttpResponse(content_type=content_type)
    response['X-Accel-Redirect'] = f'/internal-media/{path}'
    response['Content-Disposition'] = f'{disposition}; filename="{filename}"'
    return response


@csrf_exempt
def media_proxy(request):
    """Proxy WhatsApp CDN media through our server to avoid CORS issues.
    Authenticates via signed URL (sig + t params) or falls back to token auth."""
    media_url = request.GET.get('url', '')
    if not media_url:
        return HttpResponse(status=400)

    allowed_prefixes = ('https://lookaside.fbsbx.com/', 'https://media.whatsapp.net/')
    if not media_url.startswith(allowed_prefixes):
        return HttpResponse(status=403)

    sig = request.GET.get('sig')
    ts_param = request.GET.get('t')

    authenticated = False
    if sig and ts_param:
        from api.serializers import media_proxy_signer
        try:
            media_proxy_signer.unsign(f'{media_url}|{ts_param}:{sig}')
            age = int(time.time()) - int(ts_param)
            if 0 <= age <= 3600:
                authenticated = True
        except (BadSignature, ValueError):
            pass

    if not authenticated:
        try:
            auth = TokenAuthentication()
            result = auth.authenticate(request)
            if result is not None:
                authenticated = True
        except Exception:
            pass

    if not authenticated:
        return HttpResponse(status=401)

    token = settings.WHATSAPP_API_TOKEN
    if not token:
        return HttpResponse(status=500)

    try:
        req = urllib.request.Request(media_url, headers={'Authorization': f'Bearer {token}'})
        with urllib.request.urlopen(req, timeout=30) as response:
            content_type = response.headers.get('Content-Type', 'application/octet-stream')
            data = response.read()
            return HttpResponse(data, content_type=content_type)
    except Exception as e:
        return HttpResponse(f'Proxy error: {e}', status=502)


_tile_cache_root = None
_tile_cache_lock = threading.Lock()


def _get_tile(zoom, x, y):
    """Fetch a single OSM tile with on-disk cache. Thread-safe."""
    global _tile_cache_root
    if _tile_cache_root is None:
        with _tile_cache_lock:
            if _tile_cache_root is None:
                _tile_cache_root = os.path.join(settings.MEDIA_ROOT, '.tile_cache')

    cache_path = os.path.join(_tile_cache_root, str(zoom), str(x), f'{y}.png')
    if os.path.exists(cache_path):
        with open(cache_path, 'rb') as f:
            return f.read()

    tile_url = f'https://tile.openstreetmap.org/{zoom}/{x}/{y}.png'
    req = urllib.request.Request(tile_url, headers={'User-Agent': 'DomiMessager/1.0'})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = resp.read()

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, 'wb') as f:
        f.write(data)

    return data


@api_view(['GET'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
@cache_page(86400)
def static_map(request):
    import math
    from PIL import Image, ImageDraw
    import io as io_module
    from concurrent.futures import ThreadPoolExecutor, as_completed

    try:
        lat = float(request.GET.get('lat', ''))
        lng = float(request.GET.get('lng', ''))
    except (TypeError, ValueError):
        return HttpResponse(status=400)

    zoom = 15
    tile_size = 256
    width, height = 280, 180

    lat_rad = math.radians(lat)
    n = 2.0 ** zoom
    x_float = (lng + 180.0) / 360.0 * n
    y_float = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    x_tile = int(math.floor(x_float))
    y_tile = int(math.floor(y_float))
    px = int((x_float - x_tile) * tile_size)
    py = int((y_float - y_tile) * tile_size)

    crop_left = x_tile * tile_size + px - width // 2
    crop_top = y_tile * tile_size + py - height // 2

    tile_left = int(math.floor(crop_left / tile_size))
    tile_top = int(math.floor(crop_top / tile_size))
    tile_right = int(math.floor((crop_left + width - 1) / tile_size))
    tile_bottom = int(math.floor((crop_top + height - 1) / tile_size))

    tiles = [
        (zoom, tx, ty, col, row)
        for row, ty in enumerate(range(tile_top, tile_bottom + 1))
        for col, tx in enumerate(range(tile_left, tile_right + 1))
    ]

    results = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_get_tile, zoom, tx, ty): (col, row)
            for zoom, tx, ty, col, row in tiles
        }
        for future in as_completed(futures):
            col, row = futures[future]
            try:
                results[(col, row)] = future.result()
            except Exception:
                pass

    cols = tile_right - tile_left + 1
    rows = tile_bottom - tile_top + 1
    canvas = Image.new('RGB', (cols * tile_size, rows * tile_size), '#e8eed9')

    for (col, row), data in results.items():
        try:
            tile_img = Image.open(io_module.BytesIO(data))
            canvas.paste(tile_img, (col * tile_size, row * tile_size))
        except Exception:
            pass

    offset_x = crop_left - tile_left * tile_size
    offset_y = crop_top - tile_top * tile_size
    cropped = canvas.crop((offset_x, offset_y, offset_x + width, offset_y + height))

    draw = ImageDraw.Draw(cropped)
    mx, my = width // 2, height // 2
    pin_size = 16
    draw.ellipse([mx - pin_size, my - pin_size * 2, mx + pin_size, my], fill='#d63031', outline='#b71c1c')
    draw.polygon([mx - pin_size // 2, my - 4, mx + pin_size // 2, my - 4, mx, my + pin_size // 2], fill='#d63031', outline='#b71c1c')
    draw.ellipse([mx - 4, my - pin_size - 4, mx + 4, my - pin_size + 4], fill='white')

    buf = io_module.BytesIO()
    cropped.save(buf, format='PNG')
    return HttpResponse(buf.getvalue(), content_type='image/png')


# --- Call REST endpoints ---

def _get_recording_config():
    return {"status": "ENABLED", "purpose": "seguridad y calidad", "announcement_language": "es"}


@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def call_answer(request):
    call_id = request.data.get('call_id')
    sdp = request.data.get('sdp')

    if not call_id or not sdp:
        return JsonResponse({'error': 'call_id and sdp required'}, status=400)

    call = get_object_or_404(Call, call_id=call_id)

    if call.status not in ('pending',):
        return JsonResponse(
            {'error': 'Call already answered or terminated'},
            status=409,
        )

    recording = _get_recording_config()

    try:
        call.sdp_answer = sdp
        call.status = 'connected'
        call.recording_status = recording.get('status')
        call.recording_purpose = recording.get('purpose')
        call.recording_announcement_language = recording.get('announcement_language')
        call.save()

        logger.info(
            "call_answer %s: recording %s (purpose=%s lang=%s)",
            call_id, recording['status'], recording['purpose'], recording['announcement_language'],
        )

        release_agent_take_records(call.conversation)
        ConversationTake.objects.filter(conversation=call.conversation).delete()
        ConversationTake.objects.create(
            conversation=call.conversation,
            created_by=request.user,
            duration_minutes=10,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        create_agent_take_record(call.conversation, request.user, duration_minutes=10)

        _publish_call_event(call, 'connected')

        accept_resp = accept_call(call_id, sdp, recording=recording)
        if accept_resp:
            logger.debug("call_answer %s: accept response=%s", call_id, accept_resp)

        return JsonResponse({'success': True, 'call_id': call_id, 'recording': recording})
    except Exception as e:
        logger.exception("Failed to answer call %s", call_id)
        call.status = 'failed'
        call.error_message = str(e)
        call.save()
        return JsonResponse({'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def call_reject(request):
    call_id = request.data.get('call_id')

    if not call_id:
        return JsonResponse({'error': 'call_id required'}, status=400)

    call = get_object_or_404(Call, call_id=call_id)

    try:
        reject_call(call_id)
        call.status = 'rejected'
        call.end_time = timezone.now()
        call.save()
        _publish_call_event(call, 'rejected')
        return JsonResponse({'success': True})
    except Exception as e:
        logger.exception("Failed to reject call %s", call_id)
        return JsonResponse({'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def call_terminate(request):
    call_id = request.data.get('call_id')

    if not call_id:
        return JsonResponse({'error': 'call_id required'}, status=400)

    call = get_object_or_404(Call, call_id=call_id)

    try:
        terminate_call(call_id)
        call.status = 'completed'
        call.end_time = timezone.now()
        if call.start_time:
            call.duration_seconds = int(
                (call.end_time - call.start_time).total_seconds()
            )
        call.save()
        _publish_call_event(call, 'terminated')
        return JsonResponse({'success': True})
    except Exception as e:
        logger.exception("Failed to terminate call %s", call_id)
        return JsonResponse({'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def call_initiate(request):
    to_number = request.data.get('to')
    recipient = request.data.get('recipient')
    sdp = request.data.get('sdp')
    recording = _get_recording_config()

    if not to_number and not recipient:
        return JsonResponse({'error': 'to or recipient required'}, status=400)
    if not sdp:
        return JsonResponse({'error': 'sdp required'}, status=400)

    if to_number:
        conversation = Conversation.objects.filter(whatsapp_id=to_number).first()
        if not conversation:
            conversation = Conversation.objects.create(
                whatsapp_id=to_number,
                contact_name=to_number,
                contact_phone=to_number if to_number.isdigit() else None,
                group=get_default_group(),
            )
    else:
        conversation = Conversation.objects.filter(whatsapp_id=recipient).first()
        if not conversation:
            conversation = Conversation.objects.create(
                whatsapp_id=recipient,
                contact_name=recipient[:50],
                group=get_default_group(),
            )

    try:
        call_id = initiate_call(
            to_number=to_number, recipient_bsuid=recipient,
            sdp_offer=sdp, recording=recording,
        )

        if not call_id:
            return JsonResponse(
                {'error': 'Failed to initiate call — no call_id returned'},
                status=502,
            )

        phone_number_id = settings.WHATSAPP_PHONE_NUMBER_ID

        call = Call.objects.create(
            call_id=call_id,
            conversation=conversation,
            direction='outbound',
            status='pending',
            from_number=phone_number_id,
            to_number=to_number or '',
            recipient_bsuid=recipient or '',
            sdp_offer=sdp,
            recording_status=recording['status'],
            recording_purpose=recording['purpose'],
            recording_announcement_language=recording['announcement_language'],
        )

        logger.info(
            "call_initiate %s: recording %s (purpose=%s lang=%s)",
            call_id, recording['status'], recording['purpose'], recording['announcement_language'],
        )
        logger.debug("call_initiate %s: connect payload recording=%s", call_id, json.dumps(recording))

        release_agent_take_records(conversation)
        ConversationTake.objects.filter(conversation=conversation).delete()
        ConversationTake.objects.create(
            conversation=conversation,
            created_by=request.user,
            duration_minutes=10,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        create_agent_take_record(conversation, request.user, duration_minutes=10)

        _publish_call_event(call, 'outgoing_pending')

        return JsonResponse({'success': True, 'call_id': call_id})
    except CallAPIError as e:
        logger.exception("Failed to initiate call to %s: %s", to_number, e.original_message or str(e))
        call = Call.objects.create(
            call_id=f'failed-{uuid4().hex[:8]}',
            conversation=conversation,
            direction='outbound',
            status='failed',
            from_number=settings.WHATSAPP_PHONE_NUMBER_ID,
            to_number=to_number or '',
            error_code=e.error_code,
            error_message=str(e),
        )
        return JsonResponse({
            'error': str(e),
            'error_code': e.error_code,
            'error_subcode': e.error_subcode,
        }, status=500)
    except Exception as e:
        logger.exception("Failed to initiate call to %s", to_number)
        return JsonResponse({
            'error': 'Error inesperado al iniciar la llamada. Intente nuevamente.',
            'error_code': None,
        }, status=500)


@api_view(['GET'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def call_list(request):
    before = request.query_params.get('before')
    conversation_id = request.query_params.get('conversation_id')
    limit = int(request.query_params.get('limit', 50))
    limit = min(limit, 100)

    qs = Call.objects.select_related('conversation')

    if conversation_id:
        qs = qs.filter(conversation_id=conversation_id)

    user = request.user
    if not user.is_staff:
        try:
            profile = user.profile
            if profile and profile.group_id:
                qs = qs.filter(conversation__group_id=profile.group_id)
            else:
                qs = qs.none()
        except Exception:
            qs = qs.none()
        other_human_takes = ConversationTake.objects.filter(
            conversation=OuterRef('conversation'),
            expires_at__gt=timezone.now(),
        ).exclude(created_by__isnull=True).exclude(created_by=user).exclude(
            created_by__username='bot'
        )
        qs = qs.annotate(
            _has_other_human_take=Exists(other_human_takes)
        ).filter(_has_other_human_take=False)

    if before:
        try:
            pivot = Call.objects.get(id=before)
            qs = qs.filter(created_at__lt=pivot.created_at)
        except Call.DoesNotExist:
            pass

    qs = qs[:limit + 1]
    results = list(qs)
    has_more = len(results) > limit
    if has_more:
        results = results[:limit]

    return JsonResponse({
        'results': CallSerializer(results, many=True).data,
        'cursor': results[-1].id if results else None,
        'has_more': has_more,
    })


@api_view(['GET'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def call_active(request):
    qs = Call.objects.filter(
        status__in=('pending', 'ringing', 'connected')
    )

    user = request.user
    if not user.is_staff:
        try:
            profile = user.profile
            if profile and profile.group_id:
                qs = qs.filter(conversation__group_id=profile.group_id)
            else:
                qs = qs.none()
        except Exception:
            qs = qs.none()
        other_human_takes = ConversationTake.objects.filter(
            conversation=OuterRef('conversation'),
            expires_at__gt=timezone.now(),
        ).exclude(created_by__isnull=True).exclude(created_by=user).exclude(
            created_by__username='bot'
        )
        qs = qs.annotate(
            _has_other_human_take=Exists(other_human_takes)
        ).filter(_has_other_human_take=False)

    active_call = qs.order_by('-created_at').first()

    if active_call:
        return JsonResponse({'active': True, 'call': CallSerializer(active_call).data})
    return JsonResponse({'active': False, 'call': None})


@api_view(['GET'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def call_turn_config(request):
    turn_url = settings.TURN_SERVER_URL
    turn_username = settings.TURN_SERVER_USERNAME
    turn_credential = settings.TURN_SERVER_CREDENTIAL

    ice_servers = []

    stun_host = urllib.parse.urlparse(turn_url).hostname or 'localhost'
    ice_servers.append({"urls": f"stun:{stun_host}:3478"})

    if turn_url and turn_username and turn_credential:
        ice_servers.append({
            "urls": turn_url,
            "username": turn_username,
            "credential": turn_credential,
        })

    return JsonResponse({"iceServers": ice_servers})


@api_view(['GET'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated, IsAdminUser])
def call_recordings(request):
    before = request.query_params.get('before')
    limit = int(request.query_params.get('limit', 50))
    limit = min(limit, 100)

    qs = Call.objects.filter(
        recording_status='ENABLED'
    ).select_related('conversation')

    if before:
        qs = qs.filter(id__lt=int(before))

    qs = qs.order_by('-id')[:limit + 1]

    has_more = len(qs) > limit
    results = qs[:limit]

    data = []
    for call in results:
        recording_url = call.recording_local_path or ''
        has_recording = bool(recording_url)
        if recording_url.startswith('/media/'):
            recording_url = sign_media_url(recording_url)

        take = ConversationTake.objects.filter(
            conversation=call.conversation,
        ).exclude(
            created_by__username='bot',
        ).order_by('-created_at').first()

        data.append({
            'id': call.id,
            'call_id': call.call_id,
            'direction': call.direction,
            'status': call.status,
            'start_time': call.start_time,
            'end_time': call.end_time,
            'duration_seconds': call.duration_seconds,
            'client_name': call.conversation.contact_name,
            'client_phone': call.conversation.contact_phone,
            'agent_username': take.created_by.username if take and take.created_by else None,
            'agent_full_name': take.created_by.get_full_name() or take.created_by.first_name if take and take.created_by else None,
            'recording_url': recording_url,
            'has_recording': has_recording,
        })

    cursor = results[-1].id if results else None

    return JsonResponse({
        'results': data,
        'cursor': cursor,
        'has_more': has_more,
    })


@api_view(['GET', 'POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated, IsAdminUser])
def call_settings(request):
    phone_number_id = settings.WHATSAPP_PHONE_NUMBER_ID
    token = settings.WHATSAPP_API_TOKEN
    base_url = f"https://graph.facebook.com/v20.0/{phone_number_id}/settings"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    if request.method == 'GET':
        req = urllib.request.Request(base_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            calling = data.get('calling', {})
            return JsonResponse(calling)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode() if hasattr(e, 'read') else ''
            return JsonResponse(
                {'error': f"HTTP {e.code}: {err_body[:300]}"},
                status=e.code,
            )

    calling_data = {}
    for field in ('status', 'call_icon_visibility', 'callback_permission_status'):
        if field in request.data:
            calling_data[field] = request.data[field]

    if not calling_data:
        logger.warning("call_settings POST no valid fields. request.data=%s", request.data)
        return JsonResponse({'error': 'No valid fields provided', 'received': dict(request.data)}, status=400)

    payload = {"calling": calling_data}
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(base_url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return JsonResponse(json.loads(resp.read().decode()))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if hasattr(e, 'read') else ''
        return JsonResponse(
            {'error': f"HTTP {e.code}: {err_body[:300]}"},
            status=e.code,
        )


@api_view(['GET'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def export_conversations_csv(request):
    qs = Conversation.objects.select_related('group').prefetch_related(
        'takes', 'tags', 'notes',
    )

    if not request.user.is_staff:
        try:
            profile = request.user.profile
            if profile and profile.group_id:
                qs = qs.filter(group_id=profile.group_id)
            else:
                qs = qs.none()
        except Exception:
            qs = qs.none()
        # Annotate personal pin so it can be referenced in the filter
        user_pin = ConversationUserPin.objects.filter(
            conversation=OuterRef('id'),
            user=request.user,
        )
        qs = qs.annotate(
            _has_user_pin=Exists(user_pin),
        )

        other_human_takes = ConversationTake.objects.filter(
            conversation=OuterRef('id'),
            expires_at__gt=timezone.now(),
        ).exclude(created_by__isnull=True).exclude(created_by=request.user).exclude(
            created_by__username='bot'
        )
        qs = qs.annotate(
            _has_other_human_take=Exists(other_human_takes)
        ).filter(
            Q(_has_other_human_take=False) | Q(_has_user_pin=True)
        )

    qs = qs.order_by('-last_message_at')

    def stream():
        import io
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            'ID', 'Contacto', 'Teléfono', 'Último mensaje',
            'Fecha último mensaje', 'Estado', 'Agente',
            'Tags', 'Notas', 'Creado', 'Actualizado',
        ])
        yield buffer.getvalue().encode('utf-8-sig')
        buffer.truncate(0)
        buffer.seek(0)

        for conv in qs.iterator(chunk_size=200):
            now = timezone.now()
            active_take = None
            for t in conv.takes.all():
                if t.expires_at and t.expires_at > now:
                    active_take = t
                    break

            agent_name = ''
            if active_take and active_take.created_by:
                agent_name = active_take.created_by.get_full_name() or active_take.created_by.username

            tag_names = ', '.join(
                t.tag_name for t in conv.tags.all()
                if t.expires_at is None or t.expires_at > now
            )
            note_contents = ' | '.join(
                n.content[:100] for n in conv.notes.all()
                if n.expires_at is None or n.expires_at > now
            )

            writer.writerow([
                conv.id,
                conv.custom_name or conv.contact_name or '',
                conv.contact_phone or '',
                conv.last_message or '',
                conv.last_message_at.isoformat() if conv.last_message_at else '',
                conv.get_status_display() if hasattr(conv, 'get_status_display') else conv.status,
                agent_name,
                tag_names,
                note_contents,
                conv.created_at.isoformat() if conv.created_at else '',
                conv.updated_at.isoformat() if conv.updated_at else '',
            ])
            yield buffer.getvalue().encode('utf-8-sig')
            buffer.truncate(0)
            buffer.seek(0)

    from zoneinfo import ZoneInfo
    bog_now = timezone.now().astimezone(ZoneInfo('America/Bogota'))
    response = StreamingHttpResponse(stream(), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="conversaciones_{bog_now.strftime("%Y%m%d")}.csv"'
    return response


@api_view(['GET'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated, IsAdminUser])
def agent_stats(request):
    """Per-agent statistics for admin view."""
    from django.db.models import Count, Avg, F, Func, FloatField
    import redis as sync_redis

    days = int(request.query_params.get('days', 30))
    since = timezone.now() - timedelta(days=days)

    agents = User.objects.exclude(username='bot').select_related('profile__group', 'presence')

    msg_counts = dict(
        Message.objects.filter(
            sender__in=agents, direction='outbound', created_at__gte=since,
        ).values('sender_id').annotate(cnt=Count('id')).values_list('sender_id', 'cnt')
    )

    conv_counts = dict(
        AgentTakeRecord.objects.filter(
            agent__in=agents, taken_at__gte=since,
        ).values('agent_id').annotate(cnt=Count('conversation_id', distinct=True)).values_list('agent_id', 'cnt')
    )

    avg_response_times = dict(
        AgentTakeRecord.objects.filter(
            agent__in=agents,
            taken_at__gte=since,
            first_response_at__isnull=False,
        ).values('agent_id').annotate(
            avg_seconds=Avg(
                Func(
                    F('first_response_at') - F('taken_at'),
                    function='EXTRACT',
                    template="EXTRACT(EPOCH FROM %(expressions)s)",
                    output_field=FloatField(),
                )
            )
        ).values_list('agent_id', 'avg_seconds')
    )

    presences = {}
    try:
        r = sync_redis.from_url(settings.REDIS_URL, decode_responses=True)
        for key in r.scan_iter('agent:presence:*'):
            uid = key.split(':')[-1]
            try:
                uid_int = int(uid)
                presences[uid_int] = r.get(key) or 'offline'
            except ValueError:
                pass
        r.close()
    except Exception:
        pass

    results = []
    for agent in agents:
        uid = agent.id
        online_status = presences.get(uid)
        if not online_status:
            try:
                online_status = agent.presence.status
            except Exception:
                online_status = 'offline'

        avg_rt = avg_response_times.get(uid)
        results.append({
            'id': uid,
            'username': agent.username,
            'first_name': agent.first_name,
            'last_name': agent.last_name,
            'group': agent.profile.group.name if hasattr(agent, 'profile') and agent.profile and agent.profile.group else None,
            'online': online_status in ('online', 'away'),
            'status': online_status,
            'messages_sent': msg_counts.get(uid, 0),
            'conversations_handled': conv_counts.get(uid, 0),
            'avg_response_time_seconds': round(avg_rt, 1) if avg_rt is not None else None,
        })

    return Response({'results': results, 'days': days})
