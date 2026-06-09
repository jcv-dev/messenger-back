# AGENTS.md — Domi Messager Backend

Python 3.12. Django 5.2 + DRF. Single-app flat structure (`api/` is the only app in `INSTALLED_APPS`).

## Commands

```
python manage.py test api               # run all tests (needs PostgreSQL + Redis)
python manage.py test api --keepdb      # reuse test DB for speed
python manage.py runserver              # dev at :8000
python manage.py migrate                # apply migrations
python manage.py cleanup_expired_messages  # delete old messages (MESSAGE_RETENTION_MINUTES)
```

## Test prerequisites

Tests need **PostgreSQL** (port 5436 per `.env.test`) and **Redis** (port 6379) running. Without Redis, ~65 tests fail with connection errors. Use `docker run -d --name redis-test -p 6379:6379 redis:7` if no Redis is available.

## Architecture

- **`config/`** — Django project (settings, root URLconf, ASGI).
- **`api/`** — single Django app. All models, views, serializers, realtime, bot, management commands.
- **`migration_overrides/`** — overrides Django's built-in `auth` migrations to avoid a non-existent `users` app dependency on this machine. **Do not remove** the `MIGRATION_MODULES['auth']` override.
- Flat structure, no Celery (it's in requirements.txt but unused). No task queue.

## Authentication

- Token auth: `Authorization: Token <key>` header. Obtain via `POST /api-auth/`.
- `/api/bot/status/` requires `IsAdminUser` (enforced via DRF decorator). Non-staff tokens get 403.
- Webhook `/webhook/` is `@csrf_exempt` — HMAC verification, no token auth.

## Bot (critical — separate process)

The bot runs as a **separate async process** managed by supervisord (`python manage.py run_bot`). It is NOT part of uvicorn workers. Key facts:

- **Event loop**: `asyncio.run(bot_loop())` in `api/bot/dispatcher.py`. Subscribes to Redis SSE channel, processes inbound messages sequentially (but spawns concurrent tasks capped by `BOT_MAX_CONCURRENT_TASKS`, default 50).
- **Conversation locking**: `api/bot/lock.py` — Redis `SET NX EX` per conversation (60s TTL). Prevents duplicate processing across workers/restarts. Fails open if Redis is down.
- **LLM**: Gemini via `google-genai`. Tools: `calculate_price`, `geocode_search`, `geocode_details`, `send_interactive`, `escalate_to_human`. Defined in `api/bot/llm.py`.
- **Session storage**: Redis hash `bot:session:{conv_id}` (TTL 600s). Stores LLM chat history, fallback count, and `pending_coords` list.
- **Inbound rate limit**: Per-conversation sliding window in `api/bot/limits.py` (default 10 msg/min).
- **Metrics**: In-process `BotMetrics` counters in `api/bot/metrics.py`. Flushed to Redis hashes (`bot:metrics:{name}`) every 60s by the cleanup loop. Read by `GET /api/bot/status/`. 24h TTL, per-minute buckets.
- **Cleanup loop**: Runs every 60s inside the bot process. Deletes expired takes/tags/notes from DB and publishes SSE events for affected conversations.

## Coordinates override (important gotcha)

LLMs mutate numeric values. To prevent wrong coordinates reaching `calculate_price`:

1. `geocode_details` tool stores API-returned `{lat, lng}` in `session["pending_coords"]`.
2. `calculate_price` tool silently overrides segment origin/destination coordinates with stored values, consuming them in order.
3. The LLM must still call `geocode_details` (null-coordinate validation enforces it) and still triggers the location map + confirmation — but its numeric output is ignored.

## Models — expiry is physical delete, not soft-delete

- `ConversationTake` — `expires_at` is required. Default 30 min duration.
- `ConversationTag`, `ConversationNote` — `expires_at` nullable (`None` = never expires). Expiry types: 1h, 5h, end_of_day, never, custom.
- Expired items are **physically deleted** from the DB. There is no `is_active` field.
- Cleanup triggers: (1) bot cleanup loop every 60s, (2) `metadata` endpoint on conversation detail open, (3) `remove_expired_tags` POST endpoint, (4) `get_queryset` prefetches filter `expires_at__gt=now`.
- The list view NEVER includes expired takes/tags. The frontend shows `time_remaining` (not `duration_minutes`) for the countdown.

## Realtime (SSE)

- **Production**: Redis pub/sub on channel `sse:events`. Uvicorn workers + bot process all subscribe to the same channel.
- **Dev/fallback**: In-process pub/sub via `threading.Lock` + `queue.Queue`.
- SSE auth: POST to `/api/sse-token/` for a one-time 30s token, then GET `/api/events/?sse_token=...`.
- `publish_conversation_update(conv, msg)` broadcasts to all SSE subscribers.

## WhatsApp Integration

- Graph API v20.0. Outbound messages sent via `ThreadPoolExecutor` (32 workers).
- `send_whatsapp_outbound` supports: `text`, `interactive`, `location`, `sticker`, `image`, `video`, `audio`, `document`.
- Location messages accept `content` as dict: `{longitude, latitude, name, address}`.
- WhatsApp rate limiter: `api/rate_limiter.py` — Redis sliding window per `phone_number_id`, 70 req/s default. Blocks until capacity available, fails open after 30s.

## Database

- PostgreSQL. Point at pgbouncer in production (`CONN_MAX_AGE=0`).
- `.env.test` uses port 5436, `.env.example` uses 5434, production env varies.
- `MESSAGE_RETENTION_MINUTES=0` disables retention cleanup.

## Gotchas

- Files `test_*.py` at repo root are manual scripts (call `django.setup()`), NOT Django test-runner tests.
- Uvicorn runs with `--workers 4 --limit-concurrency 50 --limit-max-requests 1000`.
- The bot in-process metrics are per-process (bot process only). The `bot_status` endpoint reads from Redis, which the bot flushes to.
