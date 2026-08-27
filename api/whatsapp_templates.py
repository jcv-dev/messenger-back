"""WhatsApp Template API client — Meta Graph API integration.

All functions communicate with the Meta Graph API v20.0 using the same
urllib + Bearer auth pattern used by ``send_whatsapp_outbound`` in views.py.
"""

from __future__ import annotations

import json
import os
import mimetypes
import re
import uuid
import logging

import urllib.request
import urllib.error

from django.conf import settings

from .rate_limiter import acquire as acquire_rate_capacity

logger = logging.getLogger("api.whatsapp_templates")

_GRAPH_API_VERSION = "v25.0"
_GRAPH_BASE = f"https://graph.facebook.com/{_GRAPH_API_VERSION}"


def _headers() -> dict:
    token = settings.WHATSAPP_API_TOKEN
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _business_id() -> str:
    return settings.WHATSAPP_BUSINESS_ACCOUNT_ID


def _phone_number_id() -> str:
    return getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", None) or settings.WHATSAPP_PHONE_NUMBER


def _request(method: str, url: str, data: dict | None = None) -> dict:
    """Low-level HTTP request to the Meta Graph API."""
    headers = _headers()
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _request_ok(method: str, url: str, data: dict | None = None) -> bool:
    """Send a request and return True on 2xx, False on anything else."""
    headers = _headers()
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True
    except urllib.error.HTTPError:
        return False


_INTERNAL_KEYS = {'parameters', 'image_name'}


def _sanitize_components(components: list[dict]) -> list[dict]:
    """Strip internal metadata keys that Meta doesn't accept, and
    build the ``example`` block for named parameters."""
    cleaned = []
    for comp in components:
        entry = {k: v for k, v in comp.items() if k not in _INTERNAL_KEYS}
        if 'buttons' in entry:
            entry['buttons'] = [
                {k: v for k, v in btn.items() if k not in _INTERNAL_KEYS}
                for btn in entry['buttons']
            ]

        # Image headers: move header_handle into example block for Meta
        if comp.get('type') == 'header' and comp.get('format') == 'image':
            handle = entry.pop('header_handle', None) or comp.get('header_handle')
            logger.info("_sanitize_components: header_handle=%s", handle)
            if handle:
                entry['example'] = {'header_handle': [handle]}
                logger.info("_sanitize_components: example.header_handle=[%s]", handle)
            elif 'example' not in entry:
                entry['example'] = {'header_handle': []}
        elif 'example' in entry:
            entry['example'] = {k: v for k, v in entry['example'].items() if k not in _INTERNAL_KEYS}

        # Build body_text_named_params from internal parameter metadata for Meta review
        if comp.get('type') == 'body':
            text = comp.get('text', '')
            params = comp.get('parameters', [])
            if not params and re.findall(r'\{\{(\w+)\}\}', text):
                logger.warning(
                    "Body component has variables but no parameters — "
                    "template will likely be rejected by Meta"
                )
            if params:
                named_params = []
                for p in params:
                    example_val = p.get('example') or p.get('name', '')
                    named_params.append({
                        'param_name': p['name'],
                        'example': example_val,
                    })
                if named_params:
                    if 'example' not in entry:
                        entry['example'] = {}
                    entry['example']['body_text_named_params'] = named_params

        cleaned.append(entry)
    return cleaned


def create_template(name: str, language: str, category: str,
                    components: list[dict]) -> dict | None:
    """Submit a new template to Meta for review.

    Returns the API response dict (containing ``id`` and ``status``) on
    success, or ``None`` on failure.
    """
    business_id = _business_id()
    if not business_id:
        logger.error("WHATSAPP_BUSINESS_ACCOUNT_ID not configured")
        return None

    url = f"{_GRAPH_BASE}/{business_id}/message_templates"
    payload = {
        "name": name,
        "language": language,
        "category": category.lower(),
        "parameter_format": "named",
        "components": _sanitize_components(components),
    }

    acquire_rate_capacity(_phone_number_id())
    try:
        logger.info("Meta create_template payload: %s", payload)
        return _request("POST", url, payload)
    except urllib.error.HTTPError as e:
        body = e.read().decode() if hasattr(e, "read") else ""
        logger.error("Meta create_template HTTP %s: %s", e.code, body[:500])
        return None
    except Exception:
        logger.exception("Meta create_template network error")
        return None


def list_templates() -> list[dict]:
    """Fetch all templates from Meta.

    Returns a list of dicts (each with ``id``, ``name``, ``status``,
    ``category``, ``language``, ``components``, etc.).
    """
    business_id = _business_id()
    if not business_id:
        return []

    url = f"{_GRAPH_BASE}/{business_id}/message_templates"
    acquire_rate_capacity(_phone_number_id())
    try:
        resp = _request("GET", url)
        return resp.get("data", [])
    except urllib.error.HTTPError as e:
        body = e.read().decode() if hasattr(e, "read") else ""
        logger.error("Meta list_templates HTTP %s: %s", e.code, body[:500])
        return []
    except Exception:
        logger.exception("Meta list_templates network error")
        return []


def delete_template(name: str) -> bool:
    """Delete a template from Meta by name.

    Returns True if successful, False otherwise.
    """
    business_id = _business_id()
    if not business_id:
        return False

    url = f"{_GRAPH_BASE}/{business_id}/message_templates?name={name}"
    acquire_rate_capacity(_phone_number_id())
    return _request_ok("DELETE", url)


def get_template(template_id: str) -> dict | None:
    """Fetch a single template's current status from Meta.

    Returns the template dict (with ``status``, ``quality_score``, etc.)
    or ``None`` on failure.
    """
    url = f"{_GRAPH_BASE}/{template_id}"
    acquire_rate_capacity(_phone_number_id())
    try:
        return _request("GET", url)
    except urllib.error.HTTPError as e:
        body = e.read().decode() if hasattr(e, "read") else ""
        logger.error("Meta get_template HTTP %s: %s", e.code, body[:500])
        return None
    except Exception:
        logger.exception("Meta get_template network error")
        return None


def _app_id() -> str:
    return settings.WHATSAPP_APP_ID


def upload_template_media(file_path: str) -> str | None:
    """Upload an image via Meta's Resumable Upload API for use as a template header.

    Creates an upload session on the Facebook App (``/{app_id}/uploads``),
    then transfers the file data to obtain a media asset handle (``h``)
    suitable for use as ``header_handle`` in template creation.

    See https://developers.facebook.com/docs/graph-api/guides/upload

    Returns the handle string, or ``None`` on failure.
    """
    app_id = _app_id()
    token = settings.WHATSAPP_API_TOKEN
    if not app_id or not token:
        logger.error("WHATSAPP_APP_ID or WHATSAPP_API_TOKEN not configured")
        return None

    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        mime_type = "application/octet-stream"

    with open(file_path, "rb") as f:
        file_data = f.read()

    filename = os.path.basename(file_path)
    file_length = len(file_data)

    # Step 1 — create an upload session
    # POST /{app_id}/uploads?file_name=&file_length=&file_type=&access_token=
    session_url = (
        f"{_GRAPH_BASE}/{app_id}/uploads"
        f"?file_name={filename}"
        f"&file_length={file_length}"
        f"&file_type={mime_type}"
        f"&access_token={token}"
    )

    acquire_rate_capacity(_phone_number_id())
    try:
        req = urllib.request.Request(session_url, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            session_data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if hasattr(e, "read") else ""
        logger.error("Meta upload_template_media session HTTP %s: %s", e.code, err_body[:500])
        return None

    logger.info("Meta upload_template_media session response: %s", session_data)

    session_id = session_data.get("id")
    if not session_id:
        logger.error("Meta upload_template_media: no session id in response: %s", session_data)
        return None

    # Step 2 — upload the file binary to the session
    # POST /upload:{session_id}  Authorization: OAuth {token}  file_offset: 0
    upload_url = f"{_GRAPH_BASE}/{session_id}"
    upload_headers = {
        "Authorization": f"OAuth {token}",
        "file_offset": "0",
    }

    acquire_rate_capacity(_phone_number_id())
    try:
        req = urllib.request.Request(upload_url, data=file_data, headers=upload_headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if hasattr(e, "read") else ""
        logger.error("Meta upload_template_media upload HTTP %s: %s", e.code, err_body[:500])
        return None
    except Exception:
        logger.exception("Meta upload_template_media upload network error")
        return None

    logger.info("Meta upload_template_media upload response: %s", result)

    handle = result.get("h")
    if handle:
        logger.info("Template media uploaded: handle=%s", handle)
        return handle

    logger.error("Meta upload_template_media: no handle in upload response: %s", result)
    return None
