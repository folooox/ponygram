"""
Lightweight async-safe in-memory TTL cache.

Usage:
    cache = TTLCache(ttl=300)          # 5-minute default TTL
    await cache.set("key", value)
    result = await cache.get("key")    # None if expired or missing
    await cache.delete("key")
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional, Tuple


class TTLCache:
    def __init__(self, ttl: int = 300, max_size: int = 1000) -> None:
        self._ttl = ttl
        self._max_size = max_size
        self._store: Dict[str, Tuple[Any, float]] = {}  # key -> (value, expires_at)
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                return None
            return value

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        expires_at = time.monotonic() + (ttl if ttl is not None else self._ttl)
        async with self._lock:
            # Evict oldest entries when at capacity
            if len(self._store) >= self._max_size and key not in self._store:
                oldest = min(self._store, key=lambda k: self._store[k][1])
                del self._store[oldest]
            self._store[key] = (value, expires_at)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()


# Module-level shared caches per domain
movie_cache = TTLCache(ttl=3600, max_size=500)   # 1 hour
book_cache = TTLCache(ttl=3600, max_size=500)
music_cache = TTLCache(ttl=1800, max_size=500)   # 30 minutes
