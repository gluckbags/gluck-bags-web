"""CDN caching for the public pages.

Every visit used to execute the function and open a Postgres connection, and every
connection wakes the Neon compute for five more minutes — which is what exhausted the
free tier on 2026-09-22. A page served from Vercel's CDN never reaches the function, so
this is where most of the saving comes from.

Two things have to be true for Vercel to cache a response: it must carry an `s-maxage`
directive, and it must not look visitor-specific. The second one is why the cart badge
stopped being rendered server side (see `inject_globals` in app/factory.py): reading the
session makes Flask add `Vary: Cookie`, and Vercel refuses to cache any response whose
Vary names Cookie. It also refuses responses with `Set-Cookie`, `no-store` or `private`.

The TTL is deliberately long because `app/services/cdn_cache.py` purges the affected tags
whenever a product or a text changes, so fresh content shows up at once. The TTL is only
the safety net for a purge that failed.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from flask import Flask, Response, g, request, session

# Cache tags, as sent in `Vercel-Cache-Tag` and purged through the REST API.
TAG_SITE = "site"  # everything, for a full purge
TAG_CONTENT = "content"  # anything rendered from the editable copy — that is, all of it
TAG_CATALOG = "catalog"  # pages that list products
TAG_PRODUCT = "product-{tn_id}"  # one product's own page

# Endpoints that render the same HTML for every visitor. A whitelist, never a
# denylist: a new route has to opt in, so nothing personal leaks into a shared cache
# by forgetting to exclude it.
_CATALOG_ENDPOINTS = frozenset({"index", "category_page", "sitemap"})
_CONTENT_ONLY_ENDPOINTS = frozenset({"robots"})
_PRODUCT_ENDPOINT = "product_detail"

DEFAULT_TTL = 86_400  # 1 day
DEFAULT_STALE = 604_800  # 7 days of stale-while-revalidate


def _config(app: Flask) -> tuple[int, int]:
    return (
        int(app.config.get("PAGE_CACHE_TTL", DEFAULT_TTL)),
        int(app.config.get("PAGE_CACHE_STALE", DEFAULT_STALE)),
    )


def configure(app: Flask) -> None:
    """Read the TTLs off the environment, like the rest of the app's switches."""
    app.config["PAGE_CACHE_TTL"] = int(os.environ.get("PAGE_CACHE_TTL", DEFAULT_TTL))
    app.config["PAGE_CACHE_STALE"] = int(os.environ.get("PAGE_CACHE_STALE", DEFAULT_STALE))


def tags_for(endpoint: Optional[str], view_args: Optional[dict[str, Any]]) -> Optional[list[str]]:
    """The cache tags for a request, or None when the page must not be cached."""
    if endpoint is None:
        return None
    if endpoint in _CATALOG_ENDPOINTS:
        return [TAG_SITE, TAG_CONTENT, TAG_CATALOG]
    if endpoint == _PRODUCT_ENDPOINT:
        product_id = (view_args or {}).get("product_id")
        tags = [TAG_SITE, TAG_CONTENT, TAG_CATALOG]
        if product_id is not None:
            tags.append(TAG_PRODUCT.format(tn_id=product_id))
        return tags
    if endpoint in _CONTENT_ONLY_ENDPOINTS or endpoint.startswith("page_"):
        return [TAG_SITE, TAG_CONTENT]
    return None


def _is_cacheable(response: Response) -> bool:
    if request.method not in ("GET", "HEAD"):
        return False
    # Vercel only caches these; a 500 must never be stored in place of a real page.
    if response.status_code not in (200, 404, 301, 302, 307, 308):
        return False
    if response.headers.get("Set-Cookie"):
        return False
    # The editor's markup must never reach a shared cache. sitecopy marks those
    # responses `no-store, private` itself, but the flags are checked here too so this
    # does not depend on the order the after_request hooks happen to run in.
    if "edit" in request.args or "preview" in request.args:
        return False
    existing = response.headers.get("Cache-Control", "")
    if "no-store" in existing or "private" in existing or "no-cache" in existing:
        return False
    # A page served from the degraded path (DB down, snapshot fallback) must not be
    # pinned on the CDN for a day.
    if g.get("cache_degraded"):
        return False
    # Anything that read the session varies per visitor, whatever the endpoint says.
    return not session.accessed


def register_page_cache(app: Flask) -> None:
    """Mark the public pages as cacheable by Vercel's CDN.

    Must be registered BEFORE `register_content`: Flask runs `after_request` hooks in
    reverse registration order, and this one has to see the `no-store` that sitecopy's
    hook puts on preview responses.
    """
    configure(app)

    @app.after_request
    def _page_cache(response: Response) -> Response:
        tags = tags_for(request.endpoint, request.view_args)
        if tags is None or not _is_cacheable(response):
            return response

        ttl, stale = _config(app)
        # The targeted header governs Vercel's cache alone and is not forwarded to the
        # browser, so a visitor still revalidates on every load and never gets stuck
        # with stale HTML, while the CDN keeps serving it for a day.
        response.headers["Vercel-CDN-Cache-Control"] = (
            f"public, s-maxage={ttl}, stale-while-revalidate={stale}"
        )
        response.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
        response.headers["Vercel-Cache-Tag"] = ",".join(tags)
        return response
