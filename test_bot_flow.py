#!/usr/bin/env python3
"""
Bot flow test script.

Sends WhatsApp webhook POSTs to simulate inbound messages, then drives the bot's
dispatcher directly to see how it responds. Prints the conversation and reply for
each scenario.

Usage:
    python test_bot_flow.py              # run all scenarios
    python test_bot_flow.py delivery     # run one scenario by name

Redis is not required (bypasses pub/sub). Run from backend/ directory.
"""
import os
import sys
import json
import asyncio

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"

import django
django.setup()

from django.test import Client
from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone

from api.models import Conversation, Message, ConversationTag, BotExemptContact
from api.bot.dispatcher import handle_inbound
from api.bot.session import delete_session

# Bypass HMAC verification for test webhook calls
settings.WHATSAPP_APP_SECRET = ""
settings.ALLOWED_HOSTS.append("testserver")

client = Client(SERVER_NAME="testserver")


def ensure_bot_user():
    bot, _ = User.objects.get_or_create(
        username="bot",
        defaults={"first_name": "Domii Bot"},
    )
    return bot


def webhook_payload(from_phone, msg_id, text, contact_name="Test User"):
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": "123456"},
                    "contacts": [{"wa_id": from_phone, "profile": {"name": contact_name}}],
                    "messages": [{
                        "from": from_phone,
                        "id": msg_id,
                        "type": "text",
                        "text": {"body": text},
                        "timestamp": str(int(timezone.now().timestamp())),
                    }],
                }
            }],
        }],
    }


def send_webhook(from_phone, text, seq=0):
    msg_id = f"test.wamid.{from_phone}.{int(timezone.now().timestamp())}.{seq}"
    payload = webhook_payload(from_phone, msg_id, text)
    resp = client.post(
        "/webhook/",
        data=json.dumps(payload),
        content_type="application/json",
    )
    if resp.status_code != 200:
        print(f"  WEBHOOK ERROR {resp.status_code}: {resp.content.decode()[:200]}")
        return None
    return Conversation.objects.filter(whatsapp_id=from_phone).first()


async def drive_bot(conversation, message_content):
    event = {
        "conversation": {"id": conversation.id},
        "message": {
            "direction": "inbound",
            "content": message_content,
        },
    }
    await handle_inbound(event)


def get_replies(conversation):
    return Message.objects.filter(
        conversation=conversation,
        direction="outbound",
        sender__username="bot",
    ).order_by("-created_at")


def cleanup_phone(from_phone):
    Conversation.objects.filter(whatsapp_id=from_phone).delete()
    delete_session(from_phone)


def bold(s):
    return f"\033[1m{s}\033[0m"


TEST_PHONES = {
    "greeting": "573001000001",
    "delivery": "573001000002",
    "escalate": "573001000003",
    "faq": "573001000004",
    "exempt": "573001000005",
    "cancel": "573001000006",
    "fallback": "573001000007",
}


# ── Scenarios ──────────────────────────────────────────────────────────────

async def scenario_greeting():
    """First message → fallback (classifier routes to greeting handler, expects a number)."""
    phone = TEST_PHONES["greeting"]
    cleanup_phone(phone)
    conv = send_webhook(phone, "Hola")
    assert conv
    print(f"  Inbound: 'Hola' → new session, greeting handler")
    await drive_bot(conv, "Hola")
    replies = get_replies(conv)
    if replies:
        print(f"  Bot: {replies[0].content[:150]}...")
    else:
        print("  ⚠ No bot reply")
    print()


async def scenario_delivery():
    """User selects option 1 (delivery quote) → bot asks profile."""
    phone = TEST_PHONES["delivery"]
    cleanup_phone(phone)
    # First message to create session + get welcome/fallback
    conv = send_webhook(phone, "Hola")
    await drive_bot(conv, "Hola")
    # Now send "1" → selects delivery
    send_webhook(phone, "1", seq=1)
    print(f"  Inbound: '1' → delivery mode → SELECT_PROFILE")
    await drive_bot(conv, "1")
    replies = get_replies(conv)
    if replies:
        print(f"  Bot: {replies[0].content[:200]}")
    else:
        print("  ⚠ No bot reply")
    print()


async def scenario_escalate():
    phone = TEST_PHONES["escalate"]
    cleanup_phone(phone)
    conv = send_webhook(phone, "Necesito hablar con un agente")
    assert conv
    print(f"  Inbound: 'Necesito hablar con un agente'")
    await drive_bot(conv, "Necesito hablar con un agente")
    replies = get_replies(conv)
    if replies:
        print(f"  Bot: {replies[0].content[:200]}")
    else:
        print("  ⚠ No bot reply")
    print()


async def scenario_faq():
    phone = TEST_PHONES["faq"]
    cleanup_phone(phone)
    conv = send_webhook(phone, "Cual es el horario")
    assert conv
    print(f"  Inbound: 'Cual es el horario'")
    await drive_bot(conv, "Cual es el horario")
    replies = get_replies(conv)
    if replies:
        print(f"  Bot: {replies[0].content[:200]}")
    else:
        print("  ⚠ No bot reply")
    print()


async def scenario_exempt():
    phone = TEST_PHONES["exempt"]
    cleanup_phone(phone)
    bot = ensure_bot_user()
    BotExemptContact.objects.get_or_create(
        contact_phone=phone,
        defaults={"contact_name": "VIP Client", "created_by": bot},
    )
    conv = send_webhook(phone, "Hola necesito ayuda")
    assert conv
    domii_tag = conv.tags.filter(tag_name="Domii", expires_at__isnull=True).exists()
    print(f"  Domii tag applied: {domii_tag}")
    print(f"  Inbound: 'Hola necesito ayuda'")
    await drive_bot(conv, "Hola necesito ayuda")
    replies = get_replies(conv)
    if not replies:
        print("  Bot SKIPPED (exempt contact) ✓")
    else:
        print("  ⚠ Bot replied despite exempt tag")
    print()


async def scenario_cancel():
    phone = TEST_PHONES["cancel"]
    cleanup_phone(phone)
    conv = send_webhook(phone, "quiero un domicilio")
    assert conv
    print(f"  Inbound: 'quiero un domicilio'")
    await drive_bot(conv, "quiero un domicilio")
    replies = get_replies(conv)
    if replies:
        print(f"  Bot: {replies[0].content[:120]}...")
    send_webhook(phone, "salir", seq=1)
    await drive_bot(conv, "salir")
    replies2 = get_replies(conv)
    if replies2:
        print(f"  After 'salir': {replies2[0].content[:120]}...")
    print()


async def scenario_fallback():
    phone = TEST_PHONES["fallback"]
    cleanup_phone(phone)
    conv = send_webhook(phone, "xyzzy")
    assert conv
    print(f"  Attempt 1: 'xyzzy'")
    await drive_bot(conv, "xyzzy")
    replies = get_replies(conv)
    if replies:
        print(f"  Bot: {replies[0].content[:150]}")

    for i in range(2, 5):
        send_webhook(phone, "plugh", seq=i)
        await drive_bot(conv, "plugh")
        replies = get_replies(conv)
        if replies:
            r = replies[0].content[:150]
            print(f"  Attempt {i}: 'plugh'")
            print(f"  Bot: {r}")
            if "asesor" in r.lower() or "agente" in r.lower():
                print("  → Escalated to human after 3 failures ✓")
                break


# ── Runner ─────────────────────────────────────────────────────────────────

SCENARIOS = {
    "greeting": scenario_greeting,
    "delivery": scenario_delivery,
    "escalate": scenario_escalate,
    "faq": scenario_faq,
    "exempt": scenario_exempt,
    "cancel": scenario_cancel,
    "fallback": scenario_fallback,
}


async def main():
    for phone in TEST_PHONES.values():
        cleanup_phone(phone)

    if len(sys.argv) > 1:
        names = [n for n in sys.argv[1:] if n in SCENARIOS]
        if not names:
            print(f"Unknown scenario. Choose: {', '.join(SCENARIOS)}")
            sys.exit(1)
    else:
        names = list(SCENARIOS)

    for name in names:
        print(f"{'='*60}")
        print(f"  Scenario: {bold(name)}")
        print(f"{'='*60}")
        await SCENARIOS[name]()


if __name__ == "__main__":
    asyncio.run(main())
