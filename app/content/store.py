"""A caching layer over sitecopy's override store.

sitecopy memoizes the overrides once per request, which is the right default for a
long-lived worker sharing a database with others. Here every rendered page still costs a
`SELECT * FROM site_texts`, and on Neon's free tier a connection is not free: it keeps
the compute awake for five more minutes.

This wraps the real store and holds that map in the process for a short TTL. It rides
the `store=` option sitecopy already exposes, so nothing in the library is patched.

Two rules keep it honest:

- the admin and any preview/edit request read straight through, so nobody editing copy
  is ever shown a stale draft;
- `commit()` — the single point every one of the panel's writes goes through — drops the
  cache and purges the CDN tag.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Optional

from flask import has_request_context, request
from sitecopy.storage import TextRow, TextStore

DEFAULT_TTL_SECONDS = 60


class CachingTextStore(TextStore):
    """Wraps a `TextStore`, caching its two read maps for `ttl` seconds."""

    def __init__(self, inner: TextStore, ttl: Optional[int] = None) -> None:
        self._inner = inner
        self._ttl = ttl if ttl is not None else _ttl_from_env()
        self._lock = threading.Lock()
        self._as_map: Optional[dict[str, tuple[str | None, str | None]]] = None
        self._previous: Optional[dict[str, str]] = None
        self._loaded_at = 0.0
        self._dirty = False

    # --- reads ---------------------------------------------------------------

    def as_map(self) -> dict[str, tuple[str | None, str | None]]:
        if self._bypass():
            return self._inner.as_map()
        with self._lock:
            if self._as_map is not None and self._fresh():
                return self._as_map
        value = self._inner.as_map()
        with self._lock:
            self._as_map = value
            self._loaded_at = time.monotonic()
        return value

    def previous_map(self) -> dict[str, str]:
        if self._bypass():
            return self._inner.previous_map()
        with self._lock:
            if self._previous is not None and self._fresh():
                return self._previous
        value = self._inner.previous_map()
        with self._lock:
            self._previous = value
            if self._as_map is None:
                self._loaded_at = time.monotonic()
        return value

    def draft_keys(self) -> list[str]:
        # Only the panel asks this, and it must never be stale.
        return self._inner.draft_keys()

    def get(self, key: str) -> TextRow | None:
        return self._inner.get(key)

    # --- writes --------------------------------------------------------------

    def set_draft(self, key: str, value: str | None) -> None:
        self._dirty = True
        self._inner.set_draft(key, value)

    def publish(self, keys: list[str], defaults: dict[str, str]) -> int:
        self._dirty = True
        return self._inner.publish(keys, defaults)

    def discard_drafts(self, keys: list[str]) -> int:
        self._dirty = True
        return self._inner.discard_drafts(keys)

    def commit(self) -> None:
        self._inner.commit()
        if self._dirty:
            self.invalidate()
            self._dirty = False
            _purge_content_tag()

    def rollback(self) -> None:
        self._inner.rollback()
        self.invalidate()
        self._dirty = False

    def ensure_schema(self) -> None:
        self._inner.ensure_schema()

    # --- conveniences the library does not call at runtime --------------------

    def __getattr__(self, name: str) -> Any:
        # `set_published` and `delete` are used by seeding and by sitecopy's test
        # helpers; forwarding keeps this a drop-in for the bundled stores.
        attr = getattr(self._inner, name)
        if name in ("set_published", "delete"):
            self._dirty = True
        return attr

    # --- internals -----------------------------------------------------------

    def invalidate(self) -> None:
        with self._lock:
            self._as_map = None
            self._previous = None
            self._loaded_at = 0.0

    def _fresh(self) -> bool:
        return self._ttl > 0 and (time.monotonic() - self._loaded_at) < self._ttl

    def _bypass(self) -> bool:
        """True for the editor: the panel always reads the database."""
        if not has_request_context():
            return False
        if request.path.startswith("/admin"):
            return True
        return "edit" in request.args or "preview" in request.args


def _ttl_from_env() -> int:
    try:
        return int(os.environ.get("CONTENT_CACHE_TTL", DEFAULT_TTL_SECONDS))
    except ValueError:
        return DEFAULT_TTL_SECONDS


def _purge_content_tag() -> None:
    from app.services import cdn_cache

    cdn_cache.purge_content()
