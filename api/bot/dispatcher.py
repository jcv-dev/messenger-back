"""Bot dispatcher — receives Redis SSE events, routes to handlers."""

import asyncio
import json
import logging
import re

from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone

from api.models import Conversation, Message, ConversationTake, ConversationTag
from api.realtime import subscribe, unsubscribe
from api.serializers import MessageSerializer
from api.views import publish_conversation_update

from .session import get_session, save_session, create_session, delete_session
from .classifier import classify
from .handlers import greeting, faq, escalation, fallback, delivery

logger = logging.getLogger("api.bot")


def get_bot_user():
    return User.objects.filter(username="bot").first()


def has_active_human_take(conversation_id) -> bool:
    take = ConversationTake.objects.filter(
        conversation_id=conversation_id,
        expires_at__gt=timezone.now(),
    ).select_related("created_by").first()
    if take and take.created_by and take.created_by.username != "bot":
        return True
    return False


def has_domii_tag(conversation_id) -> bool:
    return ConversationTag.objects.filter(
        conversation_id=conversation_id,
        tag_name="Domii",
        expires_at__isnull=True,
    ).exists()


def send_reply(conversation, text, session=None):
    bot = get_bot_user()
    msg = Message.objects.create(
        conversation=conversation,
        direction="outbound",
        message_type="text",
        content=text,
        sender_name="Bot",
        sender=bot,
    )
    conversation.last_message = text[:255]
    conversation.last_message_at = timezone.now()
    conversation.save(update_fields=["last_message", "last_message_at"])

    msg_data = MessageSerializer(msg).data
    publish_conversation_update(conversation, msg_data)
    return msg


async def handle_inbound(event: dict):
    conv_data = event.get("conversation")
    msg_data = event.get("message")
    if not conv_data or not msg_data:
        return
    if msg_data.get("direction") != "inbound":
        return

    conversation_id = conv_data["id"]

    if has_active_human_take(conversation_id):
        return

    if has_domii_tag(conversation_id):
        return

    try:
        conversation = Conversation.objects.get(id=conversation_id)
    except Conversation.DoesNotExist:
        return

    user_text = msg_data.get("content", "").strip()
    session = get_session(conversation_id)

    if session is None:
        mode = classify(user_text)
        session = create_session(conversation_id, mode=mode)
    elif session.get("state") == "WELCOME" and session.get("mode") == "greeting":
        mode = classify(user_text)
        session["mode"] = mode
        session["state"] = "WELCOME"
        save_session(conversation_id, session)

    session.setdefault("history", []).append({"role": "user", "content": user_text})

    if re.search(r"\b(salir|cancelar|men[uú])\b", user_text, re.I):
        delete_session(conversation_id)
        reply = greeting.welcome_message()
        send_reply(conversation, reply)
        return

    try:
        if session["mode"] == "delivery":
            reply = delivery.advance(session, user_text, conversation)
        elif session["mode"] == "faq":
            reply = faq.handle(session, user_text)
        elif session["mode"] == "escalate":
            reply = escalation.handle(conversation, session)
            delete_session(conversation_id)
        else:
            reply = greeting.handle(session, user_text, conversation)
    except fallback.FallbackRequired:
        session["fallback_count"] = session.get("fallback_count", 0) + 1
        if session["fallback_count"] >= 3:
            reply = escalation.handle(conversation, session)
            delete_session(conversation_id)
        else:
            reply = fallback.message(session)
        save_session(conversation_id, session)
    except Exception:
        logger.exception("Bot handler error for conv %s", conversation_id)
        reply = "Ocurrió un error. Un asesor te atenderá pronto."
        delete_session(conversation_id)
        escalation.release(conversation)

    send_reply(conversation, reply, session)


async def bot_loop():
    subscriber_id, event_queue, _, _ = await subscribe()
    logger.info("Bot subscribed to Redis SSE (id=%s)", subscriber_id)

    bot = get_bot_user()
    if bot:
        ConversationTake.objects.filter(
            created_by=bot,
            expires_at__gt=timezone.now(),
        ).update(
            expires_at=timezone.now(),
        )

    try:
        while True:
            raw = await event_queue.get()
            try:
                event = json.loads(raw)
                await handle_inbound(event)
            except Exception:
                logger.exception("Error processing event")
    finally:
        await unsubscribe(subscriber_id)


def run():
    asyncio.run(bot_loop())
