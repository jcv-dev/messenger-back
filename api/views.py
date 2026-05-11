"""
Views for WhatsApp Messenger API
"""
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import FormParser, MultiPartParser, JSONParser
from django.utils import timezone
from django.contrib.auth.models import User
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from rest_framework.authtoken.models import Token
import threading

from .models import Conversation, Message, ConversationTag, ConversationNote, ConversationTake, StickerAsset
from .serializers import (
    ConversationSerializer,
    ConversationListSerializer, MessageSerializer, ConversationTagSerializer,
    ConversationNoteSerializer, ConversationTakeSerializer,
    CreateConversationTagSerializer, CreateConversationNoteSerializer,
    TakeConversationSerializer, UserSerializer, StickerAssetSerializer
)
import json
import queue

from .realtime import publish, subscribe, unsubscribe


import uuid
import os
import mimetypes
import urllib.request
import urllib.error
import urllib.parse

def publish_conversation_update(conversation, message=None):
    payload = {
        'type': 'conversation.updated',
        'conversation': ConversationSerializer(conversation).data,
    }
    if message is not None:
        payload['message'] = message
    publish(payload)

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
        print(f"Error downloading WhatsApp media: {e}")
        return None


def download_media_async(message_id, raw_media, media_type):
    token = settings.WHATSAPP_API_TOKEN
    url = download_whatsapp_media(raw_media, token, media_type)
    if url:
        try:
            msg = Message.objects.get(id=message_id)
            msg.media_url = url
            msg.save(update_fields=['media_url'])
            msg.conversation.save()
            publish_conversation_update(msg.conversation)
        except Message.DoesNotExist:
            pass
            pass


def send_whatsapp_outbound(message_type, content, contact_phone):
    phone_number_id = getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', None) or settings.WHATSAPP_PHONE_NUMBER
    token = settings.WHATSAPP_API_TOKEN
    if not phone_number_id or not token or not contact_phone:
        return

    try:
        url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        wa_type = 'image' if message_type == 'sticker' else message_type
        payload = {
            "messaging_product": "whatsapp",
            "to": contact_phone,
            "type": wa_type,
        }

        if message_type == 'text':
            payload['text'] = {"body": content}
        elif message_type in ['sticker', 'image', 'video', 'audio', 'document']:
            parsed = urllib.parse.urlparse(content)
            media_url = getattr(settings, 'MEDIA_URL', '/media/')
            media_id = None

            if parsed.path.startswith(media_url):
                relative_path = parsed.path[len(media_url):].lstrip('/')
                file_path = os.path.join(settings.MEDIA_ROOT, relative_path)
                if os.path.exists(file_path):
                    try:
                        media_id = upload_media_to_whatsapp(file_path, phone_number_id, token)
                    except Exception as e:
                        print(f"Error uploading media to WhatsApp: {e}")

            if media_id:
                payload[wa_type] = {"id": media_id}
            else:
                if parsed.hostname in ['localhost', '127.0.0.1']:
                    public_host = next((h for h in settings.ALLOWED_HOSTS if h not in ['localhost', '127.0.0.1', '*']), None)
                    if public_host:
                        content = urllib.parse.urlunparse(('https', public_host, parsed.path, parsed.params, parsed.query, parsed.fragment))
                payload[wa_type] = {"link": content}
        else:
            payload['type'] = 'text'
            payload['text'] = {"body": content}

        req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
        with urllib.request.urlopen(req) as response:
            pass
    except Exception as e:
        print(f"Error sending WhatsApp message: {e}")



class ConversationViewSet(viewsets.ModelViewSet):
    """ViewSet for managing conversations"""
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Return all conversations"""
        return Conversation.objects.order_by('-last_message_at', '-created_at')

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
        """Deactivate a note on a conversation"""
        conversation = self.get_object()
        note_id = request.data.get('note_id')

        try:
            note = ConversationNote.objects.get(id=note_id, conversation=conversation)
            note.is_active = False
            note.save()
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
            duration_minutes = serializer.validated_data.get('duration_minutes', 30)
            ConversationTake.objects.filter(conversation=conversation, is_active=True).update(is_active=False)
            take = ConversationTake.create_take(
                conversation=conversation,
                created_by=request.user,
                duration_minutes=duration_minutes,
            )
            take_serializer = ConversationTakeSerializer(take)
            conversation.save()
            publish_conversation_update(conversation)
            return Response(take_serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def release_conversation(self, request, pk=None):
        """Release the active conversation claim"""
        conversation = self.get_object()
        changed = ConversationTake.objects.filter(conversation=conversation, is_active=True).update(is_active=False)
        conversation.save()
        publish_conversation_update(conversation)
        return Response({'released_count': changed}, status=status.HTTP_204_NO_CONTENT)

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
        """Deactivate expired tags"""
        expired_tags = ConversationTag.objects.filter(
            expires_at__lt=timezone.now(),
            is_active=True
        )
        count = expired_tags.update(is_active=False)
        return Response({'deactivated_count': count})

    @action(detail=True, methods=['post'])
    def remove_tag(self, request, pk=None):
        """Remove a tag from a conversation"""
        conversation = self.get_object()
        tag_id = request.data.get('tag_id')

        try:
            tag = ConversationTag.objects.get(id=tag_id, conversation=conversation)
            tag.is_active = False
            tag.save()
            conversation.save()
            publish_conversation_update(conversation)
            return Response(status=status.HTTP_204_NO_CONTENT)
        except ConversationTag.DoesNotExist:
            return Response({'error': 'Tag not found'}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=['get', 'post'])
    def messages(self, request, pk=None):
        """Get or create messages for a conversation"""
        conversation = self.get_object()

        if request.method == 'GET':
            messages = conversation.messages.all()
            serializer = MessageSerializer(messages, many=True)
            return Response(serializer.data)

        elif request.method == 'POST':
            data = request.data
            direction = data.get('direction', 'outbound')
            message_type = data.get('message_type', 'text')
            content = data.get('content')
            
            message = Message.objects.create(
                conversation=conversation,
                direction=direction,
                message_type=message_type,
                content=content,
                sender_name=data.get('sender_name', request.user.get_full_name() or request.user.username),
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

            if direction == 'outbound' and message_type not in ('edit', 'reaction'):
                threading.Thread(
                    target=send_whatsapp_outbound,
                    args=(message_type, content, conversation.contact_phone),
                    daemon=True
                ).start()

            serializer = MessageSerializer(message)
            publish_conversation_update(conversation, serializer.data)

            return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'])
    def active_conversations(self, request):
        """Get active conversations with non-expired tags"""
        conversations = self.get_queryset().filter(status='active')
        serializer = ConversationListSerializer(conversations, many=True)
        return Response(serializer.data)


class MessageViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for reading messages"""
    serializer_class = MessageSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Message.objects.all()


class UserViewSet(viewsets.ModelViewSet):
    """ViewSet for managing user information"""
    queryset = User.objects.all().order_by('id')
    serializer_class = UserSerializer

    def get_permissions(self):
        from rest_framework.permissions import IsAdminUser, IsAuthenticated
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            return [IsAuthenticated(), IsAdminUser()]
        return [IsAuthenticated()]

    @action(detail=False, methods=['get'])
    def current_user(self, request):
        """Get current user information"""
        serializer = self.get_serializer(request.user)
        return Response(serializer.data)


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

        expected_token = (settings.WEBHOOK_TOKEN or '').strip('"\'')
        if mode == 'subscribe' and expected_token and verify_token == expected_token:
            return HttpResponse(challenge)

        return HttpResponse(status=403)

    # Incoming messages (POST)
    if request.method == 'POST':
        try:
            payload = json.loads(request.body.decode('utf-8'))
        except Exception:
            return HttpResponse(status=400)

        try:
            entries = payload.get('entry', [])
            changes = entries[0].get('changes', []) if entries else []
            value = changes[0].get('value', {}) if changes else payload.get('value', {})
            messages = value.get('messages', [])
            msg = messages[0] if messages else None

            if not msg:
                return JsonResponse({'status': 'no_message'}, status=200)

            msg_type = msg.get('type', 'text')
            sender = msg.get('from') or msg.get('from_user_id')
            msg_id = msg.get('id')

            contacts = value.get('contacts', [])
            contact_info = contacts[0] if contacts else {}
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
                media_url = ''
                meta = {
                    'mime_type': img.get('mime_type', ''),
                    'sha256': img.get('sha256', ''),
                    'media_id': img.get('id', ''),
                }
                if 'context' in msg:
                    meta['context'] = msg['context']
                last_msg_text = content or 'Image'

            elif msg_type == 'video':
                vid = msg.get('video', {})
                content = vid.get('caption', '')
                raw_media = vid.get('url') or vid.get('id') or ''
                media_type_for_download = 'video'
                media_url = ''
                meta = {
                    'mime_type': vid.get('mime_type', ''),
                    'sha256': vid.get('sha256', ''),
                    'media_id': vid.get('id', ''),
                }
                if 'context' in msg:
                    meta['context'] = msg['context']
                last_msg_text = content or 'Video'

            elif msg_type == 'sticker':
                stk = msg.get('sticker', {})
                raw_media = stk.get('url') or stk.get('id') or ''
                media_type_for_download = 'sticker'
                media_url = ''
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
                media_url = ''
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
                media_url = ''
                meta = {
                    'mime_type': aud.get('mime_type', ''),
                    'sha256': aud.get('sha256', ''),
                    'media_id': aud.get('id', ''),
                    'voice': aud.get('voice', False),
                }
                last_msg_text = 'Voice message' if meta.get('voice') else 'Audio'

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
                meta = {
                    'message_id': rxn.get('message_id', ''),
                    'emoji': emoji,
                }
                last_msg_text = f'Reacted {emoji}' if emoji else 'Removed reaction'

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

            else:
                content = msg.get('text', {}).get('body', '') if isinstance(msg.get('text'), dict) else msg.get('text', '')
                last_msg_text = content or msg_type

            if hasattr(content, '__iter__') and not isinstance(content, str):
                content = str(content)

            message = Message.objects.create(
                conversation=conversation,
                direction='inbound',
                message_type=msg_type,
                content=content,
                sender_name=conversation.contact_name,
                whatsapp_message_id=msg_id,
                media_url=media_url,
                metadata=meta,
            )

            conversation.last_message = last_msg_text
            conversation.last_message_at = timezone.now()
            conversation.save()

            publish_conversation_update(conversation, MessageSerializer(message).data)

            if raw_media and media_type_for_download:
                threading.Thread(target=download_media_async, args=(message.id, raw_media, media_type_for_download), daemon=True).start()

            return JsonResponse({'status': 'received'}, status=200)
        except Exception:
            return JsonResponse({'status': 'error'}, status=200)

    return HttpResponse(status=405)


@csrf_exempt
def realtime_events(request):
    if request.method != 'GET':
        return HttpResponse(status=405)

    token_key = request.GET.get('token')
    if not token_key:
        return HttpResponse(status=401)

    try:
        token = Token.objects.select_related('user').get(key=token_key)
    except Token.DoesNotExist:
        return HttpResponse(status=401)

    subscriber_id, event_queue = subscribe()

    def event_stream():
        try:
            yield 'retry: 3000\n\n'
            while True:
                try:
                    event = event_queue.get(timeout=15)
                    yield f'data: {event}\n\n'
                except queue.Empty:
                    yield ': keep-alive\n\n'
        finally:
            unsubscribe(subscriber_id)

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


def media_proxy(request):
    """Proxy WhatsApp CDN media through our server to avoid CORS issues."""
    media_url = request.GET.get('url', '')
    if not media_url:
        return HttpResponse(status=400)

    token = settings.WHATSAPP_API_TOKEN
    if not token:
        return HttpResponse(status=500)

    try:
        req = urllib.request.Request(media_url, headers={'Authorization': f'Bearer {token}'})
        with urllib.request.urlopen(req) as response:
            content_type = response.headers.get('Content-Type', 'application/octet-stream')
            data = response.read()
            return HttpResponse(data, content_type=content_type)
    except Exception as e:
        return HttpResponse(f'Proxy error: {e}', status=502)


def static_map(request):
    import math
    from PIL import Image, ImageDraw
    import io as io_module

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

    cols = tile_right - tile_left + 1
    rows = tile_bottom - tile_top + 1
    canvas = Image.new('RGB', (cols * tile_size, rows * tile_size), '#e8eed9')

    for row in range(rows):
        for col in range(cols):
            tx = tile_left + col
            ty = tile_top + row
            try:
                tile_url = f"https://tile.openstreetmap.org/{zoom}/{tx}/{ty}.png"
                req = urllib.request.Request(tile_url, headers={'User-Agent': 'Django/StaticMap'})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    tile_img = Image.open(io_module.BytesIO(resp.read()))
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
