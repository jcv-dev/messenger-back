"""Normalize the ops courier descriptor shared by the detail endpoint and events.

Phase 8 introduced the full descriptor ``{id, name, code}`` in the create
response and in the push events. The public detail endpoint
(``GET /api/v1/orders/{n}``), used by ``Refrescar`` and the link-time backfill,
carries it too since the 2026-09-21 ops fix; older ops builds returned only the
short code string, so both shapes are accepted here.
"""


def courier_snapshot(data: dict | None) -> dict:
    """Return ``{id, name, code}`` from an ops payload (object or legacy string).

    Missing pieces stay out of the dict, so ``{}`` means "ops reports no
    courier" and a legacy string (code only) never clobbers a known name.
    ``courier_code`` (top-level, same shape the push events use) is the
    fallback for payloads that only filled the short code.
    """
    payload = data if isinstance(data, dict) else {}
    raw = payload.get('courier')

    if isinstance(raw, dict):
        snapshot = {
            'id': raw.get('id'),
            'name': str(raw.get('name') or '').strip()[:255],
            'code': str(raw.get('code') or '').strip()[:12],
        }
    elif isinstance(raw, str):
        snapshot = {'code': raw.strip()[:12]}
    else:
        snapshot = {}

    if not snapshot.get('code'):
        code = str(payload.get('courier_code') or '').strip()[:12]
        if code:
            snapshot['code'] = code

    return {key: value for key, value in snapshot.items() if value not in (None, '')}
