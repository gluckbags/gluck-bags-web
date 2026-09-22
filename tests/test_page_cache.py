"""Tests for the CDN caching of the public pages (app.services.page_cache).

The whole point is that a visit to a public page can be served by Vercel's CDN and never
reach the function — which is what keeps the Neon compute asleep. Vercel refuses to cache
a response that looks visitor-specific, so what is asserted here is both halves: the
cacheable pages carry the directives and tags, and everything that depends on a session
carries none of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.services import page_cache

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient


CACHEABLE_PATHS = ["/", "/contacto", "/nosotras", "/sitemap.xml", "/robots.txt"]
UNCACHEABLE_PATHS = ["/carrito", "/api/cart", "/gracias", "/admin/"]


def _cdn_header(response: object) -> str:
    return response.headers.get("Vercel-CDN-Cache-Control", "")  # type: ignore[attr-defined]


# --- cacheable pages ---------------------------------------------------------


def test_public_pages_are_marked_cacheable(client: "FlaskClient") -> None:
    for path in CACHEABLE_PATHS:
        response = client.get(path)
        assert response.status_code == 200, path
        assert "s-maxage=" in _cdn_header(response), path
        assert response.headers.get("Vercel-Cache-Tag"), path


def test_public_pages_never_vary_on_cookie(client: "FlaskClient") -> None:
    """A Vary naming Cookie makes Vercel skip the cache outright, which is how the
    storefront ended up sending every visit to Postgres."""
    for path in CACHEABLE_PATHS:
        response = client.get(path)
        assert "cookie" not in response.headers.get("Vary", "").lower(), path
        assert not response.headers.get("Set-Cookie"), path


def test_home_carries_the_catalog_tag(client: "FlaskClient") -> None:
    tags = client.get("/").headers["Vercel-Cache-Tag"].split(",")
    assert page_cache.TAG_CATALOG in tags
    assert page_cache.TAG_CONTENT in tags
    assert page_cache.TAG_SITE in tags


def test_static_pages_do_not_carry_the_catalog_tag(client: "FlaskClient") -> None:
    tags = client.get("/contacto").headers["Vercel-Cache-Tag"].split(",")
    assert page_cache.TAG_CATALOG not in tags
    assert page_cache.TAG_CONTENT in tags


# --- pages that must never be cached -----------------------------------------


def test_session_dependent_pages_are_not_cached(client: "FlaskClient") -> None:
    for path in UNCACHEABLE_PATHS:
        response = client.get(path, follow_redirects=False)
        assert not _cdn_header(response), path
        assert not response.headers.get("Vercel-Cache-Tag"), path


def test_post_is_never_cached(client: "FlaskClient") -> None:
    response = client.post("/api/cart/clear")
    assert not _cdn_header(response)


def test_edit_mode_is_never_cached(auth_client: "FlaskClient") -> None:
    """The editor injects its own markup and payload; a shared cache must never see it."""
    for query in ("?edit=1", "?preview=1"):
        response = auth_client.get(f"/{query}")
        assert not _cdn_header(response), query
        assert not response.headers.get("Vercel-Cache-Tag"), query


def test_a_page_that_read_the_session_is_not_cached(app: "Flask") -> None:
    """The belt-and-braces guard: if a public view ever reads the session again, the
    page stops being cached instead of leaking one visitor's state to everyone."""

    @app.route("/_reads_session")
    def _reads_session() -> str:
        from flask import session

        session.get("anything")
        return "ok"

    # The endpoint is not on the whitelist, so it is not cached for that reason either;
    # what this pins is the tags_for + session guard pairing.
    with app.test_client() as client:
        response = client.get("/_reads_session")
    assert not _cdn_header(response)


# --- tag mapping -------------------------------------------------------------


def test_tags_for_unknown_endpoint_is_none() -> None:
    assert page_cache.tags_for(None, None) is None
    assert page_cache.tags_for("admin.dashboard", None) is None


def test_tags_for_product_includes_its_own_tag() -> None:
    tags = page_cache.tags_for("product_detail", {"product_id": 42})
    assert tags is not None
    assert "product-42" in tags
