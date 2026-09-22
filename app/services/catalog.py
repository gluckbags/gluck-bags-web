"""Storefront catalogue facade (headless POC — closing the loop).

The storefront (home, product detail, category, sitemap) and the cart read products
through THIS module instead of talking to a repository directly. That indirection is
the swap seam: a single flag, ``CATALOG_SOURCE``, decides where products come from.

- ``admin`` (code default; dev/rollback) — the admin-managed ``Product`` table, via
  ``ProductRepository``. The pre-migration site.
- ``tiendanube`` (**production**, via Infisical) — the mirrored Tienda Nube catalogue
  (``TiendaNubeProduct``), wrapped
  in a thin adapter so the existing templates render it unchanged. Crucially, in this
  mode a product's ``id`` is its **Tienda Nube product id**, so a cart line carries a
  TN id and the checkout resolver (``mirror_variant_resolver``) maps straight to a TN
  variant — the loop is closed end to end.

The adapter (``StorefrontProduct`` + ``RemoteMedia``) exposes exactly the read surface
the templates and the cart use on a ``Product``/``Media`` (title, price, formatted_price,
cover, media, images, videos, …). Tienda Nube serves plain image URLs with no responsive
variants, so ``RemoteMedia`` advertises a single candidate and lets the ``<picture>``
webp/avif sources fall through to the ``<img>`` — no template change, clean degradation.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any, Optional

from flask import current_app, has_app_context

from app.models import TiendaNubeProduct
from app.repositories import ProductRepository

SOURCE_ADMIN = "admin"
SOURCE_TIENDANUBE = "tiendanube"


class _TextExtractor(HTMLParser):
    """Flatten Tienda Nube's rich-text HTML into plain paragraphs."""

    _BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "br", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")


def html_to_text(value: Optional[str]) -> Optional[str]:
    """Plain text out of TN's HTML descriptions (block tags become line breaks).

    The templates auto-escape whatever they interpolate, so raw HTML from the TN
    admin would otherwise surface as literal "<p>" text on the PDP — and leak
    markup into the JSON-LD/meta descriptions. Rendering it unescaped instead is
    not an option: the mirror is third-party data."""
    if not value:
        return value
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{2,}", "\n\n", text)
    return text.strip() or None


def source() -> str:
    """The active catalogue source, from ``CATALOG_SOURCE`` (default 'admin')."""
    if not has_app_context():
        return SOURCE_ADMIN
    return (current_app.config.get("CATALOG_SOURCE") or SOURCE_ADMIN).strip().lower()


def is_tiendanube() -> bool:
    return source() == SOURCE_TIENDANUBE


# --- Tienda Nube mirror adapters ---------------------------------------------


class RemoteMedia:
    """A ``Media``-compatible view over a single Tienda Nube image URL.

    Tienda Nube has no responsive derivatives, so we advertise one candidate for the
    JPEG ``<img>`` fallback and return an empty srcset for webp/avif — a ``<source>``
    with an empty srcset is skipped by the browser, which then uses the ``<img>``. No
    lie about content type, and no broken image.
    """

    is_image = True
    is_video = False
    has_avif = False
    og_image_url = None

    def __init__(
        self,
        src: str,
        width: Optional[int] = None,
        height: Optional[int] = None,
        media_id: int = 0,
    ) -> None:
        self.src = src
        # Stable per-image id so product_detail's "first image gets fetchpriority"
        # comparison (m.id == first_image.id) picks exactly one image.
        self.id = media_id
        # Sane 4:5 default when Tienda Nube omits dimensions, so <img width/height>
        # still reserves space (avoids layout shift).
        self.width = int(width) if width else 1000
        self.height = int(height) if height else 1250

    @property
    def smallest_width(self) -> int:
        return self.width

    @property
    def largest_width(self) -> int:
        return self.width

    def image_url(self, width: Optional[int] = None, ext: str = "jpg") -> str:
        return self.src

    def image_srcset(self, ext: str = "jpg", max_width: Optional[int] = None) -> str:
        return f"{self.src} {self.width}w" if ext == "jpg" else ""

    @property
    def default_image_url(self) -> str:
        return self.src

    @property
    def thumb_url(self) -> str:
        return self.src

    # Present for interface parity (images-only in the POC; never rendered).
    @property
    def poster_url(self) -> str:
        return self.src

    @property
    def video_url(self) -> str:
        return self.src


class StorefrontProduct:
    """A ``Product``-compatible view over a mirrored ``TiendaNubeProduct`` row.

    ``id`` is the Tienda Nube product id: cart lines built from these carry TN ids, so
    the checkout resolver maps them straight to variants.
    """

    def __init__(self, row: Any) -> None:
        # A live `TiendaNubeProduct` row, or the `ProductSnapshot` the cache and the
        # Blob fallback hold — both expose the same attributes.
        self._row = row
        self._media_cache: Optional[list[RemoteMedia]] = None

    @property
    def id(self) -> int:
        return self._row.tn_id

    @property
    def title(self) -> str:
        return self._row.name

    @property
    def description(self) -> Optional[str]:
        return html_to_text(self._row.description)

    @property
    def category(self) -> Optional[str]:
        return self._row.category

    @property
    def currency(self) -> str:
        return self._row.currency or "ARS"

    @property
    def is_published(self) -> bool:
        return bool(self._row.published)

    @property
    def in_stock(self) -> bool:
        return self._row.in_stock

    @property
    def updated_at(self) -> Any:
        return self._row.updated_at

    @property
    def price(self) -> Optional[int]:
        """Lowest variant price as whole ARS pesos (the storefront/cart use ints)."""
        raw = self._row.price
        if raw in (None, ""):
            return None
        try:
            return int(round(float(raw)))
        except (TypeError, ValueError):
            return None

    @property
    def formatted_price(self) -> Optional[str]:
        price = self.price
        if price is None:
            return None
        return "$ " + f"{price:,.0f}".replace(",", ".")

    def _media(self) -> list[RemoteMedia]:
        if self._media_cache is not None:
            return self._media_cache
        result: list[RemoteMedia] = []
        raw = self._row.raw if isinstance(self._row.raw, dict) else {}
        images = raw.get("images")
        if isinstance(images, list) and images:
            for index, img in enumerate(sorted(images, key=lambda i: (i or {}).get("position") or 0)):
                src = (img or {}).get("src")
                if src:
                    result.append(
                        RemoteMedia(src, img.get("width"), img.get("height"), media_id=img.get("id") or index + 1)
                    )
        else:
            # Fallback: the mirror keeps a flat list of src strings.
            for index, src in enumerate(self._row.images or []):
                if src:
                    result.append(RemoteMedia(src, media_id=index + 1))
        self._media_cache = result
        return result

    @property
    def media(self) -> list[RemoteMedia]:
        return self._media()

    @property
    def images(self) -> list[RemoteMedia]:
        return self._media()

    @property
    def videos(self) -> list[RemoteMedia]:
        # Tienda Nube product videos are external embeds; the POC renders images only.
        return []

    @property
    def cover(self) -> Optional[RemoteMedia]:
        media = self._media()
        return media[0] if media else None


def _published_query():
    return TiendaNubeProduct.query.filter_by(published=True).order_by(TiendaNubeProduct.tn_id.asc())


def _published_snapshots() -> tuple[Any, ...]:
    """The published catalogue as detached data, from the cache when it is fresh.

    One query per process per TTL instead of two or three per request — and when
    Postgres refuses connections, the last snapshot written to the Blob store, so the
    storefront keeps selling instead of returning 500s.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from app.services import catalog_cache, catalog_snapshot

    cached = catalog_cache.cached()
    if cached is not None:
        if catalog_cache.is_degraded():
            _mark_degraded()
        return cached

    try:
        rows = _published_query().all()
    except SQLAlchemyError as exc:
        current_app.logger.error("Catalogue read failed, falling back to snapshot: %s", exc)
        fallback = [snap for snap in catalog_snapshot.load() if snap.published]
        _mark_degraded()
        return catalog_cache.store(fallback, degraded=True)

    return catalog_cache.store(
        [catalog_snapshot.ProductSnapshot.from_row(row) for row in rows]
    )


def _mark_degraded() -> None:
    """Flag the request so page_cache does not pin a fallback page on the CDN."""
    from flask import g, has_request_context

    if has_request_context():
        g.cache_degraded = True


# --- facade (source-dispatching) ---------------------------------------------


def get_published() -> list[Any]:
    if is_tiendanube():
        return [StorefrontProduct(snap) for snap in _published_snapshots()]
    return ProductRepository.get_published()


def get_by_id(product_id: int) -> Any:
    if is_tiendanube():
        from app.services import catalog_cache

        snap = catalog_cache.cached_by_id(int(product_id))
        if snap is None:
            # Warming the whole catalogue is one query for any number of lookups: a
            # cart of N lines used to be N round-trips to Neon.
            snap = next(
                (s for s in _published_snapshots() if s.tn_id == int(product_id)), None
            )
        if snap is not None:
            return StorefrontProduct(snap)
        # Unpublished products are not in the snapshot; the PDP still has to find them
        # to decide between a 404 and a redirect.
        from sqlalchemy.exc import SQLAlchemyError

        try:
            row = TiendaNubeProduct.query.filter_by(tn_id=int(product_id)).one_or_none()
        except SQLAlchemyError:
            return None
        return StorefrontProduct(row) if row is not None else None
    return ProductRepository.get_by_id(product_id)


def get_published_by_category(category: str) -> list[Any]:
    if is_tiendanube():
        return [
            StorefrontProduct(snap)
            for snap in _published_snapshots()
            if snap.category == category
        ]
    return ProductRepository.get_published_by_category(category)


def published_categories() -> list[str]:
    if is_tiendanube():
        seen: list[str] = []
        for snap in _published_snapshots():
            if snap.category and snap.category not in seen:
                seen.append(snap.category)
        return seen
    return ProductRepository.published_categories()


def get_related(product: Any, limit: int = 4) -> list[Any]:
    if is_tiendanube():
        others = [p for p in get_published() if p.id != product.id]
        same = [p for p in others if product.category and p.category == product.category]
        rest = [p for p in others if p not in same]
        return (same + rest)[:limit]
    return ProductRepository.get_related(product, limit)


def is_purchasable(product: Any) -> bool:
    """A product can be bought online: published, priced and in stock.

    Source-agnostic (plain attribute checks). ``Product`` has no ``in_stock``, so it
    defaults to True there — preserving the admin-source behaviour exactly.
    """
    return (
        product is not None
        and getattr(product, "is_published", False)
        and getattr(product, "price", None) is not None
        and getattr(product, "in_stock", True)
    )
