"""
Views for WhatsApp Messenger API
"""
import time
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.parsers import FormParser, MultiPartParser, JSONParser
from django.utils import timezone
from django.contrib.auth.models import User
from django.http import FileResponse, HttpResponse, HttpResponseNotFound, JsonResponse, StreamingHttpResponse
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.cache import cache_page
from django.db import transaction
from rest_framework.authentication import TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from datetime import datetime, timezone as dt_timezone
from django.shortcuts import get_object_or_404
from django.db.models import Exists, OuterRef, Subquery, Q, Count, Prefetch
from django.core.signing import BadSignature
import threading
import hmac
import hashlib
from concurrent.futures import ThreadPoolExecutor

from uuid import uuid4
from .models import Conversation, Message, ConversationTag, ConversationNote, ConversationTake, StickerAsset, SSEToken, CityGroup, BotExemptContact, BotSchedule, BotConfig, WhatsAppTemplate, Call
from .serializers import (
    ConversationSerializer, CityGroupSerializer,
    ConversationListSerializer, MessageSerializer, ConversationTagSerializer,
    ConversationNoteSerializer, ConversationTakeSerializer,
    CreateConversationTagSerializer, CreateConversationNoteSerializer,
    BotScheduleSerializer, BotConfigSerializer,
    TakeConversationSerializer, InitiateConversationSerializer,
    UserSerializer, StickerAssetSerializer, BotExemptContactSerializer,
    WhatsAppTemplateSerializer, CallSerializer,
    media_signer, sign_media_url,
)
from .redis_client import get_sync_redis
import asyncio
import json
import subprocess
import tempfile
import logging

from django.core.cache import cache

from .realtime import publish, subscribe, unsubscribe
from .rate_limiter import acquire as acquire_rate_capacity

logger = logging.getLogger('api')


def get_default_group():
    try:
        return CityGroup.objects.get(slug='tulua')
    except CityGroup.DoesNotExist:
        return None

_send_pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix='wa-send')
_download_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix='wa-dl')

import uuid
import os
import mimetypes
import urllib.request
import urllib.error
import urllib.parse

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
        publish(payload)
    except Exception:
        logger.exception("Failed to publish SSE conversation update")

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
    with urllib.request.urlopen(req) as response:
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


def download_whatsapp_media(media_value, token, media_type):
    if not media_value or not token:
        return None

    try:
        if media_value.startswith('http'):
            fetch_url = media_value
        else:
            graph_url = f"https://graph.facebook.com/v20.0/{media_value}"
            req = urllib.request.Request(graph_url, headers={'Authorization': f'Bearer {token}'})
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode())
                fetch_url = data.get('url', '')
            if not fetch_url:
                return None

        req = urllib.request.Request(fetch_url, headers={'Authorization': f'Bearer {token}'})
        with urllib.request.urlopen(req) as response:
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


def send_whatsapp_outbound(message_type, content, contact_phone, message_id=None, conversation_id=None, context_wamid=None):
    phone_number_id = getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', None) or settings.WHATSAPP_PHONE_NUMBER
    token = settings.WHATSAPP_API_TOKEN
    if not phone_number_id or not token or not contact_phone:
        logger.error(
            'WhatsApp outbound send blocked: phone_number_id=%s token_set=%s contact_phone=%r',
            bool(phone_number_id), bool(token), contact_phone,
        )
        return

    try:
        url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": contact_phone,
            "type": message_type,
        }

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
                acquire_rate_capacity(phone_number_id)
                try:
                    media_id = upload_media_to_whatsapp(upload_path, phone_number_id, token)
                except urllib.error.HTTPError as e:
                    err_body = e.read().decode() if hasattr(e, 'read') else ''
                    logger.warning("Media upload to WhatsApp failed: HTTP %s %s", e.code, err_body[:200])
                except Exception:
                    logger.warning("Media upload to WhatsApp failed (network/config error)")
                finally:
                    if converted_path:
                        try:
                            os.remove(converted_path)
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
                    if message_type == 'document' and message_id:
                        try:
                            msg = Message.objects.get(id=message_id)
                            meta = msg.metadata or {}
                            if meta.get('filename'):
                                media_payload['filename'] = meta['filename']
                            if msg.content:
                                media_payload['caption'] = msg.content
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
                if message_type == 'document' and message_id:
                    try:
                        msg = Message.objects.get(id=message_id)
                        meta = msg.metadata or {}
                        if meta.get('filename'):
                            media_payload['filename'] = meta['filename']
                        if msg.content:
                            media_payload['caption'] = msg.content
                    except Message.DoesNotExist:
                        pass
                payload[message_type] = media_payload
        elif message_type == 'template':
            # content is a dict: {name, language, components}
            payload['type'] = 'template'
            payload['template'] = content
        else:
            payload['type'] = 'text'
            payload['text'] = {"body": content}

        if context_wamid:
            payload['context'] = {"message_id": context_wamid}

        acquire_rate_capacity(phone_number_id)

        body = json.dumps(payload).encode('utf-8')
        logger.info('WhatsApp outbound -> %s [%s]', contact_phone, message_type)
        req = urllib.request.Request(url, data=body, headers=headers, method='POST')
        with urllib.request.urlopen(req) as response:
            resp_body = response.read().decode()
            if message_id:
                try:
                    resp_data = json.loads(resp_body)
                    wamid = resp_data.get('messages', [{}])[0].get('id', '')
                    if wamid:
                        Message.objects.filter(id=message_id).update(whatsapp_message_id=wamid)
                        logger.info('Updated message %d with wamid %s', message_id, wamid)
                    if conversation_id:
                        try:
                            conv = Conversation.objects.get(id=conversation_id)
                            sent_msg = Message.objects.get(id=message_id)
                            publish_conversation_update(conv, MessageSerializer(sent_msg).data)
                        except Exception:
                            logger.exception('Failed to publish update after wamid for message %d', message_id)
                except Exception:
                    logger.exception('Failed to update wamid for message %d', message_id)
    except urllib.error.HTTPError as e:
        body = e.read().decode() if hasattr(e, 'read') else ''
        logger.error('WhatsApp API HTTP %s: %s', e.code, body)
        if message_id:
            try:
                err_data = {}
                try:
                    parsed = json.loads(body)
                    err_data = parsed.get('error', {})
                except Exception:
                    pass
                failed_msg = Message.objects.get(id=message_id)
                meta = failed_msg.metadata or {}
                meta['send_error'] = err_data.get('message', body[:200]) or body[:200]
                meta['send_error_code'] = err_data.get('code', e.code)
                failed_msg.metadata = meta
                failed_msg.save(update_fields=['metadata'])
                if conversation_id:
                    conv = Conversation.objects.get(id=conversation_id)
                    publish_conversation_update(conv, MessageSerializer(failed_msg).data)
            except Exception:
                logger.exception('Failed to update send_error for message %d', message_id)
    except Exception:
        logger.exception("Error sending WhatsApp message")
        if message_id:
            try:
                failed_msg = Message.objects.get(id=message_id)
                meta = failed_msg.metadata or {}
                meta['send_error'] = 'Network error sending message'
                failed_msg.metadata = meta
                failed_msg.save(update_fields=['metadata'])
                if conversation_id:
                    conv = Conversation.objects.get(id=conversation_id)
                    publish_conversation_update(conv, MessageSerializer(failed_msg).data)
            except Exception:
                logger.exception('Failed to update send_error for message %d', message_id)


# --- WhatsApp Calling API helpers ---

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
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if hasattr(e, 'read') else ''
        logger.error("WhatsApp /calls API error %s: %s", e.code, err_body[:500])
        raise


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
        phone_number_id, "pre_accept", call_id=call_id, session=session
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

    def get_permissions(self):
        if self.action in ('destroy', 'remove_expired_tags', 'send_template'):
            return [IsAuthenticated(), IsAdminUser()]
        return super().get_permissions()

    def get_queryset(self):
        """Return conversations for the user's group (staff sees all)"""
        qs = Conversation.objects.select_related('group').order_by('-last_message_at', '-created_at')
        now = timezone.now()
        last_msg = Message.objects.filter(conversation=OuterRef('pk')).order_by('-created_at')

        user = self.request.user
        if user.is_authenticated and not user.is_staff:
            try:
                profile = user.profile
                if profile and profile.group_id:
                    qs = qs.filter(group_id=profile.group_id)
            except Exception:
                qs = qs.none()

            # Only filter out other-human-taken conversations for list views
            # Detail actions rely on action-level permission checks instead
            if self.action in ('list', 'active_conversations'):
                other_human_takes = ConversationTake.objects.filter(
                    conversation=OuterRef('pk'),
                    expires_at__gt=now,
                ).exclude(created_by__isnull=True).exclude(created_by=user).exclude(created_by__username='bot')
                qs = qs.annotate(
                    _has_other_human_take=Exists(other_human_takes)
                ).filter(_has_other_human_take=False)

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
            conversation.save()
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
            conversation.save()
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
            conversation.save()
            publish_conversation_update(conversation)
            return Response(status=status.HTTP_204_NO_CONTENT)
        except ConversationNote.DoesNotExist:
            return Response({'error': 'Note not found'}, status=status.HTTP_404_NOT_FOUND)

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
            ConversationTake.objects.filter(conversation=conversation).delete()
            get_sync_redis().delete(f"bot:escalated:{conversation.id}")
            duration_minutes = serializer.validated_data.get('duration_minutes', 30)
            take = ConversationTake.create_take(
                conversation=conversation,
                created_by=request.user,
                duration_minutes=duration_minutes,
            )
            take_serializer = ConversationTakeSerializer(take)
            conversation.save()
            # Clear prefetch cache so SSE serializes fresh takes, not stale prefetch
            if hasattr(conversation, '_prefetched_objects_cache'):
                conversation._prefetched_objects_cache.pop('takes', None)
            publish_conversation_update(conversation)
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
            active_take.delete()
        conversation.save()
        # Clear prefetch cache so SSE serializes fresh takes, not stale prefetch
        if hasattr(conversation, '_prefetched_objects_cache'):
            conversation._prefetched_objects_cache.pop('takes', None)
        publish_conversation_update(conversation)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'])
    def hand_over_to_bot(self, request, pk=None):
        """Release the conversation back to the bot — clears all bot-blocking conditions.

        Does NOT remove Domii tags (exemptions stay intact).
        """
        conversation = self.get_object()

        # 1. Release any human take
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

        conversation.save()
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
            publish_conversation_update(conversation)
        return Response({'marked_read': count}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def set_custom_name(self, request, pk=None):
        """Set a custom display name for this contact"""
        conversation = self.get_object()
        custom_name = request.data.get('custom_name', '').strip() or None
        conversation.custom_name = custom_name
        conversation.save()

        Conversation.objects.filter(whatsapp_id=conversation.whatsapp_id, custom_name__isnull=True).update(
            custom_name=custom_name
        )

        publish_conversation_update(conversation)
        return Response({'custom_name': custom_name}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'])
    def remove_expired_tags(self, request):
        """Delete expired tags, notes, and takes"""
        now = timezone.now()
        tags_count = ConversationTag.objects.filter(expires_at__lt=now).delete()[0]
        notes_count = ConversationNote.objects.filter(expires_at__lt=now).delete()[0]
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
            conversation.save()
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

            queryset = conversation.messages.select_related('context_message').all()

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
            conversation.save()

            if direction == 'outbound' and message_type not in ('edit', 'reaction'):
                context_wamid = context_msg.whatsapp_message_id if context_msg else None
                transaction.on_commit(lambda: _send_pool.submit(
                    send_whatsapp_outbound,
                    message_type, content, conversation.contact_phone, message.id, conversation.id,
                    context_wamid=context_wamid,
                ))

            serializer = MessageSerializer(message)
            publish_conversation_update(conversation, serializer.data)

            return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'])
    def active_conversations(self, request):
        """Get active conversations with cursor-based pagination"""
        queryset = self.get_queryset().filter(status='active').annotate(
            msg_count=Count('messages')
        ).filter(msg_count__gt=0)

        before = request.query_params.get('before')
        limit = min(int(request.query_params.get('limit', 100)), 500)

        if before:
            queryset = queryset.filter(last_message_at__lt=before)

        queryset = queryset.order_by('-last_message_at', '-created_at')
        conv_page = list(queryset[:limit + 1])
        has_more = len(conv_page) > limit
        conv_page = conv_page[:limit]

        cursor = conv_page[-1].last_message_at.isoformat() if conv_page and has_more else None

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
            conversation.save()

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

        if message_type not in ('edit', 'reaction'):
            transaction.on_commit(lambda: _send_pool.submit(
                send_whatsapp_outbound,
                message_type, content, conversation.contact_phone, message.id, conversation.id,
            ))

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

            # Exclude conversations taken by another human (non-bot, non-self)
            other_human_takes = ConversationTake.objects.filter(
                conversation=OuterRef('pk'),
                expires_at__gt=now,
            ).exclude(created_by=user).exclude(created_by__username='bot')
            base_qs = base_qs.annotate(
                _has_other_human_take=Exists(other_human_takes)
            ).filter(_has_other_human_take=False)

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

        if not conversation.contact_phone:
            return Response({'error': 'No contact phone'}, status=status.HTTP_400_BAD_REQUEST)

        # Build components with parameter substitution
        components = []
        for comp in template.components:
            comp_type = comp.get('type')
            if comp_type == 'body':
                params = []
                if parameters:
                    for p in parameters:
                        params.append({
                            'type': 'text',
                            'parameter_name': p,
                            'text': parameters[p],
                        })
                components.append({'type': 'body', 'parameters': params})
            elif comp_type == 'header' and comp.get('format') in ('image', 'video', 'document'):
                header_param = parameters.get('header_media_id')
                if header_param:
                    components.append({
                        'type': 'header',
                        'parameters': [{'type': comp['format'], comp['format']: {'id': header_param}}],
                    })
            elif comp_type == 'buttons':
                button_params = parameters.get('buttons', [])
                btn_components = []
                for i, btn in enumerate(comp.get('buttons', [])):
                    if btn['type'] == 'url' and i < len(button_params):
                        btn_components.append({
                            'type': 'url',
                            'text': btn['text'],
                            'url': button_params[i],
                        })
                if btn_components:
                    components.append({'type': 'button', 'sub_type': 'url', 'index': '0', 'parameters': btn_components})

        payload = {
            'name': template.name,
            'language': {'code': template.language},
            'components': components,
        }

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

        transaction.on_commit(lambda: _send_pool.submit(
            send_whatsapp_outbound,
            'template', payload, conversation.contact_phone,
            message.id, conversation.id,
        ))

        publish_conversation_update(conversation)
        return Response(MessageSerializer(message).data, status=status.HTTP_201_CREATED)


class MessageViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for reading messages"""
    serializer_class = MessageSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = Message.objects.select_related('conversation', 'context_message').all()

        if user.is_authenticated and not user.is_staff:
            from django.utils import timezone as tz
            now = tz.now()
            if self.action == 'list':
                other_takes = ConversationTake.objects.filter(
                    conversation=OuterRef('conversation'),
                    expires_at__gt=now,
                ).exclude(created_by=user).exclude(created_by__username='bot')
                qs = qs.annotate(
                    _msg_other_take=Exists(other_takes)
                ).filter(_msg_other_take=False)

        return qs


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


class StickerAssetViewSet(viewsets.ModelViewSet):
    """Store and serve reusable sticker images."""

    serializer_class = StickerAssetSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_permissions(self):
        from rest_framework.permissions import IsAdminUser, IsAuthenticated
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            return [IsAuthenticated(), IsAdminUser()]
        return [IsAuthenticated()]

    def get_queryset(self):
        return StickerAsset.objects.all()

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
        return Response(BotConfigSerializer(config).data)


class WhatsAppTemplateViewSet(viewsets.ModelViewSet):
    """Manage WhatsApp message templates. Admins can create, list, sync, delete."""
    queryset = WhatsAppTemplate.objects.all()
    serializer_class = WhatsAppTemplateSerializer
    permission_classes = [IsAuthenticated, IsAdminUser]

    def perform_create(self, serializer):
        from . import whatsapp_templates
        template = serializer.save()
        try:
            result = whatsapp_templates.create_template(
                name=template.name,
                language=template.language,
                category=template.category,
                components=template.components,
            )
            if result:
                template.template_id = str(result.get('id', ''))
                template.status = result.get('status', 'PENDING')
            else:
                template.status = 'REJECTED'
                template.rejection_reason = 'Failed to submit to Meta API'
        except Exception:
            template.status = 'REJECTED'
            template.rejection_reason = 'Error submitting to Meta API'
        template.save()

    def perform_destroy(self, instance):
        from . import whatsapp_templates
        if instance.name:
            whatsapp_templates.delete_template(name=instance.name)
        instance.delete()

    @action(detail=True, methods=['post'])
    def sync_status(self, request, pk=None):
        """Re-fetch template status from Meta."""
        from . import whatsapp_templates
        template = self.get_object()
        if template.template_id:
            result = whatsapp_templates.get_template(template.template_id)
            if result:
                template.status = result.get('status', template.status)
                template.save(update_fields=['status'])
                return Response(WhatsAppTemplateSerializer(template).data)
            return Response({'error': 'Failed to sync with Meta'}, status=400)
        return Response({'error': 'No template_id to sync'}, status=400)

    @action(detail=False, methods=['post'])
    def sync_all(self, request):
        """Sync status of all templates from Meta."""
        from . import whatsapp_templates
        remote = whatsapp_templates.list_templates()
        updated = 0
        for rt in remote:
            try:
                template = WhatsAppTemplate.objects.get(name=rt.get('name', ''), language=rt.get('language', 'es'))
                old = template.status
                template.status = rt.get('status', template.status)
                if old != template.status:
                    template.save(update_fields=['status'])
                    updated += 1
            except WhatsAppTemplate.DoesNotExist:
                pass
        return Response({'synced': len(remote), 'updated': updated})

    @action(detail=False, methods=['post'], parser_classes=[FormParser, MultiPartParser])
    def upload_media(self, request):
        """Upload an image to Meta for use as a template header."""
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

    @action(detail=False, methods=['post'])
    def bulk_send(self, request):
        """Send a template to the last N conversations (admin only)."""
        from .serializers import BulkSendTemplateSerializer
        serializer = BulkSendTemplateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        template = WhatsAppTemplate.objects.get(id=serializer.validated_data['template_id'])
        count = serializer.validated_data['count']
        parameter_sources = serializer.validated_data.get('parameter_sources', {})
        fixed_values = serializer.validated_data.get('fixed_values', {})
        header_media_id = request.data.get('header_media_id')

        conversations = Conversation.objects.exclude(
            contact_phone__isnull=True,
        ).exclude(contact_phone='').order_by('-last_message_at')[:count]

        def _resolve_params(conv):
            """Resolve template parameters for a given conversation."""
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

        queued = 0
        for conv in conversations:
            resolved_params = _resolve_params(conv)
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

            transaction.on_commit(lambda c=conv, p=payload, m=message: _send_pool.submit(
                send_whatsapp_outbound,
                'template', p, c.contact_phone, m.id, c.id,
            ))

            publish_conversation_update(conv)
            queued += 1

        return Response({
            'queued': queued,
            'template_name': template.name,
            'total': len(conversations),
        })

    @action(detail=False, methods=['get'])
    def approved(self, request):
        """Return only APPROVED templates (for sending UI)."""
        queryset = self.queryset.filter(status='APPROVED')
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)


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
        publish(payload)
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
        call = Call.objects.filter(call_id=call_id).first()
        if not call:
            return

        rec = call_event.get('call_recording', {})
        audio = rec.get('audio', {})
        call.recording_audio_id = audio.get('id')
        call.recording_audio_url = audio.get('url')
        call.recording_audio_sha256 = audio.get('sha256')
        call.recording_audio_mime_type = audio.get('mime_type')
        call.save()
        _publish_call_event(call, 'recording_available')

        cdn_url = audio.get('url')
        if cdn_url and settings.WHATSAPP_API_TOKEN:
            try:
                token = settings.WHATSAPP_API_TOKEN
                req = urllib.request.Request(cdn_url, headers={'Authorization': f'Bearer {token}'})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                ext = '.ogg'
                filename = f"{uuid.uuid4().hex}{ext}"
                local_dir = os.path.join(settings.MEDIA_ROOT, 'recordings')
                os.makedirs(local_dir, exist_ok=True)
                local_path = os.path.join(local_dir, filename)
                with open(local_path, 'wb') as f:
                    f.write(raw)
                call.recording_local_path = f"{settings.MEDIA_URL}recordings/{filename}"
                call.save(update_fields=['recording_local_path'])
            except Exception as e:
                logger.error("Failed to download call recording %s: %s", call_id, e)


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


# Webhook endpoint for WhatsApp (verification + incoming messages)
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
                        conversation.save()

                if not conversation.custom_name:
                    existing_custom = Conversation.objects.filter(
                        whatsapp_id=wa_id
                    ).exclude(custom_name__isnull=True).exclude(custom_name='').values_list('custom_name', flat=True).first()
                    if existing_custom:
                        conversation.custom_name = existing_custom
                        conversation.save(update_fields=['custom_name'])

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

                else:
                    content = msg.get('text', {}).get('body', '') if isinstance(msg.get('text'), dict) else msg.get('text', '')
                    last_msg_text = content or msg_type

                if hasattr(content, '__iter__') and not isinstance(content, str):
                    content = str(content)

                context_message_obj = None
                if 'context' in msg:
                    meta['context'] = msg['context']
                    ctx_wamid = msg['context'].get('id', '')
                    if ctx_wamid:
                        ctx_msg = Message.objects.filter(
                            conversation=conversation,
                            whatsapp_message_id=ctx_wamid,
                        ).first()
                        if ctx_msg:
                            context_message_obj = ctx_msg

                if msg_id:
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

                elapsed = time.time() - webhook_start
                logger.info("Webhook msg %s: %.3fs from receipt to SSE publish (last_msg=%s)", message.id, elapsed, last_msg_text)
                publish_conversation_update(conversation, MessageSerializer(message).data)

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
        sse_token = await SSEToken.objects.select_related('user').aget(key=sse_token_key)
        if not sse_token.is_valid():
            return HttpResponse(status=401)
        sse_token.used = True
        await sse_token.asave(update_fields=['used'])
    except SSEToken.DoesNotExist:
        return HttpResponse(status=401)

    subscriber_id, event_queue, _, _ = await subscribe()

    async def event_stream():
        try:
            yield 'retry: 3000\n\n'
            while True:
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=15)
                    yield f'data: {event}\n\n'
                except asyncio.TimeoutError:
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
    try:
        from .models import BotConfig
        cfg = BotConfig.objects.filter(key='call_recording_enabled').first()
        if cfg and cfg.value:
            return {"status": "ENABLED", "purpose": "quality assurance", "announcement_language": "es"}
    except Exception:
        pass
    return None


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
        if recording:
            call.recording_status = recording.get('status')
            call.recording_purpose = recording.get('purpose')
            call.recording_announcement_language = recording.get('announcement_language')
        call.save()

        pre_accept_call(call_id, sdp)
        accept_call(call_id, sdp, recording=recording)

        _publish_call_event(call, 'connected')

        return JsonResponse({'success': True, 'call_id': call_id})
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
            recording_status=recording.get('status') if recording else None,
            recording_purpose=recording.get('purpose') if recording else None,
            recording_announcement_language=(
                recording.get('announcement_language') if recording else None
            ),
        )

        _publish_call_event(call, 'outgoing_pending')

        return JsonResponse({'success': True, 'call_id': call_id})
    except Exception as e:
        logger.exception("Failed to initiate call to %s", to_number)
        return JsonResponse({'error': str(e)}, status=500)


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
            created_at__lte=call.start_time or call.created_at,
            expires_at__gt=call.start_time or call.created_at,
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
            'agent_name': take.created_by.username if take and take.created_by else None,
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
            with urllib.request.urlopen(req) as resp:
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
        with urllib.request.urlopen(req) as resp:
            return JsonResponse(json.loads(resp.read().decode()))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if hasattr(e, 'read') else ''
        return JsonResponse(
            {'error': f"HTTP {e.code}: {err_body[:300]}"},
            status=e.code,
        )
