# AGENTS.md — Domi Messager Backend

Python 3.12. Django 6.0 + DRF.

## Commands

```
python manage.py runserver          # dev at :8000
python manage.py test               # run Django tests
python manage.py test api           # test only the api app
python manage.py migrate            # apply migrations
python manage.py makemigrations     # create migrations
python manage.py shell              # Django shell
python manage.py cleanup_expired_messages  # delete old messages per MESSAGE_RETENTION_MINUTES
```

## Architecture

- **`config/`** — Django project (settings, root URLconf, WSGI/ASGI).
- **`api/`** — single Django app (`INSTALLED_APPS = ['api']`). All models, views, serializers, admin, realtime live here.
- **`migration_overrides/`** — custom migration modules that override problematic site-package migrations (see Migration overrides section). Not a Django app.
- No monorepo, no multi-package — flat single-app structure.

## Authentication

- Token auth only (`rest_framework.authentication.TokenAuthentication`).
- Obtain token: `POST /api-auth/` with `username` + `password`.
- All API endpoints require `Authorization: Token <key>` header.
- Webhook (`/webhook/`) is `@csrf_exempt` — no auth required.

## Realtime (SSE)

- In-process pub/sub using `threading.Lock` + `queue.Queue` in `api/realtime.py`. **NOT** Django Channels or Redis.
- SSE endpoint: `/api/events/` (bound directly in `config/urls.py`, not via router).
- After any conversation mutation, call `publish_conversation_update(conv, msg)` to broadcast over SSE.

## WhatsApp Integration

- Sends outbound messages via `threading.Thread(daemon=True)` — failures are logged, never raised.
- Downloads WhatsApp media asynchronously via daemon threads after webhook receipt.
- Webhook endpoint: `/webhook/` handles both GET (verification) and POST (incoming messages).
- Graph API v20.0.
- outbound never sends `edit` or `reaction` message types to WhatsApp (filtered in `send_whatsapp_outbound`).
- **Contextual replies**: Outbound messages can include a `context.message_id` to quote a previous message. Webhook captures `context.id` from incoming messages (all types) and resolves it to the local Message FK (`context_message`). POST to `/messages/` accepts `context_message_id` (local PK) to set up the reply chain.
- **Send flow (async)**: POST to `/messages/` creates the Message record and returns immediately. The daemon thread (`send_whatsapp_outbound`) sends to WhatsApp asynchronously. On success, the message's `whatsapp_message_id` is set and an SSE event is published. On failure, `metadata.send_error` is set and an SSE event is published. The initial POST response does NOT include the message in the SSE event — only the daemon thread's result carries the message data, so the frontend keeps the message in "sending" state until WhatsApp confirms.

## Models (Team removed)

The README describes Team/TeamMember models and team-scoped conversations — these were **removed** in migration 0002. Conversations are now global (not filtered by team). Key models:

- `Conversation` — identified by `whatsapp_id` (unique). Uses cursor-based pagination on list views.
- `Message` — `direction` (inbound/outbound), `message_type`, `media_url`, `metadata` (JSONField). Has `context_message` (nullable FK to self) for contextual replies — points to the Message being replied to. Serialized as `context_message_id` (FK id) + `context_message_preview` (object with content/type/sender_name).
- `ConversationTag`, `ConversationNote`, `ConversationTake` — all soft-delete via `is_active=False`, with time-based expiry.
- `StickerAsset` — reusable images uploaded by users, stored under `MEDIA_ROOT/stickers/%Y/%m/`.

## Env & Settings

- Uses `python-decouple` to read `.env`. Settings file has a fallback if `decouple` is not installed (reads from `os.environ` directly).
- `ALLOWED_HOSTS` and `CORS_ALLOWED_ORIGINS` are **comma-separated** strings, split by settings.
- `.env.example` points to PostgreSQL; the actual `.env` uses PostgreSQL on non-standard port 5434.
- `MESSAGE_RETENTION_MINUTES=0` disables retention cleanup.

## Migration overrides

The project overrides Django's built-in `auth` migrations via `MIGRATION_MODULES['auth'] = 'migration_overrides.auth'`. This is because this development machine has custom auth migrations (0013+) in the site-packages that depend on a non-existent `users` app. The overrides in `migration_overrides/auth/` contain only the standard Django 5.2 auth migrations (0001–0012), ensuring reproducibility on fresh installations. **Do not remove** the override or the directory.

## Gotchas

- The three `test_*.py` files at the repo root are **manual exploratory scripts** that call `django.setup()` and run inline — they are **not** Django test-runner tests. `manage.py test` runs tests in `api/tests.py` (doesn't exist) or elsewhere under `api/`.
- README still documents Team models/endpoints that no longer exist. Trust the code over the README.
- The router in `api/urls.py` uses `DefaultRouter`; custom detail actions (add_tag, add_note, messages, etc.) are defined via `@action` decorators on `ConversationViewSet`.
- Sticker and User resources require `IsAdminUser` for write operations, `IsAuthenticated` for reads.
- Dev server serves media files from `MEDIA_ROOT=media/` when `DEBUG=True` (via `static()` in `config/urls.py`).
- Production uses gunicorn with `--worker-class gthread --threads 4` (gthread workers needed because views spawn daemon threads).
- Dockerfile uses Python 3.10-slim (narrower than `.python-version`'s 3.12.13).
