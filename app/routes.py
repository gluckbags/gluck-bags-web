from typing import Any

from flask import Flask, Response, abort, redirect, render_template, url_for

from app import content
from app.services import catalog
from app.seo import (
    absolute_url,
    breadcrumb_jsonld,
    category_breadcrumb_jsonld,
    dump_jsonld,
    organization_jsonld,
    product_jsonld,
    website_jsonld,
)
from app.utils import slugify

# --- Static site content (the non-product sections of the landing page) -------
#
# The COPY of these sections is editable from the admin (app/content/registry.py);
# what stays here is the structure that copy hangs off: which categories exist,
# which image each card uses, and which URL each editorial page answers on.
#
# A category's `name` is its identity — it is the URL slug AND the value stored on
# every product — so it is deliberately not editable. Its on-screen label comes
# from `content.category_label()`.

CATEGORIES: list[dict[str, str]] = [
    {"name": "Tote", "image": "img/productos/tote-cognac-01"},
    {"name": "Mini Bag", "image": "img/productos/crossbody-rosa"},
    {"name": "Bucket Bag", "image": "img/video-posters/bucket-bag-reveal"},
    {"name": "Clutch", "image": "img/productos/clutch-rosa-sobre"},
]

# Editorial trust pages (E-E-A-T). Each is rendered by the shared page shell from
# its `page.<key>.*` registry entries, and is listed in the sitemap.
STATIC_PAGES: dict[str, str] = {
    "nosotras": "about",
    "contacto": "contact",
    "envios": "shipping",
    "cambios-y-devoluciones": "returns",
    "terminos": "terms",
    "privacidad": "privacy",
}


# Slugs of the curated category cards: permanent URLs with pre-migration ranking
# history, kept indexable and sitemap-listed even while empty (their editorial
# intro carries the page until the TN catalogue fills them).
CURATED_SLUGS: list[str] = [slugify(c["name"]) for c in CATEGORIES]


def _category_cards() -> list[dict[str, str]]:
    """The home grid cards with their editable label/tagline resolved."""
    return [
        {
            "name": card["name"],
            "image": card["image"],
            "label": content.category_label_editable(card["name"]),
            "tagline": content.category_tagline_editable(card["name"]),
        }
        for card in CATEGORIES
    ]


def _category_name_for_slug(slug: str) -> str | None:
    """Map a URL slug back to a category name. Considers both the curated category
    cards and any category actually present on published products."""
    names = [c["name"] for c in CATEGORIES] + catalog.published_categories()
    for name in names:
        if slugify(name) == slug:
            return name
    return None


def register_routes(app: Flask) -> None:
    # Expose slugify to templates so links can build /categoria/<slug> URLs.
    app.add_template_filter(slugify, "slugify")

    # Media URLs may be root-relative (admin uploads) or absolute (TN CDN); this
    # keeps og:image/twitter:image from double-prefixing the absolute ones.
    def _absolute(url: str | None) -> str | None:
        return absolute_url(url, app.config["SITE_URL"])

    app.add_template_filter(_absolute, "absolute")

    @app.route("/")
    def index() -> str:
        site_url = app.config["SITE_URL"]
        brand = content.brand()
        alternate_names = content.t_plain("seo.organization.alternate_names")
        jsonld = dump_jsonld(
            [
                organization_jsonld(
                    site_url,
                    brand=brand,
                    instagram=content.instagram_url(),
                    description=str(content.t("seo.organization.description")),
                    alternate_names=alternate_names,
                ),
                website_jsonld(site_url, brand=brand, alternate_names=alternate_names),
            ]
        )
        # Categories that actually have published products, so the home grid can
        # avoid linking an empty (noindex) category as if it were shoppable. Keyed by
        # slug (not raw name) so an off-casing product category (admin input is
        # free-text, e.g. "tote") still matches the curated card ("Tote").
        published_cats = {slugify(c) for c in catalog.published_categories()}
        context: dict[str, Any] = {
            "categories": _category_cards(),
            "published_cats": published_cats,
            "products": catalog.get_published(),
            "jsonld": jsonld,
        }
        return render_template("index.html", **context)

    @app.route("/producto/<int:product_id>")
    def product_detail(product_id: int) -> Any:
        product = catalog.get_by_id(product_id)
        if product is None or not product.is_published:
            # Pre-migration /producto/<id> URLs (Google-indexed) meant legacy admin
            # ids; under the TN source the same route means TN ids, so those URLs
            # would 404. The legacy table is still the source of truth for what
            # each id was — 301 to its category to transfer the equity. Only under
            # the TN source: legacy ids never collide with 9-digit TN ids there,
            # and under the admin source an unpublished product stays a 404.
            if catalog.is_tiendanube():
                from app.repositories import ProductRepository

                legacy = ProductRepository.get_by_id(product_id)
                if legacy is not None and legacy.category:
                    slug = slugify(legacy.category)
                    if _category_name_for_slug(slug) is not None:
                        return redirect(url_for("category_page", slug=slug), code=301)
            abort(404)
        site_url = app.config["SITE_URL"]
        # The JSON-LD has to say exactly what the page says, so it reads the same
        # editable labels the template renders.
        label = content.category_label(product.category) if product.category else None
        home_label = str(content.t("product.breadcrumb_home"))
        jsonld = dump_jsonld(
            [
                product_jsonld(
                    product,
                    site_url,
                    brand=content.brand(),
                    description_fallback=str(
                        content.t("seo.product.description_fallback", title=product.title)
                    ),
                    category_label=label,
                ),
                breadcrumb_jsonld(
                    product, site_url, home_label=home_label, category_label=label
                ),
            ]
        )
        return render_template(
            "product_detail.html",
            product=product,
            related=catalog.get_related(product),
            nav_categories=catalog.published_categories(),
            jsonld=jsonld,
        )

    @app.route("/categoria/<slug>")
    def category_page(slug: str) -> str:
        name = _category_name_for_slug(slug)
        if name is None:
            abort(404)
        site_url = app.config["SITE_URL"]
        products = catalog.get_published_by_category(name)
        return render_template(
            "category.html",
            category=name,
            category_display=content.category_label(name),
            products=products,
            intro=content.category_intro_editable(name),
            # Sibling categories (non-empty) for the inter-category nav chips.
            nav_categories=catalog.published_categories(),
            jsonld=dump_jsonld(
                category_breadcrumb_jsonld(
                    name,
                    site_url,
                    home_label=str(content.t("product.breadcrumb_home")),
                    label=content.category_label(name),
                )
            ),
            # Curated categories stay indexable even while empty (see CURATED_SLUGS);
            # only ad-hoc empty categories (free-text admin input) leave the index.
            noindex=not products and slug not in CURATED_SLUGS,
        )

    # Register the editorial trust pages from STATIC_PAGES. They all render the same
    # shell; the heading, the meta description and the body come from the registry,
    # so editing one is a save in the admin, not a deploy.
    def _make_static_view(slug: str, page_key: str) -> Any:
        def view() -> str:
            return render_template(
                "pages/_page_base.html",
                page_key=page_key,
                page_title=content.t(f"page.{page_key}.title"),
                # t_plain, not t: it is rendered into an attribute, but it still has
                # to be registered or the editor's panel never lists it.
                page_description=content.t_plain(f"page.{page_key}.meta_description"),
            )

        view.__name__ = f"page_{slug.replace('-', '_')}"
        return view

    for _slug, _page_key in STATIC_PAGES.items():
        _view = _make_static_view(_slug, _page_key)
        app.add_url_rule(f"/{_slug}", endpoint=_view.__name__, view_func=_view)

    @app.route("/media/<path:filename>")
    def media_file(filename: str) -> Response:
        # Only the local store is served from here; every other backend hands out its
        # own CDN URLs, so nothing ever asks this route for them.
        from flask import send_from_directory

        from app.services import media_store

        if not media_store.is_local():
            abort(404)
        # Uploaded media is immutable (new upload = new path), so cache forever.
        resp = send_from_directory(app.config["MEDIA_ROOT"], filename, conditional=True)
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp

    @app.route("/robots.txt")
    def robots() -> Response:
        # Dynamic so the Sitemap line always points at the canonical origin.
        body = render_template("robots.txt", site_url=app.config["SITE_URL"])
        return Response(body, mimetype="text/plain")

    @app.route("/sitemap.xml")
    def sitemap() -> Response:
        site_url = app.config["SITE_URL"]
        # changefreq/priority are hints (Google largely ignores them, but they're
        # cheap and valid); lastmod is the signal it does use — set where we have it.
        urls: list[dict[str, str | None]] = [
            {"loc": f"{site_url}/", "lastmod": None, "changefreq": "weekly", "priority": "1.0"}
        ]
        for slug in STATIC_PAGES:
            urls.append(
                {"loc": f"{site_url}/{slug}", "lastmod": None, "changefreq": "yearly", "priority": "0.3"}
            )
        # Curated categories are always listed (same rule as the noindex exemption
        # in category_page); published_categories() adds ad-hoc ones with products.
        category_slugs = list(CURATED_SLUGS)
        category_slugs += [
            slug
            for slug in (slugify(n) for n in catalog.published_categories())
            if slug not in category_slugs
        ]
        for slug in category_slugs:
            urls.append(
                {
                    "loc": f"{site_url}/categoria/{slug}",
                    "lastmod": None,
                    "changefreq": "weekly",
                    "priority": "0.8",
                }
            )
        for product in catalog.get_published():
            lastmod = product.updated_at.date().isoformat() if product.updated_at else None
            urls.append(
                {
                    "loc": f"{site_url}/producto/{product.id}",
                    "lastmod": lastmod,
                    "changefreq": "weekly",
                    "priority": "0.7",
                }
            )
        body = render_template("sitemap.xml", urls=urls)
        return Response(body, mimetype="application/xml")
