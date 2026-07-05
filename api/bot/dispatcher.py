"""Bot dispatcher — receives Redis SSE events, routes to LLM handler."""

import asyncio
import json
import logging
import os
import re
import signal
import time
from datetime import datetime, timedelta

from asgiref.sync import sync_to_async

from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone

from api.models import Conversation, Message, ConversationNote, ConversationTake, ConversationTag, AgentTakeRecord, create_agent_take_record, release_agent_take_records, set_first_response
from api.realtime import subscribe, unsubscribe
from api.serializers import MessageSerializer
from api.views import publish_conversation_update, send_whatsapp_outbound, _send_pool

from .constants import WELCOME_REPLY
from .utils import get_bot_user, get_bot_user_async
from .lock import conversation_lock
from .guard import sanitize_user_input
from .limits import check_inbound_rate
from . import metrics, flow as state_flow
from .metrics import incr as incr_metric
from .session import get_session, save_session, delete_session, extract_state_from_turn
from .llm import handle_with_llm
from .router import try_route_message
from .config import is_within_operating_hours, get_outside_hours_reply, get_state_machine_enabled, get_max_user_message_length, get_grouped_hours_text, get_testing_warning_enabled

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

    await sync_to_async(lambda: AgentTakeRecord.objects.filter(
        released_at__isnull=True,
        taken_at__lt=now,
    ).update(released_at=now))()
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
    await sync_to_async(lambda: release_agent_take_records(conversation, agent=bot))()
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
    release_agent_take_records(conversation, agent=bot)
    ConversationTake.objects.filter(
        created_by=bot,
        conversation=conversation,
    ).delete()
    ConversationTake.create_take(
        conversation=conversation,
        created_by=bot,
        duration_minutes=5,
    )
    create_agent_take_record(conversation, bot, duration_minutes=5)
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

    conversation._last_msg_direction = 'outbound'
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

        # --- Operating hours gate ---
        if not await sync_to_async(is_within_operating_hours)():
            try:
                import redis.asyncio as aioredis
                r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
                today = datetime.now().strftime('%Y%m%d')
                replied_key = f"bot:outside_hours_replied:{conversation_id}:{today}"
                already_replied = await r.get(replied_key)
                if already_replied:
                    await r.aclose()
                    return
                await r.setex(replied_key, 90000, "1")
                await r.aclose()
            except Exception:
                pass
            reply = await sync_to_async(get_outside_hours_reply)()
            hours = await sync_to_async(get_grouped_hours_text)()
            if hours:
                reply = f"{reply}\n\nNuestro horario:\n{hours}"
            await sync_to_async(send_reply)(conversation, reply)
            return

        await sync_to_async(_renew_bot_take)(conversation)

        # --- Sanitize input ---
        user_text = msg_data.get("content", "").strip()
        user_text = await sync_to_async(sanitize_user_input)(user_text)
        if user_text is None:
            logger.debug("Skipping empty/filtered message for conv=%s", conversation_id)
            return

        # --- Detect interactive button/list reply ---
        button_id = None
        meta = msg_data.get("metadata")
        if isinstance(meta, dict):
            itype = meta.get("interactive_type")
            if itype in ("button_reply", "list_reply"):
                ireply = meta.get("interactive_reply", {})
                button_id = ireply.get("id") or user_text
            elif itype == "button":
                button_id = user_text

        # --- Inbound rate limit ---
        if not await check_inbound_rate(conversation_id):
            incr_metric("messages.rate_limited")
            logger.warning("Inbound rate limit exceeded for conv=%s", conversation_id)
            return

        incr_metric("messages.processed")

        session = await sync_to_async(get_session)(conversation_id)

        # --- Rich first message gate (applies to both bot modes) ---
        if session is None and not button_id:
            address_matches = _extract_address_matches(user_text)
            should_escalate = len(address_matches) >= 2

            if not should_escalate and _is_rich_first_message_heuristic(user_text):
                from .llm_fallback import is_rich_first_message
                should_escalate = await is_rich_first_message(user_text)

            if should_escalate:
                incr_metric("escalations")
                await sync_to_async(delete_session)(conversation_id)
                await _release_bot_take(conversation, escalated=True)
                bot = await get_bot_user_async()
                if bot:
                    if len(address_matches) >= 2:
                        addr_str = '" y "'.join(address_matches)
                        note = f"[Bot] Cliente proporcion\u00f3 {len(address_matches)} direcciones: \"{addr_str}\". Transferido a agente."
                    else:
                        note = "[Bot] Cliente proporcion\u00f3 informaci\u00f3n completa en su primer mensaje. Transferido a agente."
                    await sync_to_async(ConversationNote.create_note)(
                        conversation=conversation,
                        content=note,
                        expiry_type='custom',
                        custom_expiry_minutes=10,
                        created_by=bot,
                    )
                await sync_to_async(send_reply)(
                    conversation,
                    "He revisado tu mensaje y veo que tienes toda la informaci\u00f3n lista. Un asesor te atender\u00e1 para procesar tu solicitud r\u00e1pidamente.",
                )
                return

        if await sync_to_async(get_state_machine_enabled)():
            await _handle_with_state_machine(session, conversation, conversation_id, user_text, button_id)
        else:
            await _handle_with_llm_legacy(session, conversation, conversation_id, user_text)


# ── State machine handler ─────────────────────────────────────────────────

_FAQ_PATTERNS_CANCEL = re.compile(
    r"\b(?:salir|cancelar|cancel|d[eé]jame|"
    r"ya\s+no\s+(?:quiero|necesito)|no\s+m[áa]s)\b",
    re.I,
)
_FAQ_MENU_RETURN = re.compile(
    r"\b(?:men[úu](?:\s+principal)?|"
    r"volver\s+(?:al?\s+)?(?:men[úu]|inicio|empezar|principio)|"
    r"regresar|vuelve\s+a\s+(?:men[úu]|inicio)|"
    r"ir\s+(?:al\s+)?men[úu])\b",
    re.I,
)
_FAQ_PATTERNS_ESCALATE = re.compile(
    r"\b(agente|asesor|humano|persona|operador|"
    r"hablar\s+con|atenci[oó]n|atender|atenderme|"
    r"p[aá]same\s+con|quiero\s+(que\s+)?(me\s+)?(atienda|hable|ayuden|una\s+persona)|"
    r"necesito\s+(ayuda|hablar|una\s+persona|un\s+asesor)|"
    r"ay[uú]dame|ay[uú]da\s+por\s+favor|"
    r"no\s+(funciona|sirve|sirves)|"
    r"esto\s+no|mejor\s+(hablo|llamo|quiero)\s+con|"
    r"comun[ií]came|transfi[eé]reme|"
    r"qu[eé]\s+pereza|qu[eé]\s+fastidio)\b",
    re.I | re.MULTILINE,
)

# Prevent false cancel when "cancelar" means "pagar" in Colombian Spanish
_PAYMENT_WORDS = re.compile(r"\b(cuenta|factura|pago|recibo|total|tarjeta|transferencia|nequi|bancolombia|daviplata|billetera|efectivo|pedido|servicio|pesos|valor)\b", re.I)

# ── Heuristic patterns for "rich first message" detection ────────────────
# These are quick pre-filters before calling the LLM for confirmation.

_RICH_FIRST_ADDRESS = re.compile(
    r"\b(cra\b|calle\b|carrera\b|avenida|transversal|diagonal|autopista|"
    r"barrio|torre\b|apto\b|apartamento|oficina|local\b|km\b|"
    r"nro|n\.?\s*\d|esquina|"
    r"dirección|direccion|donde|ubicación)\b"
    r"|#\d+",
    re.I,
)

_RICH_FIRST_SERVICE = re.compile(
    r"\b(necesito|quiero|solicito|busco|"
    r"enviar|envío|domicilio|paquete|mensajería|"
    r"recoger|recogida|entregar|entrega|llevar|traer|"
    r"pedido|encargo|cotizar|precio)\b",
    re.I,
)

_RICH_FIRST_PERSONAL = re.compile(
    r"\b(soy\b|llamo\b|me\s+llamo|nombre|"
    r"teléfono|telefono|celular|"
    r"efectivo|nequi|pago|tarjeta)\b",
    re.I,
)


def _is_rich_first_message_heuristic(text: str) -> bool:
    """Quick pre-filter: does the message look rich enough to warrant an LLM check?"""
    if len(text) <= 100:
        return False
    score = 0
    if _RICH_FIRST_ADDRESS.search(text):
        score += 1
    if _RICH_FIRST_SERVICE.search(text):
        score += 1
    if _RICH_FIRST_PERSONAL.search(text):
        score += 1
    return score >= 2


# Address extraction pattern — captures full address strings for escalation notes.
# Matches Colombian address format: "Calle 22 #19-19 barrio Rojas"
_ADDRESS_EXTRACT = re.compile(
    r"\b(?:calle|cra\b|carrera|avenida|transversal|diagonal|autopista)"
    r"\s+\S+\s+#\s*\S+"
    r"(?:[ \t]+(?!\b(?:calle|cra\b|carrera|avenida|transversal|diagonal|autopista)\b)\S+){0,4}",
    re.I,
)


def _extract_address_matches(text: str) -> list[str]:
    """Extract distinct address-like strings from *text*.
    
    Returns a deduplicated list of address strings, preserving order.
    """
    matches = _ADDRESS_EXTRACT.findall(text)
    seen = set()
    result = []
    for m in matches:
        m_norm = m.strip().lower()
        if m_norm not in seen and len(m_norm) > 5:
            seen.add(m_norm)
            result.append(m.strip())
    return result


def _is_actual_cancel(user_text: str) -> bool:
    """Check if user_text is a real cancel intent, not Colombian 'cancelar = pagar'."""
    if not _FAQ_PATTERNS_CANCEL.search(user_text):
        return False
    # "cancelar" in Colombia often means "pagar" — skip if payment context
    if "cancelar" in user_text.lower() and _PAYMENT_WORDS.search(user_text):
        return False
    # Long messages (>150 chars) are unlikely to be a pure cancel intent —
    # the keyword is likely embedded in a broader request
    if len(user_text) > 150:
        return False
    return True


async def _handle_with_state_machine(session, conversation, conversation_id, user_text,
                                      button_id: str | None = None):
    """Process one turn using the state machine (flow.py)."""
    result: state_flow.FlowResult

    # --- Load or create session ---
    if session is None:
        if await sync_to_async(get_testing_warning_enabled)():
            await sync_to_async(send_reply)(
                conversation,
                "🤖 *Aviso:* Estamos probando una nueva tecnología (bot automático). Si en cualquier momento necesitas ayuda, solo escribe la palabra *asesor* para comunicarte con un humano."
            )
        session = state_flow.build_initial_session()

    # --- Fallback threshold: 2 failures → escalate ---
    if session.get("fallback_count", 0) >= 2:
        state_name = session.get("state", "desconocido")
        logger.info("Escalating conv=%s after %d fallbacks in state %s (state machine)",
                     conversation_id, session["fallback_count"], state_name)
        await sync_to_async(delete_session)(conversation_id)
        await _release_bot_take(conversation, escalated=True)
        bot = await get_bot_user_async()
        if bot:
            await sync_to_async(ConversationNote.create_note)(
                conversation=conversation,
                content=f"[Bot] Escalado autom\u00e1ticamente en estado {state_name} \u2014 no se pudo procesar la solicitud tras varios intentos",
                expiry_type='custom',
                custom_expiry_minutes=10,
                created_by=bot,
            )
        await sync_to_async(send_reply)(
            conversation,
            "He tenido dificultades para ayudarte. Un asesor humano te atender\u00e1 pronto.",
        )
        return

    # --- Global keywords (checked before state machine) ---
    if not button_id and _FAQ_MENU_RETURN.search(user_text) and not _is_actual_cancel(user_text):
        incr_metric("cancellations")
        await sync_to_async(delete_session)(conversation_id)
        await _release_bot_take(conversation)
        await sync_to_async(send_reply)(
            conversation,
            "De acuerdo, volvamos al men\u00fa principal.",
        )
        return

    if not button_id and _is_actual_cancel(user_text):
        incr_metric("cancellations")
        await sync_to_async(delete_session)(conversation_id)
        await _release_bot_take(conversation)
        await sync_to_async(send_reply)(
            conversation,
            "\u00a1Hasta luego! Cuando necesites algo, solo escr\u00edbeme.",
        )
        return

    if _FAQ_PATTERNS_ESCALATE.search(user_text):
        incr_metric("escalations")
        reason = await state_flow._escalation_note(session, "Cliente solicit\u00f3 hablar con un asesor durante la conversaci\u00f3n")
        await sync_to_async(delete_session)(conversation_id)
        await _release_bot_take(conversation, escalated=True)
        bot = await get_bot_user_async()
        if bot:
            await sync_to_async(ConversationNote.create_note)(
                conversation=conversation,
                content=f"[Bot] {reason}",
                expiry_type='custom',
                custom_expiry_minutes=10,
                created_by=bot,
            )
        await sync_to_async(send_reply)(
            conversation,
            "Un asesor te atender\u00e1 pronto.",
        )
        return

    # --- Button-based escalation (from confusion handler) ---
    if button_id == "escalate":
        incr_metric("escalations")
        reason = await state_flow._escalation_note(session, "Cliente solicit\u00f3 asesor")
        await sync_to_async(delete_session)(conversation_id)
        await _release_bot_take(conversation, escalated=True)
        bot = await get_bot_user_async()
        if bot:
            await sync_to_async(ConversationNote.create_note)(
                conversation=conversation,
                content=f"[Bot] {reason}",
                expiry_type='custom',
                custom_expiry_minutes=10,
                created_by=bot,
            )
        await sync_to_async(send_reply)(
            conversation,
            "Un asesor te atender\u00e1 pronto.",
        )
        return

    # --- FAQ mid-flow ---
    from .router import THANKS_PATTERNS
    clean = user_text.strip().lower()
    if clean in THANKS_PATTERNS:
        await sync_to_async(send_reply)(
            conversation,
            "\u00a1De nada! \u00bfHay algo m\u00e1s en lo que pueda ayudarte?",
        )
        return

    # --- Run state machine ---
    result = await state_flow.advance(conversation, session, user_text, button_id)

    # --- Process result ---
    if result.escalate:
        incr_metric("escalations")
        await sync_to_async(delete_session)(conversation_id)
        reason = result.escalate_reason or "Cliente solicit\u00f3 asesor"
        await _release_bot_take(conversation, escalated=True)
        bot = await get_bot_user_async()
        if bot:
            await sync_to_async(ConversationNote.create_note)(
                conversation=conversation,
                content=f"[Bot] {reason}",
                expiry_type='custom',
                custom_expiry_minutes=10,
                created_by=bot,
            )
        await sync_to_async(send_reply)(
            conversation,
            "Un asesor te atender\u00e1 pronto.",
        )
        return

    if result.fallback:
        session["fallback_count"] = session.get("fallback_count", 0) + 1
    else:
        session["fallback_count"] = 0

    # --- Send messages ---
    for msg in result.messages:
        if msg.get("type") == "text" and msg.get("content"):
            await sync_to_async(send_reply)(conversation, msg["content"])

    if result.send_interactive:
        await sync_to_async(_send_interactive_payload)(conversation, result.send_interactive)

    # --- Save or delete session ---
    new_state = result.state
    if new_state == state_flow.WELCOME and session.get("state") != state_flow.WELCOME:
        await sync_to_async(delete_session)(conversation_id)
    else:
        session.setdefault("history", [])
        session["history"].append({"role": "user", "content": user_text})
        await sync_to_async(save_session)(conversation_id, session)


def _send_interactive_payload(conversation, interactive_payload: dict):
    """Send an interactive message (button/list) to the user."""
    bot = get_bot_user()
    if not bot:
        return
    body_text = interactive_payload.get("body", {}).get("text", "Mensaje interactivo")
    _send_pool.submit(
        send_whatsapp_outbound,
        'interactive',
        interactive_payload,
        conversation.contact_phone,
        None,
        conversation.id,
    )
    msg = Message.objects.create(
        conversation=conversation,
        direction="outbound",
        message_type="interactive",
        content=body_text[:255],
        sender_name="Bot",
        sender=bot,
        metadata={"interactive": interactive_payload},
    )
    conversation.last_message = body_text[:255]
    conversation.last_message_at = timezone.now()
    conversation.save(update_fields=["last_message", "last_message_at"])

    msg_data = MessageSerializer(msg).data
    conversation._last_msg_direction = 'outbound'
    publish_conversation_update(conversation, msg_data)


# ── Legacy LLM handler (unchanged) ────────────────────────────────────────

async def _handle_with_llm_legacy(session, conversation, conversation_id, user_text):
    """Original LLM-driven handler — preserved for backward compat."""
    if session is None:
        if await sync_to_async(get_testing_warning_enabled)():
            await sync_to_async(send_reply)(
                conversation,
                "🤖 *Aviso:* Estamos probando una nueva tecnología (bot automático). Si en cualquier momento necesitas ayuda, solo escribe la palabra *asesor* para comunicarte con un humano."
            )
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
            "He tenido dificultades para ayudarte. Un asesor humano te atender\u00e1 pronto.",
        )
        return

    # --- Menu-return / Cancel detection ---
    if _FAQ_MENU_RETURN.search(user_text) and not _is_actual_cancel(user_text):
        await sync_to_async(delete_session)(conversation_id)
        await _release_bot_take(conversation)
        await sync_to_async(send_reply)(
            conversation,
            "De acuerdo, volvamos al men\u00fa principal.",
        )
        return

    if _is_actual_cancel(user_text):
        incr_metric("cancellations")
        await sync_to_async(delete_session)(conversation_id)
        await _release_bot_take(conversation)
        await sync_to_async(send_reply)(
            conversation,
            "\u00a1Hasta luego! Cuando necesites algo, solo escr\u00edbeme.",
        )
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

    reply, escalated, sent_interactive, function_calls = await handle_with_llm(
        session, conversation,
    )

    session["history"].append({"role": "model", "content": reply})

    if escalated:
        incr_metric("escalations")
        await sync_to_async(delete_session)(conversation_id)
    else:
        extract_state_from_turn(session, user_text)
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
                    await sync_to_async(lambda: release_agent_take_records(None, agent=bot))()
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
