"""Tests for the in-process catalogue cache and the snapshot fallback.

Two things are being protected. One is the saving: a page used to run the same query two
or three times and a cart ran one per line, and on Neon every connection keeps the
compute (and the bill) awake. The other is correctness: what the cache holds must
survive the session that read it, and a database that refuses connections must degrade
to the last snapshot instead of a 500 — which is exactly what took the site down on
2026-09-22.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from app.factory import db
from app.models import TiendaNubeProduct
from app.services import catalog, catalog_cache, catalog_snapshot

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient


def _payload(tn_id: int, name: str, price: int, *, category: str = "Tote") -> dict[str, Any]:
    return {
        "id": tn_id,
        "name": {"es": name},
        "handle": {"es": name.lower().replace(" ", "-")},
        "description": {"es": f"Bolso {name}."},
        "published": True,
        "canonical_url": f"https://tienda.example/{tn_id}",
        "categories": [{"id": 1, "name": {"es": category}}],
        "variants": [{"id": tn_id * 10, "price": str(price), "stock": 5, "currency": "ARS"}],
        "images": [
            {
                "id": tn_id * 100,
                "src": f"https://cdn.example/{tn_id}.jpg",
                "position": 1,
                "width": 1080,
                "height": 1350,
            }
        ],
    }


def _seed(app: "Flask", *payloads: dict[str, Any]) -> None:
    with app.app_context():
        for payload in payloads:
            db.session.add(TiendaNubeProduct(tn_id=int(payload["id"])).apply_payload(payload))
        db.session.commit()
        catalog_cache.invalidate()


def _tn(app: "Flask") -> "Flask":
    app.config["CATALOG_SOURCE"] = "tiendanube"
    return app


class _QueryCounter:
    """Counts statements actually sent to the database."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __enter__(self) -> "_QueryCounter":
        event.listen(Engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(Engine, "before_cursor_execute", self._record)

    def _record(self, conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        self.statements.append(statement)

    def on(self, table: str) -> int:
        return len([s for s in self.statements if table in s])


# --- the saving --------------------------------------------------------------


def test_a_second_request_does_not_touch_the_database(app: "Flask") -> None:
    _seed(app, _payload(1, "Tote Cognac", 45000))
    _tn(app)
    client = app.test_client()
    client.get("/")  # warm

    with _QueryCounter() as counter:
        client.get("/")
    assert counter.on("tiendanube_products") == 0


def test_the_catalogue_is_read_once_per_page(app: "Flask") -> None:
    """The home used to run the identical published-products query twice."""
    _seed(app, _payload(1, "Tote Cognac", 45000), _payload(2, "Mini Rosa", 30000))
    _tn(app)

    with _QueryCounter() as counter:
        app.test_client().get("/")
    assert counter.on("tiendanube_products") == 1


def test_a_cart_of_several_lines_is_not_n_plus_one(app: "Flask") -> None:
    _seed(app, _payload(1, "Uno", 1000), _payload(2, "Dos", 2000), _payload(3, "Tres", 3000))
    _tn(app)
    client = app.test_client()
    for tn_id in (1, 2, 3):
        client.post("/api/cart/add", json={"product_id": tn_id})

    with _QueryCounter() as counter:
        client.get("/api/cart")
    assert counter.on("tiendanube_products") <= 1


# --- correctness -------------------------------------------------------------


def test_cached_products_survive_the_session_that_read_them(app: "Flask") -> None:
    """A SQLAlchemy row expires when its session closes; the cache must hold plain data."""
    _seed(app, _payload(1, "Tote Cognac", 45000))
    _tn(app)
    with app.app_context():
        products = catalog.get_published()
        db.session.remove()
        assert products[0].title == "Tote Cognac"
        assert products[0].price == 45000
        assert products[0].cover.src == "https://cdn.example/1.jpg"


def test_a_mirror_write_is_visible_immediately(app: "Flask") -> None:
    from app.services import catalog_sync

    _seed(app, _payload(1, "Nombre viejo", 45000))
    _tn(app)
    with app.app_context():
        assert catalog.get_by_id(1).title == "Nombre viejo"
        catalog_sync.upsert_product(_payload(1, "Nombre nuevo", 45000))
        assert catalog.get_by_id(1).title == "Nombre nuevo"


def test_an_expired_ttl_reloads(app: "Flask", monkeypatch: Any) -> None:
    _seed(app, _payload(1, "Tote Cognac", 45000))
    _tn(app)
    monkeypatch.setenv("CATALOG_CACHE_TTL", "0")
    client = app.test_client()
    client.get("/")

    with _QueryCounter() as counter:
        client.get("/")
    assert counter.on("tiendanube_products") >= 1


def test_the_admin_source_does_not_use_the_cache(app: "Flask") -> None:
    """Under CATALOG_SOURCE=admin the repository returns ORM products with relations;
    caching those across requests is what would detach them."""
    with app.app_context():
        catalog.get_published()
        assert catalog_cache.cached() is None


# --- the fallback ------------------------------------------------------------


def _break_the_database(monkeypatch: Any) -> None:
    def _boom(*args: Any, **kwargs: Any) -> None:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(catalog, "_published_query", _boom)


def test_a_dead_database_serves_the_snapshot(app: "Flask", monkeypatch: Any) -> None:
    _seed(app, _payload(1, "Tote Cognac", 45000))
    _tn(app)
    with app.app_context():
        catalog_snapshot.save(
            [catalog_snapshot.ProductSnapshot.from_row(row) for row in TiendaNubeProduct.query.all()]
        )
    catalog_cache.invalidate()
    _break_the_database(monkeypatch)

    response = app.test_client().get("/")
    assert response.status_code == 200
    assert b"Tote Cognac" in response.data


def test_a_degraded_page_is_never_cached_on_the_cdn(app: "Flask", monkeypatch: Any) -> None:
    """Serving from the snapshot must not pin a possibly-stale page for a day."""
    _seed(app, _payload(1, "Tote Cognac", 45000))
    _tn(app)
    with app.app_context():
        catalog_snapshot.save(
            [catalog_snapshot.ProductSnapshot.from_row(row) for row in TiendaNubeProduct.query.all()]
        )
    catalog_cache.invalidate()
    _break_the_database(monkeypatch)

    response = app.test_client().get("/")
    assert not response.headers.get("Vercel-CDN-Cache-Control")


def test_without_a_snapshot_the_page_still_renders(app: "Flask", monkeypatch: Any) -> None:
    _tn(app)
    catalog_cache.invalidate()
    _break_the_database(monkeypatch)

    response = app.test_client().get("/")
    assert response.status_code == 200


def test_a_snapshot_round_trips(app: "Flask") -> None:
    _seed(app, _payload(1, "Tote Cognac", 45000))
    with app.app_context():
        rows = TiendaNubeProduct.query.all()
        snapshots = [catalog_snapshot.ProductSnapshot.from_row(row) for row in rows]
        restored = catalog_snapshot.deserialize(catalog_snapshot.serialize(snapshots))

    assert [s.tn_id for s in restored] == [1]
    assert restored[0].name == "Tote Cognac"
    assert restored[0].price == "45000"
    assert restored[0].images == ["https://cdn.example/1.jpg"]
    assert restored[0].in_stock is True
