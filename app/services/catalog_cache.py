"""In-process cache of the published catalogue.

Every public page used to run the same query two or three times, and a cart of N lines
ran N more. On Neon that is not just latency: each connection keeps the compute awake
for five more minutes, and the free tier is billed by the hour it stays up.

What is cached is a tuple of `ProductSnapshot` — plain data, never SQLAlchemy rows, so
it stays valid after the session that read it is gone.

The cache hangs off the app, not the module: several apps share a process in the tests,
and a module-level cache would serve one app's catalogue to another.

The TTL is short on purpose. Invalidation only reaches the instance that wrote, and on
serverless there are several; a stale read here can end up pinned on the CDN until the
next purge, so the window is kept small. The CDN, not this cache, is what actually keeps
traffic off the database.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional

from flask import current_app, has_app_context

from app.services.catalog_snapshot import ProductSnapshot

DEFAULT_TTL_SECONDS = 300
_EXTENSION_KEY = "catalog_cache"


class CatalogCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshots: Optional[tuple[ProductSnapshot, ...]] = None
        self._index: dict[int, ProductSnapshot] = {}
        self._loaded_at = 0.0
        self._degraded = False

    def _fresh(self) -> bool:
        if self._snapshots is None:
            return False
        ttl = ttl_seconds()
        return ttl > 0 and (time.monotonic() - self._loaded_at) < ttl

    def store(
        self, snapshots: list[ProductSnapshot], *, degraded: bool = False
    ) -> tuple[ProductSnapshot, ...]:
        with self._lock:
            self._snapshots = tuple(snapshots)
            self._index = {snap.tn_id: snap for snap in self._snapshots}
            self._loaded_at = time.monotonic()
            self._degraded = degraded
            return self._snapshots

    def cached(self) -> Optional[tuple[ProductSnapshot, ...]]:
        with self._lock:
            return self._snapshots if self._fresh() else None

    def cached_by_id(self, tn_id: int) -> Optional[ProductSnapshot]:
        with self._lock:
            return self._index.get(int(tn_id)) if self._fresh() else None

    def is_degraded(self) -> bool:
        with self._lock:
            return self._degraded

    def invalidate(self) -> None:
        with self._lock:
            self._snapshots = None
            self._index = {}
            self._loaded_at = 0.0
            self._degraded = False


# Used by the CLI and by anything running outside an app context.
_detached = CatalogCache()


def ttl_seconds() -> int:
    try:
        return int(os.environ.get("CATALOG_CACHE_TTL", DEFAULT_TTL_SECONDS))
    except ValueError:
        return DEFAULT_TTL_SECONDS


def _cache() -> CatalogCache:
    if not has_app_context():
        return _detached
    cache = current_app.extensions.get(_EXTENSION_KEY)
    if cache is None:
        cache = CatalogCache()
        current_app.extensions[_EXTENSION_KEY] = cache
    return cache


def store(
    snapshots: list[ProductSnapshot], *, degraded: bool = False
) -> tuple[ProductSnapshot, ...]:
    """Replace what is cached. `degraded` marks data that came from the Blob fallback."""
    return _cache().store(snapshots, degraded=degraded)


def cached() -> Optional[tuple[ProductSnapshot, ...]]:
    """What is cached, or None when there is nothing fresh."""
    return _cache().cached()


def cached_by_id(tn_id: int) -> Optional[ProductSnapshot]:
    return _cache().cached_by_id(tn_id)


def is_degraded() -> bool:
    """True when what is cached came from the snapshot instead of the database."""
    return _cache().is_degraded()


def invalidate() -> None:
    """Drop the cache after a write. Only affects this process."""
    _cache().invalidate()
