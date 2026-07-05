"""
Management command to dump bot performance data for analysis.

```
docker compose exec backend python manage.py dump_bot_data --days 1 --anonymize > report.json
```
"""

import json
import time as _time
from datetime import timedelta
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.contrib.auth import get_user_model

from api.models import Conversation, Message, ConversationNote, AgentTakeRecord, BotConfig

User = get_user_model()

CONFUSION_KEYWORDS = [
    "no entiendo", "entiendo", "no sé", "nonese", "ayuda", "no funciona",
    "qué hago", "cómo así", "repite", "explícame", "no me sirve",
    "mal", "error", "otra vez", "repita", "no me gusta", "confuso",
    "no es lo que", "quiero hablar", "asesor", "agente", "persona",
]

BOT_USERNAME = "bot"


def _now():
    return timezone.now()


def _parse_escalation_note(note_content):
    """Extract state and context from an escalation note."""
    result = {"state": None, "reason": None, "collected_profile": None, "collected_service": None}
    if not note_content:
        return result
    result["reason"] = note_content

    # [Bot] Escalado automáticamente en estado {state} — ...
    import re
    m = re.search(r"en estado (\w+)", note_content)
    if m:
        result["state"] = m.group(1)

    # Extract profile and service from parentheses like: usuario_final · domicilios
    ctx_m = re.search(r"— (.+?) \(estado (\w+)\)", note_content)
    if ctx_m:
        ctx = ctx_m.group(1)
        result["state"] = ctx_m.group(2)
        parts = [p.strip() for p in ctx.split("·")]
        if len(parts) >= 1:
            result["collected_profile"] = parts[0]
        if len(parts) >= 2:
            result["collected_service"] = parts[1]

    return result


def _interactive_type(metadata):
    """Get the interactive type from message metadata."""
    if not metadata:
        return None
    if isinstance(metadata, dict):
        return metadata.get("interactive_type") or metadata.get("interactive", {}).get("type")
    return None


def _interactive_buttons(metadata):
    """Extract button IDs from an interactive outbound message."""
    if not metadata or not isinstance(metadata, dict):
        return []
    interactive = metadata.get("interactive") or metadata
    buttons = []
    for section in (interactive.get("sections") or []):
        for row in (section.get("rows") or []):
            if row.get("id"):
                buttons.append(row["id"])
    for btn in (interactive.get("buttons") or []):
        if btn.get("id"):
            buttons.append(btn["id"])
    return buttons


def _get_reply_button_id(metadata):
    """Get the button reply ID from an inbound interactive reply."""
    if not metadata or not isinstance(metadata, dict):
        return None
    reply = metadata.get("interactive_reply") or metadata.get("reply")
    if reply:
        return reply.get("id")
    return metadata.get("button_id")


def _detect_pain_points(messages, conv):
    """Analyze a conversation's message sequence for pain points."""
    pain = {
        "free_text_on_interactive": 0,
        "confusion_expressions": 0,
        "long_user_pauses": 0,
        "bot_retries": 0,
        "rapid_escalation_request": False,
        "user_took_help": False,
    }
    last_was_interactive = False
    last_was_bot_text = False
    for i, msg in enumerate(messages):
        if msg["direction"] == "inbound":
            # Check if user sent free text after bot sent interactive
            if last_was_interactive and msg["message_type"] == "text":
                pain["free_text_on_interactive"] += 1
            # Check confusion keywords
            content_lower = (msg.get("content") or "").lower()
            for kw in CONFUSION_KEYWORDS:
                if kw in content_lower:
                    pain["confusion_expressions"] += 1
                    break
            # Check escalation requests
            if any(w in content_lower for w in ["asesor", "agente", "hablar con una persona"]):
                pain["user_took_help"] = True
            # Check long pauses
            if i > 0 and msg.get("delta_secs") and msg["delta_secs"] > 30:
                pain["long_user_pauses"] += 1
            # Rapid escalation (within first 3 messages)
            if pain["user_took_help"] and i <= 5:
                pain["rapid_escalation_request"] = True
            last_was_interactive = False
            last_was_bot_text = False
        else:
            if msg["sender"] == BOT_USERNAME:
                if msg["message_type"] == "interactive":
                    if last_was_bot_text:
                        pain["bot_retries"] += 1
                    last_was_interactive = True
                    last_was_bot_text = False
                else:
                    last_was_bot_text = True
                    last_was_interactive = False
    return pain


def _get_bot_user():
    try:
        return User.objects.get(username=BOT_USERNAME)
    except User.DoesNotExist:
        return None


class Command(BaseCommand):
    help = "Dump bot performance data for analysis"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=1, help="Days of history to include")
        parser.add_argument("--anonymize", action="store_true", help="Anonymize phone numbers")
        parser.add_argument("--output", type=str, default="-", help="Output file path (- for stdout)")

    def handle(self, *args, **options):
        days = options["days"]
        anonymize = options["anonymize"]
        output = options["output"]
        cutoff = _now() - timedelta(days=days)

        report = {
            "generated_at": _now().isoformat(),
            "period": {"from": cutoff.isoformat(), "to": _now().isoformat(), "days": days},
            "config": self._get_config(),
            "summary": {},
            "escalations": [],
            "flow_timeline": [],
            "pain_points": {},
            "active_sessions": [],
            "metrics_series": {},
        }

        bot_user = _get_bot_user()
        bot_conversations = self._get_bot_conversations(cutoff, bot_user)

        # Build summary
        total = len(bot_conversations)
        resolved = sum(1 for c in bot_conversations if c.resolved_by_bot)
        escalated = sum(1 for c in bot_conversations if not c.resolved_by_bot)
        bot_msg_total = 0
        user_msg_total = 0

        bot_take_records = AgentTakeRecord.objects.filter(
            agent=bot_user,
            taken_at__gte=cutoff,
            first_response_at__isnull=False,
        ) if bot_user else AgentTakeRecord.objects.none()

        response_times = []
        for rec in bot_take_records:
            delta = rec.first_response_at - rec.taken_at
            response_times.append(delta.total_seconds())

        escalations_data = []
        flow_timeline = []
        pain_accum = defaultdict(int)
        pain_conv_count = 0

        for conv in bot_conversations:
            messages = Message.objects.filter(
                conversation=conv,
                created_at__gte=cutoff,
            ).order_by("created_at", "id")
            bot_messages = [m for m in messages if m.sender == bot_user]
            user_messages = [m for m in messages if m.direction == "inbound"]
            bot_msg_total += len(bot_messages)
            user_msg_total += len(user_messages)
            duration_secs = None
            if messages and len(messages) > 1:
                duration_secs = (messages.last().created_at - messages.first().created_at).total_seconds()

            # Build message timeline with deltas
            timeline = []
            prev_ts = None
            for m in messages:
                ts = m.created_at.isoformat()
                delta_secs = None
                if prev_ts is not None:
                    delta_secs = (m.created_at - prev_ts).total_seconds()
                prev_ts = m.created_at

                entry = {
                    "direction": m.direction,
                    "message_type": m.message_type,
                    "content": m.content,
                    "ts": ts,
                    "delta_secs": delta_secs,
                }
                if m.direction == "outbound" and m.sender:
                    entry["sender"] = m.sender.username
                    if m.message_type == "interactive":
                        buttons = _interactive_buttons(m.metadata)
                        if buttons:
                            entry["buttons"] = buttons
                elif m.direction == "inbound":
                    reply_id = _get_reply_button_id(m.metadata)
                    if reply_id:
                        entry["button_reply"] = reply_id
                    interactive_t = _interactive_type(m.metadata)
                    if interactive_t:
                        entry["interactive_type"] = interactive_t
                timeline.append(entry)

            # Pain point analysis
            if timeline:
                pain = _detect_pain_points(timeline, conv)
                for k, v in pain.items():
                    if isinstance(v, bool) and v:
                        pain_accum[k] += 1
                    elif isinstance(v, int):
                        pain_accum[k] += v
                if pain["free_text_on_interactive"] > 0 or pain["confusion_expressions"] > 0:
                    pain_conv_count += 1

            # Check escalation notes
            notes = ConversationNote.objects.filter(
                conversation=conv,
                content__startswith="[Bot]",
                created_at__gte=cutoff,
            ).order_by("-created_at")
            escalation_entry = None
            for note in notes:
                parsed = _parse_escalation_note(note.content)
                escalation_entry = {
                    "conversation_id": conv.id,
                    "contact_name": _anonymize_name(conv.contact_name, anonymize),
                    "contact_phone": _anonymize_phone(conv.contact_phone, anonymize),
                    "note_content": note.content,
                    "note_created_at": note.created_at.isoformat(),
                    **parsed,
                    "bot_msg_count": len(bot_messages),
                    "user_msg_count": len(user_messages),
                    "duration_secs": duration_secs,
                }
                escalations_data.append(escalation_entry)
                break

            # Add to timelines
            flow_entry = {
                "conversation_id": conv.id,
                "contact_name": _anonymize_name(conv.contact_name, anonymize),
                "contact_phone": _anonymize_phone(conv.contact_phone, anonymize),
                "resolved_by_bot": conv.resolved_by_bot,
                "duration_secs": duration_secs,
                "flow": timeline,
                "pain_points": pain,
            }
            flow_timeline.append(flow_entry)

        # Build summary
        avg_response = round(sum(response_times) / len(response_times), 2) if response_times else None
        report["summary"] = {
            "total_bot_conversations": total,
            "resolved_by_bot": resolved,
            "escalated": escalated,
            "total_bot_messages": bot_msg_total,
            "total_user_messages": user_msg_total,
            "avg_bot_response_secs": avg_response,
        }

        report["escalations"] = escalations_data
        report["flow_timeline"] = flow_timeline

        # Aggregate pain points
        pain_stats = {}
        for k, v in sorted(pain_accum.items()):
            pain_stats[k] = v
        pain_stats["conversations_with_pain_points"] = pain_conv_count
        report["pain_points"] = pain_stats

        # Active Redis sessions
        report["active_sessions"] = self._get_active_sessions()

        # Metrics from Redis
        report["metrics_series"] = self._get_metrics()

        # Output
        raw = json.dumps(report, indent=2, ensure_ascii=False, default=str)
        if output == "-":
            self.stdout.write(raw)
        else:
            with open(output, "w", encoding="utf-8") as f:
                f.write(raw)
            self.stdout.write(self.style.SUCCESS(f"Written to {output}"))

    def _get_config(self):
        config = {}
        for entry in BotConfig.objects.all():
            config[entry.key] = entry.value
        return config

    def _get_bot_conversations(self, cutoff, bot_user):
        """Get conversations the bot participated in since cutoff."""
        if not bot_user:
            return Conversation.objects.none()
        bot_msg_ids = Message.objects.filter(
            sender=bot_user,
            created_at__gte=cutoff,
        ).values_list("conversation_id", flat=True).distinct()
        return Conversation.objects.filter(id__in=bot_msg_ids).order_by("-last_message_at")

    def _get_active_sessions(self):
        """Read active bot sessions from Redis."""
        try:
            from api.redis_client import get_sync_redis
            r = get_sync_redis()
            keys = r.keys("bot:session:*")
            sessions = []
            for key in keys:
                conv_id = int(key.split(":")[-1])
                data = r.hgetall(key)
                decoded = {}
                for k, v in data.items():
                    k = k.decode() if isinstance(k, bytes) else k
                    v = v.decode() if isinstance(v, bytes) else v
                    decoded[k] = v
                session_entry = {
                    "conversation_id": conv_id,
                    "state": decoded.get("state", "?"),
                    "fallback_count": decoded.get("fallback_count", "0"),
                    "data_keys": list(decoded.get("data", "{}").keys()) if decoded.get("data") else [],
                    "ttl_secs": r.ttl(key),
                }
                sessions.append(session_entry)
            return sessions
        except Exception as e:
            return [{"error": str(e)}]

    def _get_metrics(self):
        """Read bot metrics from Redis."""
        try:
            from api.redis_client import get_sync_redis
            METRIC_NAMES = [
                "messages.processed",
                "messages.rate_limited",
                "messages.routed",
                "locks.acquired",
                "locks.failed",
                "llm.calls",
                "llm.retries",
                "llm.failures",
                "tool_calls.succeeded",
                "tool_calls.failed",
                "cancellations",
                "escalations",
                "loop.crashes",
            ]
            now = int(_time.time())
            start = now - 86400
            first_minute = start // 60 * 60
            last_minute = now // 60 * 60
            bucket_count = (last_minute - first_minute) // 60 + 1

            r = get_sync_redis()
            pipe = r.pipeline()
            for name in METRIC_NAMES:
                pipe.hgetall(f"bot:metrics:{name}")
            results = pipe.execute()

            metrics = {}
            for name, raw in zip(METRIC_NAMES, results):
                if not raw:
                    continue
                points = []
                for ts_str, count_str in raw.items():
                    ts = int(ts_str) if isinstance(ts_str, bytes) else int(ts_str)
                    count = int(count_str) if isinstance(count_str, bytes) else int(count_str)
                    points.append({"ts": ts, "count": count})
                points.sort(key=lambda x: x["ts"])
                metrics[name] = points
            return metrics
        except Exception as e:
            return {"error": str(e)}


def _anonymize_name(name, enabled):
    if not enabled or not name:
        return name
    if len(name) <= 2:
        return name[0] + "*"
    return name[0] + "*" * (len(name) - 2) + name[-1] if len(name) > 3 else name[0] + "*"


def _anonymize_phone(phone, enabled):
    if not enabled or not phone:
        return phone
    if len(phone) <= 4:
        return phone
    return phone[:3] + "***" + phone[-3:]
