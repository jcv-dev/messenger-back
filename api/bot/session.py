import json
import time
from django.conf import settings
from api.redis_client import get_sync_redis

SESSION_TTL = 300
REDIS_KEY = "bot:session"


def get_session(conversation_id):
    r = get_sync_redis()
    raw = r.hgetall(f"{REDIS_KEY}:{conversation_id}")
    if not raw:
        return None
    def _maybe_decode(val):
        return val.decode() if isinstance(val, bytes) else val
    session = {
        _maybe_decode(k): json.loads(_maybe_decode(v)) if _maybe_decode(k) in ("data", "history") else _maybe_decode(v)
        for k, v in raw.items()
    }
    if "fallback_count" in session:
        session["fallback_count"] = int(session["fallback_count"])
    if "last_activity" in session:
        session["last_activity"] = float(session["last_activity"])
    return session


def save_session(conversation_id, session):
    r = get_sync_redis()
    mapping = {
        "state": session.get("state", "llm"),
        "mode": session.get("mode", "llm"),
        "data": json.dumps(session.get("data", {})),
        "history": json.dumps(session.get("history", [])),
        "fallback_count": session.get("fallback_count", 0),
        "last_activity": time.time(),
    }
    r.hset(f"{REDIS_KEY}:{conversation_id}", mapping=mapping)
    r.expire(f"{REDIS_KEY}:{conversation_id}", SESSION_TTL)


def delete_session(conversation_id):
    r = get_sync_redis()
    r.delete(f"{REDIS_KEY}:{conversation_id}")


def create_session(conversation_id, mode="greeting"):
    session = {
        "state": "WELCOME",
        "mode": mode,
        "data": {},
        "history": [],
        "fallback_count": 0,
    }
    save_session(conversation_id, session)
    return session
