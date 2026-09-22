"""Tests for the caching wrapper over sitecopy's store (app.content.store).

sitecopy reloads the overrides once per request, which on Neon's free tier is a
connection per rendered page — the reason even `/contacto`, which shows no products,
still woke the database. The wrapper holds them for a short TTL, and the rule that keeps
it safe is that the editor never reads through it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.content.store import CachingTextStore

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient


class _CountingStore:
    """A TextStore stand-in that records how often each read happened."""

    def __init__(self) -> None:
        self.as_map_calls = 0
        self.previous_calls = 0
        self.committed = 0
        self._map: dict[str, tuple[str | None, str | None]] = {}

    def as_map(self) -> dict[str, tuple[str | None, str | None]]:
        self.as_map_calls += 1
        return dict(self._map)

    def previous_map(self) -> dict[str, str]:
        self.previous_calls += 1
        return {}

    def draft_keys(self) -> list[str]:
        return []

    def get(self, key: str) -> None:
        return None

    def set_draft(self, key: str, value: str | None) -> None:
        self._map[key] = (None, value)

    def publish(self, keys: list[str], defaults: dict[str, str]) -> int:
        for key in keys:
            published = (self._map.get(key) or (None, None))[1]
            self._map[key] = (published, None)
        return len(keys)

    def discard_drafts(self, keys: list[str]) -> int:
        return 0

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        pass

    def ensure_schema(self) -> None:
        pass


def test_reads_are_served_from_the_cache(app: "Flask") -> None:
    inner = _CountingStore()
    store = CachingTextStore(inner, ttl=60)

    with app.test_request_context("/"):
        store.as_map()
        store.as_map()
        store.as_map()

    assert inner.as_map_calls == 1


def test_a_save_drops_the_cache(app: "Flask") -> None:
    inner = _CountingStore()
    store = CachingTextStore(inner, ttl=60)

    with app.test_request_context("/"):
        store.as_map()
        store.set_draft("home.hero.title", "Nuevo")
        store.publish(["home.hero.title"], {})
        store.commit()
        assert store.as_map()["home.hero.title"] == ("Nuevo", None)

    assert inner.as_map_calls == 2
    assert inner.committed == 1


def test_a_commit_without_writes_keeps_the_cache(app: "Flask") -> None:
    inner = _CountingStore()
    store = CachingTextStore(inner, ttl=60)

    with app.test_request_context("/"):
        store.as_map()
        store.commit()
        store.as_map()

    assert inner.as_map_calls == 1


def test_the_admin_always_reads_the_database(app: "Flask") -> None:
    """Somebody editing copy must never be shown a cached draft."""
    inner = _CountingStore()
    store = CachingTextStore(inner, ttl=60)

    with app.test_request_context("/admin/content"):
        store.as_map()
        store.as_map()

    assert inner.as_map_calls == 2


def test_preview_and_edit_bypass_the_cache(app: "Flask") -> None:
    inner = _CountingStore()
    store = CachingTextStore(inner, ttl=60)

    with app.test_request_context("/?edit=1"):
        store.as_map()
    with app.test_request_context("/?preview=1"):
        store.as_map()

    assert inner.as_map_calls == 2


def test_a_zero_ttl_disables_the_cache(app: "Flask") -> None:
    inner = _CountingStore()
    store = CachingTextStore(inner, ttl=0)

    with app.test_request_context("/"):
        store.as_map()
        store.as_map()

    assert inner.as_map_calls == 2


# --- wired into the real app -------------------------------------------------


def test_the_copy_table_is_not_queried_on_every_page(client: "FlaskClient") -> None:
    statements: list[str] = []

    def _record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    client.get("/contacto")  # warm
    event.listen(Engine, "before_cursor_execute", _record)
    try:
        client.get("/contacto")
        client.get("/nosotras")
    finally:
        event.remove(Engine, "before_cursor_execute", _record)

    assert [s for s in statements if "site_texts" in s] == []


def test_published_copy_still_reaches_the_public_page(client: "FlaskClient") -> None:
    """The cache must not be able to hide an edit: the save invalidates it."""
    from sitecopy.state import current_store

    from app.factory import db

    client.get("/contacto")  # warm the cache with the defaults
    with client.application.app_context():
        store = current_store()
        store.set_draft("page.contact.title", "Escribinos ahora")
        store.publish(["page.contact.title"], {})
        store.commit()
        db.session.commit()

    assert b"Escribinos ahora" in client.get("/contacto").data
