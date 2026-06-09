"""Bot dispatcher — receives Redis SSE events, routes to LLM handler."""

import asyncio
import json
import logging
import os
import re
import signal
from datetime import timedelta

from asgiref.sync import sync_to_async

from django.contrib.auth.models import User
from django.utils import timezone

from api.models import Conversation, Message, ConversationNote, ConversationTake, ConversationTag
from api.realtime import subscribe, unsubscribe
from api.serializers import MessageSerializer
from api.views import publish_conversation_update, send_whatsapp_outbound, _send_pool

from .constants import WELCOME_REPLY
from .utils import get_bot_user, get_bot_user_async
from .lock import conversation_lock
from .guard import sanitize_user_input
from .limits import check_inbound_rate
from .metrics import incr as incr_metric
from .session import get_session, save_session, delete_session
from .llm import handle_with_llm

logger = logging.getLogger("api.bot")

_shutdown_event = asyncio.Event()
_RECONNECT_MAX_DELAY = 60
_CLEANUP_INTERVAL = 60

_MAX_CONCURRENT = int(os.environ.get("BOT_MAX_CONCURRENT_TASKS", "50"))
_task_semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
_pending_tasks: set[asyncio.Task] = set()


async def _handle_with_semaphore(event: dict):
    async with _task_semaphore:
        try:
            await handle_inbound(event)
        except Exception:
            logger.exception("Error processing event in bot task")


async def _cleanup_expired_items():
    now = await sync_to_async(timezone.now)()

    async def _conv_ids(qs):
        ids = await sync_to_async(lambda: list(qs.values_list('conversation_id', flat=True)))()
        return set(ids)

    affected = set()
    affected |= await _conv_ids(ConversationTake.objects.filter(expires_at__lt=now))
    affected |= await _conv_ids(ConversationTag.objects.filter(expires_at__lt=now, expires_at__isnull=False))
    affected |= await _conv_ids(ConversationNote.objects.filter(expires_at__lt=now, expires_at__isnull=False))

    if not affected:
        return

    await sync_to_async(lambda: ConversationTake.objects.filter(expires_at__lt=now).delete())()
    await sync_to_async(lambda: ConversationTag.objects.filter(expires_at__lt=now, expires_at__isnull=False).delete())()
    await sync_to_async(lambda: ConversationNote.objects.filter(expires_at__lt=now, expires_at__isnull=False).delete())()

    logger.info("Cleaned up expired takes/tags/notes affecting %d conversations", len(affected))

    for conv_id in affected:
        try:
            conversation = await sync_to_async(Conversation.objects.get)(id=conv_id)
            await sync_to_async(publish_conversation_update)(conversation)
        except Conversation.DoesNotExist:
            pass


async def _cleanup_loop():
    while not _shutdown_event.is_set():
        try:
            for _ in range(_CLEANUP_INTERVAL):
                if _shutdown_event.is_set():
                    return
                await asyncio.sleep(1)
            await _cleanup_expired_items()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Error in cleanup loop")
            await asyncio.sleep(10)


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


async def _release_bot_take(conversation):
    bot = await get_bot_user_async()
    if not bot:
        return
    await sync_to_async(lambda: ConversationTake.objects.filter(
        created_by=bot,
        conversation=conversation,
        expires_at__gt=timezone.now(),
    ).update(expires_at=timezone.now()))()
    await sync_to_async(publish_conversation_update)(conversation)


def _renew_bot_take(conversation):
    bot = get_bot_user()
    if not bot:
        return
    ConversationTake.objects.filter(
        created_by=bot,
        conversation=conversation,
        expires_at__gt=timezone.now(),
    ).update(expires_at=timezone.now())
    ConversationTake.create_take(
        conversation=conversation,
        created_by=bot,
        duration_minutes=5,
    )
    publish_conversation_update(conversation)


def send_reply(conversation, text):
    bot = get_bot_user()
    if not bot:
        logger.error("Cannot send reply: bot user does not exist")
        return None
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

    _send_pool.submit(
        send_whatsapp_outbound,
        'text', text, conversation.contact_phone, msg.id, conversation.id,
    )

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

    # --- Concurrency guard: one event per conversation at a time ---
    async with conversation_lock(conversation_id) as locked:
        if not locked:
            incr_metric("locks.failed")
            logger.debug("Dropping duplicate event for conv=%s (already processing)", conversation_id)
            return
        incr_metric("locks.acquired")

        # --- Human take guard ---
        if await sync_to_async(has_active_human_take)(conversation_id):
            return

        # --- Bot-exempt (Domii tag) guard ---
        if await sync_to_async(has_domii_tag)(conversation_id):
            return

        try:
            conversation = await sync_to_async(Conversation.objects.get)(id=conversation_id)
        except Conversation.DoesNotExist:
            return

        await sync_to_async(_renew_bot_take)(conversation)

        # --- Sanitize input ---
        user_text = msg_data.get("content", "").strip()
        user_text = sanitize_user_input(user_text)
        if not user_text:
            logger.debug("Skipping empty/filtered message for conv=%s", conversation_id)
            return

        # --- Inbound rate limit ---
        if not await check_inbound_rate(conversation_id):
            incr_metric("messages.rate_limited")
            logger.warning("Inbound rate limit exceeded for conv=%s", conversation_id)
            return

        incr_metric("messages.processed")

        session = await sync_to_async(get_session)(conversation_id)
        if session is None:
            session = {
                "history": [],
                "fallback_count": 0,
            }

        # --- Pre-emptive escalation on too many failures ---
        if session.get("fallback_count", 0) >= 3:
            logger.info(
                "Escalating conv=%s after %d fallbacks",
                conversation_id, session["fallback_count"],
            )
            await sync_to_async(delete_session)(conversation_id)
            await _release_bot_take(conversation)
            await sync_to_async(send_reply)(
                conversation,
                "He tenido dificultades para ayudarte. Un asesor humano te atender\xe1 pronto.",
            )
            return

        # --- Cancel detection (must be the ONLY content in the message) ---
        cancel_pattern = re.compile(r'^(salir|cancelar|men[úu])[.!?]*\s*$', re.I)
        if cancel_pattern.search(user_text):
            incr_metric("cancellations")
            await sync_to_async(delete_session)(conversation_id)
            await _release_bot_take(conversation)
            await sync_to_async(send_reply)(conversation, WELCOME_REPLY)
            return

        session.setdefault("history", []).append({"role": "user", "content": user_text})

        reply, escalated, sent_interactive = await handle_with_llm(session, conversation)

        session["history"].append({"role": "model", "content": reply})

        if escalated:
            incr_metric("escalations")
            await sync_to_async(delete_session)(conversation_id)
        else:
            await sync_to_async(save_session)(conversation_id, session)

        if not sent_interactive and reply.strip():
            await sync_to_async(send_reply)(conversation, reply)


async def _subscribe_with_retry():
    delay = 1
    while not _shutdown_event.is_set():
        try:
            subscriber_id, event_queue, _, _ = await subscribe()
            return subscriber_id, event_queue
        except Exception:
            logger.warning("Redis subscribe failed, retrying in %ds", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RECONNECT_MAX_DELAY)
    raise asyncio.CancelledError("Shutting down during subscribe retry")


async def bot_loop():
    cleanup_task = asyncio.create_task(_cleanup_loop())
    try:
        while not _shutdown_event.is_set():
            subscriber_id = None
            event_queue = None
            try:
                subscriber_id, event_queue = await _subscribe_with_retry()
                logger.info("Bot subscribed to Redis SSE (id=%s)", subscriber_id)

                bot = await sync_to_async(get_bot_user)()
                if bot:
                    await sync_to_async(lambda: ConversationTake.objects.filter(
                        created_by=bot,
                        expires_at__gt=timezone.now(),
                    ).update(expires_at=timezone.now()))()

                while not _shutdown_event.is_set():
                    try:
                        raw = await asyncio.wait_for(event_queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue

                    try:
                        event = json.loads(raw)
                        task = asyncio.create_task(_handle_with_semaphore(event))
                        _pending_tasks.add(task)
                        task.add_done_callback(_pending_tasks.discard)
                    except Exception:
                        logger.exception("Error creating bot task")

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Bot loop error — restarting in 5s")
                incr_metric("loop.crashes")
                await asyncio.sleep(5)
            finally:
                if subscriber_id is not None:
                    await unsubscribe(subscriber_id)

    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass

    if _pending_tasks:
        logger.info("Waiting for %d pending bot tasks...", len(_pending_tasks))
        try:
            await asyncio.wait_for(
                asyncio.gather(*list(_pending_tasks), return_exceptions=True),
                timeout=30,
            )
        except asyncio.TimeoutError:
            logger.warning("Timed out waiting for %d pending tasks", len(_pending_tasks))

    logger.info("Bot loop shut down gracefully")


def _handle_signal(sig, frame):
    logger.info("Received signal %s, shutting down...", signal.Signals(sig).name)
    _shutdown_event.set()


def run():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    asyncio.run(bot_loop())
