import asyncio
import re
import unicodedata

import httpx
from django.conf import settings

CALCULATOR_BASE = settings.DOMII_CALCULATOR_URL

# ---------------------------------------------------------------------------
#  Input validation helpers
# ---------------------------------------------------------------------------

PLACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-.:]{1,500}$")
ALLOWED_PROFILES = frozenset({"usuario_final", "negocio"})
ALLOWED_PAYMENT_METHODS = frozenset({"efectivo", "nequi"})
ALLOWED_SERVICE_TYPES = frozenset({"domicilios", "mensajeria", "purchases", "tramites", "bancarios"})


def _sanitize(val, max_len=200):
    if not isinstance(val, str):
        return ""
    val = "".join(ch for ch in val if ord(ch) >= 32 or ch in "\n\r\t")
    return val[:max_len].strip()


def _validate_place_id(place_id: str) -> bool:
    if not isinstance(place_id, str) or not place_id:
        return False
    return bool(PLACE_ID_PATTERN.match(place_id))


def _validate_segments(segments) -> list | None:
    if not isinstance(segments, list):
        return None
    for seg in segments:
        if not isinstance(seg, dict):
            return None
        svc = seg.get("service_type", "")
        if svc not in ALLOWED_SERVICE_TYPES:
            return None
        origin = seg.get("origin", {})
        dest = seg.get("destination", {})
        if not isinstance(origin, dict) or not isinstance(dest, dict):
            return None
        if not isinstance(origin.get("address"), str) or not origin["address"].strip():
            return None
        if not isinstance(dest.get("address"), str) or not dest["address"].strip():
            return None
    return segments


# ---------------------------------------------------------------------------
#  Retry wrapper
# ---------------------------------------------------------------------------

MAX_RETRIES = 2
RETRYABLE_CODES = frozenset({429, 500, 502, 503})


async def _request_with_retry(client, method, url, **kwargs):
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = await client.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in RETRYABLE_CODES and attempt < MAX_RETRIES:
                delay = 0.5 * (2 ** attempt)
                await asyncio.sleep(delay)
                last_error = e
                continue
            raise
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            if attempt < MAX_RETRIES:
                delay = 0.5 * (2 ** attempt)
                await asyncio.sleep(delay)
                last_error = e
                continue
            raise
    raise last_error  # pragma: no cover


# ---------------------------------------------------------------------------
#  External API calls
# ---------------------------------------------------------------------------


async def calculate_price(profile, segments, tools=None, payment_method="efectivo", acompanante=False):
    # Validate inputs before sending
    profile = _sanitize(profile, 20)
    if profile not in ALLOWED_PROFILES:
        profile = "usuario_final"
    valid_segments = _validate_segments(segments)
    if valid_segments is None:
        raise ValueError("Invalid segments structure")
    sanitized_segments = []
    for seg in valid_segments:
        sanitized_segments.append({
            "service_type": seg["service_type"],
            "description": _sanitize(seg.get("description", ""), 500),
            "origin": {
                "address": _sanitize(seg["origin"].get("address", ""), 500),
                "lat": seg["origin"].get("lat"),
                "lng": seg["origin"].get("lng"),
            },
            "destination": {
                "address": _sanitize(seg["destination"].get("address", ""), 500),
                "lat": seg["destination"].get("lat"),
                "lng": seg["destination"].get("lng"),
            },
            "instructions": _sanitize(seg.get("instructions", ""), 500),
        })
    payment_method = _sanitize(payment_method, 10)
    if payment_method not in ALLOWED_PAYMENT_METHODS:
        payment_method = "efectivo"
    valid_tools = [_sanitize(t, 50) for t in (tools or []) if isinstance(t, str)]

    async with httpx.AsyncClient(base_url=CALCULATOR_BASE, timeout=15) as client:
        return await _request_with_retry(client, "POST", "/api/calculate-price", json={
            "profile": profile,
            "segments": sanitized_segments,
            "tools": valid_tools,
            "payment_method": payment_method,
            "acompanante": bool(acompanante),
        })


async def geocode_search(query: str):
    query = _sanitize(query, 200)
    if not query:
        return {"error": "Query vacía", "results": []}
    async with httpx.AsyncClient(base_url=CALCULATOR_BASE, timeout=10) as client:
        return await _request_with_retry(client, "GET", "/api/geocode/search", params={"q": query})


async def geocode_details(place_id: str):
    if not _validate_place_id(_sanitize(place_id, 500)):
        return {"error": "place_id inválido"}
    place_id = _sanitize(place_id, 500)
    async with httpx.AsyncClient(base_url=CALCULATOR_BASE, timeout=10) as client:
        return await _request_with_retry(client, "GET", "/api/geocode/details", params={"place_id": place_id})


async def get_tools():
    async with httpx.AsyncClient(base_url=CALCULATOR_BASE, timeout=10) as client:
        resp = await client.get("/api/tools")
        resp.raise_for_status()
        return resp.json()


async def get_whatsapp_config():
    async with httpx.AsyncClient(base_url=CALCULATOR_BASE, timeout=10) as client:
        resp = await client.get("/api/config/whatsapp")
        resp.raise_for_status()
        return resp.json()
