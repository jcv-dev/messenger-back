"""DeepSeek (OpenAI-compatible) client for the LLM order draft.

Phase 6 needs function calling, so ``chat()`` exposes one chat completion
call (optional tool definitions, optional JSON mode) and normalizes the
assistant message; ``chat_json()`` keeps the simple parsed-JSON helper.
Server-to-server only: the key never reaches the browser.
"""

import json
import logging

import httpx
from django.conf import settings

logger = logging.getLogger('api')

DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 1


class LLMError(Exception):
    """The LLM could not be reached or returned an invalid response."""


class LLMNotConfigured(LLMError):
    """``ORDER_LLM_*`` settings are missing."""


def is_configured() -> bool:
    return bool(
        getattr(settings, 'ORDER_LLM_BASE_URL', '')
        and getattr(settings, 'ORDER_LLM_API_KEY', '')
    )


def _endpoint() -> str:
    base = (getattr(settings, 'ORDER_LLM_BASE_URL', '') or '').rstrip('/')
    if base.endswith('/chat/completions'):
        return base
    return f'{base}/chat/completions'


def _normalize_tool_calls(raw_calls) -> list:
    """Turn OpenAI ``tool_calls`` into ``[{id, name, arguments: dict}]``."""
    calls = []
    for call in raw_calls or []:
        if not isinstance(call, dict):
            continue
        function = call.get('function') or {}
        if not isinstance(function, dict):
            continue
        name = function.get('name') or ''
        if not name:
            continue
        arguments = function.get('arguments')
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except ValueError:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        calls.append({
            'id': call.get('id') or f'call_{len(calls)}',
            'name': name,
            'arguments': arguments,
        })
    return calls


def chat(messages, *, tools=None, json_mode=False, tool_choice=None,
         temperature=None, max_tokens=None, model=None, timeout=None):
    """One chat completion call.

    Returns the assistant message as
    ``{'role': 'assistant', 'content': str, 'tool_calls': [{id, name, arguments}]}``.
    Raises ``LLMNotConfigured`` / ``LLMError`` on failure.
    """
    if not is_configured():
        raise LLMNotConfigured('ORDER_LLM_BASE_URL/ORDER_LLM_API_KEY no configurados.')

    payload = {
        'model': model or getattr(settings, 'ORDER_LLM_MODEL', 'deepseek-flash'),
        'messages': messages,
        'temperature': (
            getattr(settings, 'ORDER_LLM_TEMPERATURE', 0.2)
            if temperature is None else temperature
        ),
        'max_tokens': max_tokens or 1500,
    }
    if json_mode:
        # The provider requires the word "json" somewhere in the prompt.
        payload['response_format'] = {'type': 'json_object'}
    if tools:
        payload['tools'] = tools
        if tool_choice:
            payload['tool_choice'] = tool_choice
    if getattr(settings, 'ORDER_LLM_DISABLE_THINKING', True):
        payload['thinking'] = {'type': 'disabled'}

    headers = {
        'Authorization': f'Bearer {settings.ORDER_LLM_API_KEY}',
        'Content-Type': 'application/json',
    }
    timeout = timeout or getattr(settings, 'ORDER_LLM_TIMEOUT', DEFAULT_TIMEOUT)

    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(_endpoint(), json=payload, headers=headers)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                continue
            if resp.status_code >= 400:
                detail = ''
                try:
                    body = resp.json()
                    if isinstance(body, dict):
                        error = body.get('error')
                        if isinstance(error, dict):
                            detail = error.get('message') or ''
                        elif isinstance(error, str):
                            detail = error
                except ValueError:
                    detail = ''
                raise LLMError(
                    f'El LLM respondió {resp.status_code}' + (f': {detail}' if detail else ''),
                )

            body = resp.json()
            message = (body.get('choices') or [{}])[0].get('message') or {}
            return {
                'role': 'assistant',
                'content': message.get('content') or '',
                'tool_calls': _normalize_tool_calls(message.get('tool_calls')),
            }
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                continue

    logger.warning('LLM request failed: %s', last_error)
    raise LLMError(f'Error consultando el LLM: {last_error}') from last_error


def chat_json(messages, *, tools=None, temperature=None, max_tokens=None,
              model=None, timeout=None):
    """Call in JSON mode and return the parsed dict.

    Raises ``LLMNotConfigured`` / ``LLMError`` on failure. Tool calls are not
    executed here; use ``chat()`` for the tool loop.
    """
    message = chat(
        messages, tools=tools, json_mode=True, temperature=temperature,
        max_tokens=max_tokens, model=model, timeout=timeout,
    )
    content = message.get('content') or ''
    try:
        parsed = json.loads(content)
    except ValueError as exc:
        raise LLMError(f'El LLM no devolvió JSON válido: {exc}') from exc
    if not isinstance(parsed, dict):
        raise LLMError('El LLM no devolvió un objeto JSON.')
    return parsed
