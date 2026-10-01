"""Schema.org JSON-LD builders for rich results.

Plain dicts so they're unit-testable; routes serialize them with `json.dumps` and
the templates emit `<script type="application/ld+json">`. Kept faithful to the
visible page: `offers` is emitted only when a real price exists (unpriced products
render "Consultar" and get no Offer) — fabricated markup risks a manual spam action.
"""

from __future__ import annotations

import json
from typing import Any

from app.models import Product
from app.utils import slugify

# Fallbacks only. The live values are editable from the admin (app/content), and the
# routes pass them in — these keep the builders pure and directly unit-testable.
BRAND = "GLÜCK"
_LOGO = "/static/img/marca/avatar-perfil-gluck.jpg"
INSTAGRAM = "https://www.instagram.com/gluck_bags/"
ORGANIZATION_DESCRIPTION = (
    "Bolsos y carteras de cuero vegano hechos a mano en Argentina, "
    "con diseño minimalista y atemporal."
)
HOME_BREADCRUMB = "Inicio"
# People search the brand without the umlaut ("gluck", "gluck bags"). Google reads
# alternateName to pick the SERP site name and to tie those spellings to this entity.
ALTERNATE_NAMES = "Gluck, Gluck Bags, Glück"


def parse_alternate_names(raw: str | None, brand: str) -> list[str]:
    """Split the editable comma-separated list, dropping blanks, repeats and the
    brand itself (an alternateName equal to `name` adds nothing)."""
    names: list[str] = []
    for part in (ALTERNATE_NAMES if raw is None else raw).split(","):
        name = part.strip()
        if name and name != brand and name not in names:
            names.append(name)
    return names


def dump_jsonld(obj: Any) -> str:
    """Serialize JSON-LD for safe embedding in a <script type="application/ld+json">.

    json.dumps escapes quotes/backslashes but NOT '<', '>' or '&', so a string
    containing '</script>' (e.g. a product description) would break out of the
    element — an XSS vector. Escape those (and the JS line separators) as JSON
    \\uXXXX, which is still valid JSON the parser decodes back to the originals."""
    return (
        json.dumps(obj, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def organization_jsonld(
    site_url: str,
    brand: str | None = None,
    instagram: str | None = None,
    description: str | None = None,
    alternate_names: str | None = None,
) -> dict[str, Any]:
    # Stable @id so the brand entity consolidates across pages (products can point
    # their breadcrumb/brand back at this node). addressCountry + contactPoint are
    # the verifiable trust signals we can assert without inventing data.
    brand = brand or BRAND
    data: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "@id": f"{site_url}/#organization",
        "name": brand,
        "url": f"{site_url}/",
        "logo": f"{site_url}{_LOGO}",
        # `or` would resurrect the constant for a description the shop
        # deliberately cleared, so the markup would assert copy nobody wrote.
        "description": ORGANIZATION_DESCRIPTION if description is None else description,
        "address": {"@type": "PostalAddress", "addressCountry": "AR"},
        "contactPoint": {
            "@type": "ContactPoint",
            "contactType": "customer service",
            "url": instagram or INSTAGRAM,
            "availableLanguage": ["es"],
        },
        "sameAs": [instagram or INSTAGRAM],
    }
    names = parse_alternate_names(alternate_names, brand)
    if names:
        data["alternateName"] = names
    return data


def website_jsonld(
    site_url: str, brand: str | None = None, alternate_names: str | None = None
) -> dict[str, Any]:
    brand = brand or BRAND
    data: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": brand,
        "url": f"{site_url}/",
        "inLanguage": "es-AR",
        "publisher": {"@id": f"{site_url}/#organization"},
    }
    names = parse_alternate_names(alternate_names, brand)
    if names:
        data["alternateName"] = names
    return data


def absolute_url(url: str | None, site_url: str) -> str | None:
    """Absolutize a media URL. Admin media is root-relative (/media/...), but the
    Tienda Nube mirror serves absolute CDN URLs — prefixing those would produce an
    invalid double-scheme URL, so only root-relative paths get the site origin."""
    if not url:
        return None
    if url.startswith("//"):
        # Protocol-relative (some CDNs): pin https rather than emitting a URL that
        # og:image consumers may resolve against the wrong scheme.
        return f"https:{url}"
    if url.startswith(("http://", "https://")):
        return url
    return f"{site_url}{url}"


def _cover_image(product: Product, site_url: str) -> str | None:
    cover = product.cover
    if cover is None:
        return None
    if cover.is_image and cover.default_image_url:
        return absolute_url(cover.default_image_url, site_url)
    if cover.is_video:
        return absolute_url(cover.poster_url, site_url)
    return None


def product_jsonld(
    product: Product,
    site_url: str,
    brand: str | None = None,
    description_fallback: str | None = None,
    category_label: str | None = None,
) -> dict[str, Any]:
    url = f"{site_url}/producto/{product.id}"
    brand = brand or BRAND
    data: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": product.title,
        # Stable per-product identifier (no DB column needed; derived from the id).
        "sku": f"GLUCK-{product.id:04d}",
        "url": url,
        "brand": {"@type": "Brand", "name": brand},
        "description": product.description
        or description_fallback
        or f"{product.title} — {brand}, cartera de cuero vegano hecha a mano.",
    }
    image = _cover_image(product, site_url)
    if image:
        data["image"] = [image]
    if product.category:
        # The visible chip/breadcrumb may show an edited label; keep the markup
        # saying exactly what the page says.
        data["category"] = category_label or product.category
    # Only emit an Offer when there is a real price — never fabricate one. The TN
    # mirror tracks real stock and the cart refuses out-of-stock adds — keep the
    # markup honest instead of promising InStock unconditionally.
    if product.price is not None:
        data["offers"] = {
            "@type": "Offer",
            "price": str(product.price),
            "priceCurrency": product.currency,
            "availability": (
                "https://schema.org/InStock"
                if product.in_stock
                else "https://schema.org/OutOfStock"
            ),
            "url": url,
        }
    return data


def breadcrumb_jsonld(
    product: Product,
    site_url: str,
    home_label: str | None = None,
    category_label: str | None = None,
) -> dict[str, Any]:
    """Inicio › <categoría> › <producto>. The category node is included only when
    the product has a category (it links to the real /categoria/<slug> page).

    `home_label`/`category_label` carry the (editable) labels the page actually
    renders, so the markup can never disagree with the visible breadcrumb."""
    items: list[dict[str, Any]] = [
        {"name": home_label or HOME_BREADCRUMB, "url": f"{site_url}/"}
    ]
    if product.category:
        items.append(
            {
                "name": category_label or product.category,
                "url": f"{site_url}/categoria/{slugify(product.category)}",
            }
        )
    items.append({"name": product.title, "url": f"{site_url}/producto/{product.id}"})
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": it["name"], "item": it["url"]}
            for i, it in enumerate(items)
        ],
    }


def category_breadcrumb_jsonld(
    category: str,
    site_url: str,
    home_label: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Inicio › <categoría>. Mirrors the visible breadcrumb on the category page so
    the markup matches the on-page navigation (no markup-vs-content mismatch)."""
    items = [
        {"name": home_label or HOME_BREADCRUMB, "url": f"{site_url}/"},
        {"name": label or category, "url": f"{site_url}/categoria/{slugify(category)}"},
    ]
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": it["name"], "item": it["url"]}
            for i, it in enumerate(items)
        ],
    }
