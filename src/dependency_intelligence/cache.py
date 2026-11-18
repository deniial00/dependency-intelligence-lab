"""Async TTL/LRU cache (SPEC §6).

Process-local, bounded cache with insertion-time expiry and least-recently-used
eviction. Every read and write of the internal state runs under one
``asyncio.Lock``, so concurrent ``get``/``put``/``size`` calls from many tasks
see a consistent store.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _Entry[V]:
    value: V
    expires_at: float

    def is_expired(self, now: float) -> bool:
        # Boundary is inclusive: an entry is gone at exactly insertion + ttl.
        return now >= self.expires_at


class AsyncTTLCache[K, V]:
    """Bounded TTL cache with LRU eviction.

    - Expiry is fixed at insertion: ``expires_at = clock() + ttl_seconds``.
      An entry is expired when ``clock() >= expires_at``. Reads never extend it.
    - A live read marks the entry most recently used; an expired read removes it.
    - Re-``put`` of an existing key replaces the value, resets its expiry, and
      marks it most recently used.
    - When an insert exceeds ``max_entries``, expired entries are purged first;
      only then is the least recently used live entry evicted.
    - Values are stored opaquely; callers decide what may be cached.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        # Ordered from least to most recently used.
        self._entries: OrderedDict[K, _Entry[V]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: K) -> V | None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.is_expired(self._clock()):
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return entry.value

    async def put(self, key: K, value: V) -> None:
        async with self._lock:
            now = self._clock()
            # pop + insert places the key at the most-recently-used end.
            self._entries.pop(key, None)
            self._entries[key] = _Entry(value, now + self._ttl_seconds)
            if len(self._entries) > self._max_entries:
                self._purge_expired(now)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    async def size(self) -> int:
        async with self._lock:
            self._purge_expired(self._clock())
            return len(self._entries)

    def _purge_expired(self, now: float) -> None:
        """Drop every expired entry. Caller must hold ``self._lock``."""
        expired = [key for key, entry in self._entries.items() if entry.is_expired(now)]
        for key in expired:
            del self._entries[key]
