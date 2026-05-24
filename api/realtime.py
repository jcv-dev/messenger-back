"""In-process realtime event broadcaster for SSE clients."""

from __future__ import annotations

import json
import queue
import threading
from itertools import count
from typing import Any, Callable


_lock = threading.Lock()
_next_id = count(1)
_next_seq = count(1)
_subscribers: dict[int, dict[str, Any]] = {}


def _current_seq() -> int:
    return next(_next_seq) - 1


def subscribe() -> tuple[int, queue.Queue[str], Callable[[], bool], Callable[[], None]]:
    subscriber_id = next(_next_id)
    event_queue: queue.Queue[str] = queue.Queue(maxsize=1000)
    state = {'queue': event_queue, 'missed': False}
    with _lock:
        _subscribers[subscriber_id] = state

    def check_missed() -> bool:
        with _lock:
            sub = _subscribers.get(subscriber_id)
            return sub['missed'] if sub else False

    def clear_missed() -> None:
        with _lock:
            sub = _subscribers.get(subscriber_id)
            if sub:
                sub['missed'] = False

    return subscriber_id, event_queue, check_missed, clear_missed


def unsubscribe(subscriber_id: int) -> None:
    with _lock:
        _subscribers.pop(subscriber_id, None)


def publish(event: dict[str, Any]) -> None:
    seq = next(_next_seq)
    event['_seq'] = seq
    payload = json.dumps(event, default=str)
    with _lock:
        subscribers = list(_subscribers.items())

    for sub_id, state in subscribers:
        try:
            state['queue'].put_nowait(payload)
        except queue.Full:
            with _lock:
                if sub_id in _subscribers:
                    _subscribers[sub_id]['missed'] = True


def get_last_seq() -> int:
    return _current_seq()