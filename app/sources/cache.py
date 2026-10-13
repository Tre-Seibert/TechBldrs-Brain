"""A small in-memory cache with a time to live, shared by the client resolver and the signed-in technician lookup.

One fetch per key however many threads or coroutines ask at the same time; failures are not cached. The locks
are never held across an await, so it is safe from both threads and coroutines.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
from typing import Any


class TtlCache:
    def __init__(self, ttl: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[Hashable, tuple[float, Any]] = {}
        # One lock per key, so concurrent callers for the same key wait for one fetch.
        self._key_locks: dict[Hashable, threading.Lock] = {}

    def get(self, key: Hashable, fetch: Callable[[], Any]) -> Any:
        """The cached value for `key`, fetching it once (per TTL) however many callers ask at the same time."""
        with self._lock:
            hit = self._fresh(key)
            if hit is not None:
                return hit[1]
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            with self._lock:
                hit = self._fresh(key)
                if hit is not None:
                    return hit[1]
            value = fetch()
            with self._lock:
                now = self._clock()
                for stale in [k for k, (expires, _) in self._entries.items() if expires <= now]:
                    del self._entries[stale]
                self._entries[key] = (now + self._ttl, value)
            return value

    def discard(self, where: Callable[[Any], bool]) -> None:
        """Drop every entry whose key `where` accepts."""
        with self._lock:
            for key in [key for key in self._entries if where(key)]:
                del self._entries[key]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _fresh(self, key: Hashable) -> tuple[float, Any] | None:
        hit = self._entries.get(key)
        return hit if hit is not None and hit[0] > self._clock() else None
