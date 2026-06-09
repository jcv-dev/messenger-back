from django.utils import timezone
from api.models import ConversationTake
from api.views import publish_conversation_update


def get_bot_user():
    from django.contrib.auth.models import User
    return User.objects.filter(username="bot").first()


def release(conversation):
    bot = get_bot_user()
    ConversationTake.objects.filter(
        conversation=conversation,
        created_by=bot,
        expires_at__gt=timezone.now(),
    ).delete()


def handle(conversation, session) -> str:
    release(conversation)
    publish_conversation_update(conversation)
    return "Un asesor de Domii te atenderá pronto. Por favor espera unos momentos."
