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



class ConversationViewSet(viewsets.ModelViewSet):
    """ViewSet for managing conversations"""
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Return all conversations"""
        return Conversation.objects.all()

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
            # Update conversation's last message
            if message_type == 'text':
                conversation.last_message = content
            else:
                conversation.last_message = f"[{message_type.capitalize()}]"
            conversation.last_message_at = timezone.now()
            conversation.save()

            phone_number_id = getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', None) or settings.WHATSAPP_PHONE_NUMBER
            if direction == 'outbound' and phone_number_id and settings.WHATSAPP_API_TOKEN:
                import urllib.request
                import urllib.error
                import json
                try:
                    url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
                    headers = {
                        "Authorization": f"Bearer {settings.WHATSAPP_API_TOKEN}",
                        "Content-Type": "application/json"
                    }
                    payload = {
                        "messaging_product": "whatsapp",
                        "to": conversation.contact_phone,
                        "type": message_type,
                    }
                    
                    if message_type == 'text':
                        payload['text'] = {"body": content}
                    elif message_type in ['sticker', 'image', 'video', 'audio', 'document']:
                        import urllib.parse
                        parsed = urllib.parse.urlparse(content)
                        media_url = getattr(settings, 'MEDIA_URL', '/media/')
                        media_id = None
                        
                        if parsed.path.startswith(media_url):
                            relative_path = parsed.path[len(media_url):].lstrip('/')
                            file_path = os.path.join(settings.MEDIA_ROOT, relative_path)
                            if os.path.exists(file_path):
                                try:
                                    media_id = upload_media_to_whatsapp(file_path, phone_number_id, settings.WHATSAPP_API_TOKEN)
                                except Exception as e:
                                    print(f"Error uploading media to WhatsApp: {e}")
                                    
                        if media_id:
                            payload[message_type] = {"id": media_id}
                        else:
                            if parsed.hostname in ['localhost', '127.0.0.1']:
                                public_host = next((h for h in settings.ALLOWED_HOSTS if h not in ['localhost', '127.0.0.1', '*']), None)
                                if public_host:
                                    content = urllib.parse.urlunparse(('https', public_host, parsed.path, parsed.params, parsed.query, parsed.fragment))
                            payload[message_type] = {"link": content}
                    else:
                        payload['type'] = 'text'
                        payload['text'] = {"body": content}

                    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
                    with urllib.request.urlopen(req) as response:
                        pass
                except urllib.error.HTTPError as e:
                    print(f"Error sending WhatsApp message: HTTP Error {e.code}: {e.reason}")
                    try:
                        print(e.read().decode())
                    except Exception:
                        pass
                except Exception as e:
                    print(f"Error sending WhatsApp message: {e}")

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
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        return StickerAsset.objects.filter(created_by=self.request.user)

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

        # Try to extract WhatsApp-like message fields
        try:
            entries = payload.get('entry', [])
            changes = entries[0].get('changes', []) if entries else []
            value = changes[0].get('value', {}) if changes else payload.get('value', {})
            messages = value.get('messages', [])
            msg = messages[0] if messages else None

            if not msg:
                return JsonResponse({'status': 'no_message'}, status=200)

            sender = msg.get('from')
            text = None
            if 'text' in msg and isinstance(msg['text'], dict):
                text = msg['text'].get('body')
            elif msg.get('type') == 'text' and 'text' in msg:
                text = msg.get('text')

            contacts = value.get('contacts', [])
            contact_info = contacts[0] if contacts else {}
            
            contact_name = contact_info.get('profile', {}).get('name', sender)
            whatsapp_username = contact_info.get('username')
            opt_in_state = contact_info.get('opt_in_state', 'not_opted_in')
            wa_id = contact_info.get('wa_id', sender)
            
            contact_phone = wa_id if opt_in_state != 'opted_in_phone_unavailable' else None

            # Find or create a conversation associated with this sender
            conversation, created = Conversation.objects.get_or_create(
                whatsapp_id=wa_id,
                defaults={
                    'contact_name': contact_name,
                    'contact_phone': contact_phone,
                    'whatsapp_username': whatsapp_username,
                    'opt_in_state': opt_in_state,
                }
            )
            
            if not created:
                updated = False
                if whatsapp_username and conversation.whatsapp_username != whatsapp_username:
                    conversation.whatsapp_username = whatsapp_username
                    updated = True
                if opt_in_state and conversation.opt_in_state != opt_in_state:
                    conversation.opt_in_state = opt_in_state
                    updated = True
                if contact_phone and conversation.contact_phone != contact_phone:
                    conversation.contact_phone = contact_phone
                    updated = True
                if updated:
                    conversation.save()

            # Create the inbound message record
            if text is None:
                text = ''

            message = Message.objects.create(
                conversation=conversation,
                direction='inbound',
                message_type='text',
                content=text,
                sender_name=conversation.contact_name,
            )

            conversation.last_message = text
            conversation.last_message_at = timezone.now()
            conversation.save()

            publish_conversation_update(conversation, MessageSerializer(message).data)

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
