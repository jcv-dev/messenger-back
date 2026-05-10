"""In-process realtime event broadcaster for SSE clients."""

from __future__ import annotations

import json
import queue
import threading
from itertools import count
from typing import Any


_lock = threading.Lock()
_next_id = count(1)
_subscribers: dict[int, queue.Queue[str]] = {}


def subscribe() -> tuple[int, queue.Queue[str]]:
    subscriber_id = next(_next_id)
    event_queue: queue.Queue[str] = queue.Queue()
    with _lock:
        _subscribers[subscriber_id] = event_queue
    return subscriber_id, event_queue


def unsubscribe(subscriber_id: int) -> None:
    with _lock:
        _subscribers.pop(subscriber_id, None)


def publish(event: dict[str, Any]) -> None:
    payload = json.dumps(event, default=str)
    with _lock:
        subscribers = list(_subscribers.values())

    for subscriber in subscribers:
        try:
            subscriber.put_nowait(payload)
        except queue.Full:
            continue