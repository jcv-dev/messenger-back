"""Bot dispatcher — receives Redis SSE events, routes to LLM handler."""

import asyncio
import json
import logging
import os
import re
import signal
import time
from datetime import timedelta

from asgiref.sync import sync_to_async

from django.conf import settings
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
from . import metrics
from .metrics import incr as incr_metric
from .session import get_session, save_session, delete_session
from .llm import handle_with_llm
from .router import try_route_message

logger = logging.getLogger("api.bot")

_shutdown_event = asyncio.Event()
_RECONNECT_MAX_DELAY = 60
_CLEANUP_INTERVAL = 60

_MAX_CONCURRENT = int(os.environ.get("BOT_MAX_CONCURRENT_TASKS", "50"))
_task_semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
_pending_tasks: set[asyncio.Task] = set()

_ESCALATED_KEY = "bot:escalated:{conv_id}"
_ESCALATED_TTL = 600  # 10 minutes


async def _handle_with_semaphore(event: dict):
    async with _task_semaphore:
        try:
            await handle_inbound(event)
        except Exception:
            logger.exception("Error processing event in bot task")


async def _flush_metrics_to_redis():
    snapshot_data = await sync_to_async(metrics.snapshot)()
    if not snapshot_data:
        return

    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        minute_ts = int(time.time()) // 60 * 60
        pipe = r.pipeline()
        for name, count in snapshot_data.items():
            pipe.hincrby(f"bot:metrics:{name}", str(minute_ts), count)
            pipe.expire(f"bot:metrics:{name}", 86400)
        await pipe.execute()
        await r.aclose()
        await sync_to_async(metrics.reset)()
    except Exception:
        logger.exception("Failed to flush metrics to Redis")


async def _cleanup_expired_items():
    await _flush_metrics_to_redis()

    now = await sync_to_async(timezone.now)()

    async def _conv_ids(qs):
        ids = await sync_to_async(lambda: list(qs.values_list('conversation_id', flat=True)))()
        return set(ids)

    # Capture expired bot takes before deletion
    bot = await get_bot_user_async()
    bot_take_conv_ids: set[int] = set()
    if bot:
        bot_take_conv_ids = await _conv_ids(
            ConversationTake.objects.filter(
                created_by=bot,
                expires_at__lt=now,
            )
        )

    affected = set()
    affected |= await _conv_ids(ConversationTake.objects.filter(expires_at__lt=now))
    affected |= await _conv_ids(ConversationTag.objects.filter(expires_at__lt=now, expires_at__isnull=False))
    affected |= await _conv_ids(ConversationNote.objects.filter(expires_at__lt=now, expires_at__isnull=False))

    if not affected:
        return

    await sync_to_async(lambda: ConversationTake.objects.filter(expires_at__lt=now).delete())()
    await sync_to_async(lambda: ConversationTag.objects.filter(expires_at__lt=now, expires_at__isnull=False).delete())()
    await sync_to_async(lambda: ConversationNote.objects.filter(expires_at__lt=now, expires_at__isnull=False).delete())()

    # Mark conversations as resolved_by_bot if their bot take expired without escalation
    for conv_id in bot_take_conv_ids:
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            flag = await r.get(_ESCALATED_KEY.format(conv_id=str(conv_id)))
            await r.aclose()
            if not flag:
                await sync_to_async(Conversation.objects.filter(id=conv_id).update)(
                    resolved_by_bot=True,
                )
        except Exception:
            logger.exception("Error checking escalation for conv=%s", conv_id)

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


async def _release_bot_take(conversation, escalated=False):
    bot = await get_bot_user_async()
    if not bot:
        return
    await sync_to_async(lambda: ConversationTake.objects.filter(
        created_by=bot,
        conversation=conversation,
        expires_at__gt=timezone.now(),
    ).update(expires_at=timezone.now()))()
    await sync_to_async(publish_conversation_update)(conversation, escalated=escalated)

    if escalated:
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            await r.setex(
                _ESCALATED_KEY.format(conv_id=str(conversation.id)),
                _ESCALATED_TTL,
                "1",
            )
            await r.aclose()
        except Exception:
            logger.exception("Failed to set escalation flag for conv=%s", conversation.id)


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

        # --- Reset resolved_by_bot — conversation is active again ---
        if conversation.resolved_by_bot:
            conversation.resolved_by_bot = False
            await sync_to_async(conversation.save)(update_fields=['resolved_by_bot'])

        # --- Escalation cooldown — don't re-take after escalation ---
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            escalated = await r.get(_ESCALATED_KEY.format(conv_id=str(conversation_id)))
            await r.aclose()
            if escalated:
                logger.debug("Skipping conv=%s — escalation cooldown active", conversation_id)
                return
        except Exception:
            pass  # fails open — if Redis is down, don't block

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
            await _release_bot_take(conversation, escalated=True)
            bot = await get_bot_user_async()
            if bot:
                await sync_to_async(ConversationNote.create_note)(
                    conversation=conversation,
                    content="[Bot] Escalado autom\u00e1ticamente \u2014 el bot no pudo procesar la solicitud tras varios intentos",
                    expiry_type='custom',
                    custom_expiry_minutes=10,
                    created_by=bot,
                )
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

        # --- Pre-LLM routing: handle greetings, thanks, FAQ without LLM ---
        routed_reply = try_route_message(session, user_text)
        if routed_reply is not None:
            session.setdefault("history", []).append({"role": "user", "content": user_text})
            session["history"].append({"role": "model", "content": routed_reply})
            await sync_to_async(save_session)(conversation_id, session)
            await sync_to_async(send_reply)(conversation, routed_reply)
            incr_metric("messages.routed")
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
