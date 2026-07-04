# AGENTS.md — Domi Messager Backend

Python 3.12. Django 5.2 + DRF. Single-app flat structure (`api/` is the only app in `INSTALLED_APPS`).

## WhatsApp Calling

Calling is implemented via the **WhatsApp Cloud API Calls endpoints** (same Graph API v20.0, same auth).
The backend acts as a **pure signaling relay** — it never touches audio. WebRTC media flows directly
between the browser and WhatsApp servers (optionally relayed through coturn).

### Model

`Call` in `api/models.py` — stores call metadata, SDP offer/answer, recording info, and error details.
Related to `Conversation` via `ForeignKey`. Has `call_id` (unique, from WhatsApp), `direction`
(inbound/outbound), and `status` (pending/ringing/connected/completed/failed/rejected/missed).

### API helpers (`api/views.py`)

- `_call_whatsapp_api()` — POST to WhatsApp `/calls` endpoint, reuses the same rate limiter
- `send_whatsapp_call_action()` — constructs the payload and delegates to `_call_whatsapp_api`
- `pre_accept_call()` / `accept_call()` — two-step WebRTC answer flow (pre_accept → accept)
- `reject_call()` / `terminate_call()` / `initiate_call()` — call lifecycle actions
- `initiate_call()` supports `to_number`, `recipient_bsuid`, and `recording` dict

### Webhook handlers

- `_resolve_conversation()` — finds/creates Conversation from call webhook fields
- `_handle_call_webhook()` — handles `connect`, `terminate`, and `call_recording_available` events
- `_handle_call_status_webhook()` — handles `RINGING`/`ACCEPTED`/`REJECTED` status updates
- `whatsapp_webhook()` modified to process `calls[]` and `statuses[]` arrays before early-returning
- `_publish_call_event()` — publishes `call.*` SSE events with `active_take` ownership info

### REST endpoints (`/api/calls/*`)

All authenticated via `TokenAuthentication` + `IsAuthenticated`:

| Endpoint | Method | Description |
|---|---|---|
| `/api/calls/answer/` | POST | Agent accepts call; sends pre_accept + accept to WhatsApp |
| `/api/calls/reject/` | POST | Agent rejects call |
| `/api/calls/terminate/` | POST | Agent hangs up active call |
| `/api/calls/initiate/` | POST | Agent starts outbound call |
| `/api/calls/list/` | GET | Cursor-paginated call list, optional `conversation_id` filter |
| `/api/calls/active/` | GET | Returns the currently active (pending/ringing/connected) call |
| `/api/calls/turn-config/` | GET | Returns STUN + optional TURN ICE server config |
| `/api/calls/settings/` | GET/POST | Get/update WhatsApp Business calling profile |

### Ownership filtering

`call_list` and `call_active` apply the same `Exists` subquery as conversations:
non-staff users only see calls for conversations they own or that are untaken/bot-taken.

### SSE events

- `call.incoming` — new inbound call, includes `sdp_offer` and `active_take`
- `call.connected` — call was answered (by this agent or another)
- `call.terminated` — call ended
- `call.rejected` — call was rejected
- `call.outgoing_pending` — outbound call initiated, waiting for WhatsApp confirmation
- `call.outgoing_accepted` — WhatsApp accepted outbound call, carries `sdp_answer`
- `call.ringing` — call is ringing on the recipient's side
- `call.recording_available` — recording ready for download

### Ownership routing (`_serialize_call`)

Every SSE call payload includes `active_take` (created_by_id, created_by_username).
The frontend's `_isCallVisibleToMe()` filters events client-side.

### TURN config (`config/settings.py`)

```
TURN_SERVER_URL = config('TURN_SERVER_URL', default='turn:localhost:3478')
TURN_SERVER_USERNAME = config('TURN_SERVER_USERNAME', default='domi')
TURN_SERVER_CREDENTIAL = config('TURN_SERVER_CREDENTIAL', default='')
```

coturn runs via `docker-compose.yml` with `network_mode: host`.

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
- **LLM**: Gemini via `google-genai`. Tools: `calculate_price`, `geocode_search`, `geocode_details`, `send_interactive`, `escalate_to_human`. Defined in `api/bot/llm.py`. Model: `gemini-3.1-flash-lite`. Temperature: `BOT_LLM_TEMPERATURE` (default 0.25). Max output: `BOT_LLM_MAX_OUTPUT_TOKENS` (default 1024).
- **Pre-LLM router**: `api/bot/router.py` intercepts greetings, thanks, and FAQ questions before the LLM. Matched via regex; saves a full LLM call. FAQ responses for hours/payment/coverage/services/Domii Fijo. Only fires on short messages (≤100 chars) with precise question-form patterns to avoid false matches on flow messages.
- **System prompt**: Built dynamically in `_build_system_prompt()` (llm.py). Tool list fetched from calculator service (cached, TTL `BOT_TOOLS_CACHE_TTL`, default 300s). Operating hours come from `BOT_OPERATING_HOURS` env var — used in both the prompt and the FAQ router.
- **Session storage**: Redis hash `bot:session:{conv_id}` (TTL 600s). Stores LLM chat history, fallback count, and `pending_coords` list. History sent to LLM: last 10 entries, each truncated to 300 chars. Stored in Redis: last 20 entries (`HISTORY_MAX_STORED`).
- **Inbound rate limit**: Per-conversation sliding window in `api/bot/limits.py` (default 10 msg/min).
- **Metrics**: In-process `BotMetrics` counters in `api/bot/metrics.py`. Flushed to Redis hashes (`bot:metrics:{name}`) every 60s by the cleanup loop. Read by `GET /api/bot/status/`. 24h TTL, per-minute buckets. New metric: `messages.routed` (counts pre-LLM router hits).
- **Cleanup loop**: Runs every 60s inside the bot process. Deletes expired takes/tags/notes from DB and publishes SSE events for affected conversations.

### State machine (new)

The bot has two modes controlled by the `BOT_STATE_MACHINE` env var (`1`/`true` to enable):

| Mode | Files | Description |
|---|---|---|
| **State machine** (enabled) | `flow.py`, `llm_fallback.py` | State-driven flow, interactive messages, LLM only for free-text classification |
| **Legacy LLM** (disabled — default) | `llm.py`, `router.py` | LLM drives entire conversation (original behavior) |

#### State machine architecture

- **`api/bot/flow.py`** — 31-state machine covering the full quote flow, Domii Fijo, purchases/bancarios, and multi-stop. Each state is an async handler registered via `@_handler(name)` decorator. The main entrypoint is `advance(conversation, session, user_text) -> FlowResult` which returns messages to send, escalation flags, and the next state.
- **`api/bot/llm_fallback.py`** — Minimal Gemini classifier. Called only when the user sends free text instead of tapping a button. Receives current state + valid options, returns the matching button ID (or `"none"`). Tiny prompt (1-16 output tokens), temperature 0.0. Degrades gracefully (returns `None` if Gemini is down).
- **`dispatcher.py`** — `_USE_STATE_MACHINE` flag at top; routes to `_handle_with_state_machine()` or `_handle_with_llm_legacy()`.

#### Session structure (state machine)

```python
{
    "state": "WELCOME",       # current state name
    "fallback_count": 0,      # resets on successful parse, escalate at 2
    "history": [...],         # [{role, content}, ...] for LLM context
    "data": {
        "collected": {
            "profile": None, "service_type": None,
            "segments": [{
                "origin": {"address": "...", "lat": ..., "lng": ..., "confirmed": bool},
                "destination": {...},
                "description": None, "instructions": None,
            }],
            "current_segment": 0,
            "tool_keys": [],
            "payment_method": None, "acompanante": None,
            "recipient_name": None, "recipient_phone": None,
            "geocoded_addresses": [],
        },
    },
    "domii_fijo_data": {...},  # only during Domii Fijo flow
    "pending_coords": [],      # kept for compatibility
}
```

#### Key states

| State | Input type | Description |
|---|---|---|
| `WELCOME` | list | Menu: cotizar, Domii Fijo, FAQ, asesor |
| `AWAITING_PROFILE` | buttons | final / negocio |
| `AWAITING_SERVICE_TYPE` | list | domicilios / mensajeria / purchases / tramites / bancarios |
| `AWAITING_ORIGIN` | free text | Geocode address → show location → confirm |
| `CONFIRMING_ORIGIN` | buttons | Confirm with map |
| `AWAITING_DESTINATION` | free text | Same as origin |
| `CONFIRMING_DEST` | buttons | Confirm with map |
| `AWAITING_MORE_STOPS` | buttons | Multi-stop loop |
| `AWAITING_TOOLS` | list | Dynamic from calculator API |
| `AWAITING_PAYMENT` | buttons | efectivo / nequi |
| `AWAITING_ACOMPANANTE` | buttons | sí / no |
| `SHOW_PRICE` | (internal) | Calls `calculate_price`, shows breakdown |
| `CONFIRMING_QUOTE` | buttons | confirm / change / cancel |
| `AWAITING_RECIPIENT_NAME/PHONE` | free text | Contact info |
| `SUBMIT_ORDER` | (terminal) | Logs order, returns to WELCOME |

#### Escalation (state machine — 6 escape paths)

| Path | Trigger | When |
|---|---|---|
| **Keyword escalation** | `_FAQ_PATTERNS_ESCALATE` regex: agente, asesor, ayuda, no funciona, pásame con, quiero hablar con, etc. | Before state machine runs — any state |
| **Cancel** | `_FAQ_PATTERNS_CANCEL`: salir, cancelar, menú, déjame, ya no quiero, no más | Returns to WELCOME |
| **Confusion** | `_CONFUSION_PATTERNS` regex: no entiendo, repite, explícame, cómo así | 1st → re-explain + escalation hint. **2nd** → escalation offer button |
| **Fallback** | LLM can't classify free text into a button ID | After **2** consecutive → escalate |
| **Menu button** | User taps "Hablar con un asesor" in WELCOME | Direct escalate |
| **Confusion button** | User taps "✅ Sí, por favor" on escalation offer | Direct escalate (button_id="escalate" handled in dispatcher) |

**Escalation notes** include what was already collected (e.g., `"[Bot] Cotizando usuario_final · domicilios. Quedó en: método de pago (2 fallbacks)"`).

## Conversation visibility & take permissions

- **Visibility**: Non-staff users only see conversations that are free, taken by themselves, or taken by the bot. Conversations taken by another human are filtered out via `Exists` subquery in `get_queryset()` (annotates `_has_other_human_take`). Staff sees all.
- **Take permissions**: Any user can take a bot-owned conversation. The `take_conversation` endpoint allows overriding if the existing take's `created_by.username == 'bot'`. Taking deletes ALL existing takes atomically before creating the new one — so the bot won't process the next inbound message (guarded by `has_active_human_take()`).
- **Release permissions**: Any user can release a bot-owned conversation. The `release_conversation` endpoint allows releasing if `active_take.created_by.username == 'bot'`.
- **Search**: `search` action also filters out other-human-taken conversations and excludes conversations with zero messages (annotates `_msg_count=Count('messages')`, filters `> 0`). Uses `Exists` subquery — no extra round-trips.
- **Messages**: `MessageViewSet.get_queryset()` applies the same other-human-take filter — users can't read messages from conversations taken by another human.
- These filters use `Exists(OtherModel.objects.filter(...))` subqueries — evaluated once by the query planner, not per row. No n+1 queries.

## Escalation notes

When the bot escalates a conversation, it automatically creates a `ConversationNote` so the human agent can see a summary without reading history.

| Escalation path | Note content |
|-----------------|-------------|
| LLM calls `escalate_to_human` with `reason` | `[Bot] {reason}` (LLM-written, capped at 250 chars) |
| 3+ `fallback_count` (`dispatcher.py`) | `[Bot] Escalado automáticamente — el bot no pudo procesar la solicitud tras varios intentos` |
| LLM API exception (`llm.py`) | `[Bot] Error del sistema al procesar — escalado automáticamente` |
| No Gemini API key (`llm.py`) | `[Bot] Bot no configurado — escalado automáticamente` |

All notes: `expiry_type='custom'`, `custom_expiry_minutes=10`, `created_by=bot`. The LLM is prompted (in both the tool declaration and the `REGLAS` block) to write a very short summary in `reason` — what the client needed and where the flow stopped.

## SSE events

- `publish_conversation_update(conversation, message=None, escalated=False)` — broadcasts to all subscribers via Redis pub/sub on channel `sse:events`.
- When `escalated=True`, the payload includes `"escalated": true`. The frontend plays an alert sound for all users regardless of tab.
- Released conversations (bot escalation or manual release) have `active_take = null` in the SSE patch — `applyConversationPatch` adds them to every user's list if absent, making them instantly visible.

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
