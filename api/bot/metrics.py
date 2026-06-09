"""Simple in-process metrics counters for the bot.

Thread-safe via ``Counter`` from the standard library.
Expose the values via a status endpoint for monitoring.
"""

from __future__ import annotations

import threading
from collections import Counter


class BotMetrics:
    """Thread-safe collection of named counters."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counters: Counter[str] = Counter()

    def incr(self, name: str, delta: int = 1) -> None:
        with self._lock:
            self._counters[name] += delta

    def get(self, name: str) -> int:
        with self._lock:
            return self._counters.get(name, 0)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()


# Module-level singleton
_bot_metrics = BotMetrics()


def get_metrics() -> BotMetrics:
    return _bot_metrics


def incr(name: str, delta: int = 1) -> None:
    _bot_metrics.incr(name, delta)


def snapshot() -> dict[str, int]:
    return _bot_metrics.snapshot()


def reset() -> None:
    _bot_metrics.reset()
