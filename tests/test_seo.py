"""Unit tests for the JSON-LD builders in app.seo.

Dimension: structured-data. These dicts drive Google rich results, so the shape
matters: absolute URLs on the canonical origin, the brand, and — critically — an
Offer ONLY when a real price exists (fabricating a price for a 'Consultar' item
risks a manual spam action). No app/server needed; these are pure functions over
the Product model, exercised with lightweight stand-ins.
"""

from __future__ import annotations

from app.seo import (
    BRAND,
    breadcrumb_jsonld,
    category_breadcrumb_jsonld,
    organization_jsonld,
    parse_alternate_names,
    product_jsonld,
    website_jsonld,
)

SITE = "https://gluckbags.com"


class _FakeProduct:
    """Minimal stand-in matching the attributes app.seo reads off Product."""

    def __init__(self, id, title, category=None, price=None, currency="ARS", description=None,
                 in_stock=True):
        self.id = id
        self.title = title
        self.category = category
        self.price = price
        self.currency = currency
        self.description = description
        self.in_stock = in_stock
        self.cover = None  # no media -> no image key (kept simple)


def test_organization_and_website_absolute_on_canonical() -> None:
    org = organization_jsonld(SITE)
    assert org["@type"] == "Organization"
    assert org["name"] == BRAND
    assert org["url"] == f"{SITE}/"
    assert org["logo"].startswith(f"{SITE}/")
    assert any("instagram.com" in s for s in org["sameAs"])

    site = website_jsonld(SITE)
    assert site["@type"] == "WebSite"
    assert site["url"] == f"{SITE}/"


def test_organization_has_id_contactpoint_and_country() -> None:
    """The Organization carries a stable @id (for cross-page entity consolidation),
    a Google-recognized contactType, and an AR address — the verifiable trust signals
    we add without inventing personal data."""
    org = organization_jsonld(SITE)
    assert org["@id"] == f"{SITE}/#organization"
    assert org["address"]["@type"] == "PostalAddress"
    assert org["address"]["addressCountry"] == "AR"
    cp = org["contactPoint"]
    assert cp["@type"] == "ContactPoint"
    # Must be a value from Google's documented enum (not "customer support").
    assert cp["contactType"] == "customer service"
    assert "instagram.com" in cp["url"]


def test_alternate_names_are_trimmed_deduped_and_exclude_the_brand() -> None:
    assert parse_alternate_names(" Gluck , ,Gluck Bags, Gluck, GLÜCK", "GLÜCK") == [
        "Gluck",
        "Gluck Bags",
    ]
    # Cleared in the admin: no alternateName at all, not the defaults.
    assert "alternateName" not in organization_jsonld(SITE, alternate_names="")
    assert "alternateName" not in website_jsonld(SITE, alternate_names=" , ")


def test_product_has_stable_sku() -> None:
    """Every product gets a stable synthetic SKU derived from its id (no DB column)."""
    data = product_jsonld(_FakeProduct(7, "Tote Cognac"), SITE)
    assert data["sku"] == "GLUCK-0007"


def test_category_breadcrumb_matches_visible_trail() -> None:
    """category_breadcrumb_jsonld is Inicio › <categoría> with absolute canonical URLs
    (the category node points at the real /categoria/<slug> page)."""
    crumbs = category_breadcrumb_jsonld("Mini Bag", SITE)
    assert crumbs["@type"] == "BreadcrumbList"
    items = crumbs["itemListElement"]
    assert [it["name"] for it in items] == ["Inicio", "Mini Bag"]
    assert [it["position"] for it in items] == [1, 2]
    assert items[0]["item"] == f"{SITE}/"
    assert items[1]["item"] == f"{SITE}/categoria/mini-bag"


def test_product_without_price_has_no_offer() -> None:
    data = product_jsonld(_FakeProduct(1, "Tote Cognac", category="Tote"), SITE)
    assert data["@type"] == "Product"
    assert data["name"] == "Tote Cognac"
    assert data["url"] == f"{SITE}/producto/1"
    assert data["brand"]["name"] == BRAND
    assert data["category"] == "Tote"
    assert "offers" not in data  # never fabricate a price


def test_product_with_price_emits_offer() -> None:
    data = product_jsonld(_FakeProduct(2, "Mini Bag", price=45000), SITE)
    offer = data["offers"]
    assert offer["@type"] == "Offer"
    assert offer["price"] == "45000"
    assert offer["priceCurrency"] == "ARS"
    assert offer["availability"].endswith("InStock")
    assert offer["url"] == f"{SITE}/producto/2"


def test_product_out_of_stock_emits_out_of_stock() -> None:
    data = product_jsonld(_FakeProduct(2, "Mini Bag", price=45000, in_stock=False), SITE)
    assert data["offers"]["availability"].endswith("OutOfStock")


def test_product_description_falls_back_when_missing() -> None:
    data = product_jsonld(_FakeProduct(3, "Clutch Sobre"), SITE)
    assert "Clutch Sobre" in data["description"]
    assert BRAND in data["description"]


def test_breadcrumb_includes_category_node_when_present() -> None:
    crumbs = breadcrumb_jsonld(_FakeProduct(4, "Tote Gris", category="Tote"), SITE)
    items = crumbs["itemListElement"]
    assert [it["name"] for it in items] == ["Inicio", "Tote", "Tote Gris"]
    assert [it["position"] for it in items] == [1, 2, 3]
    # The category node points at the real /categoria/<slug> page.
    assert items[1]["item"] == f"{SITE}/categoria/tote"
    assert items[2]["item"] == f"{SITE}/producto/4"


def test_breadcrumb_omits_category_node_when_absent() -> None:
    crumbs = breadcrumb_jsonld(_FakeProduct(5, "Pieza Suelta"), SITE)
    assert [it["name"] for it in crumbs["itemListElement"]] == ["Inicio", "Pieza Suelta"]
