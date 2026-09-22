"""Editable site copy — backed by flask-sitecopy.

Migrated from the in-house editor to the packaged **flask-sitecopy** engine (same
design, now maintained as a versioned dependency). `app/content/registry.py` stays as
the pure catalogue of strings; this module adapts it to a sitecopy `Registry`, wires
the extension, and keeps the app-specific helpers (per-category labels) as thin
wrappers over sitecopy's resolver.

Public surface is unchanged, so callers keep doing:
    from app.content import t, t_lines, register_content, category_label
    from app.content import registry
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

# The engine, re-exported so the rest of the app keeps importing from app.content.
from sitecopy import (
    FileStore as _ScFileStore,
    Group as _ScGroup,
    Registry as _ScRegistry,
    Section as _ScSection,
    SiteCopy,
    TextField as _ScField,
    field_state,
    group_states,
    has_stray_brace,
    is_preview,
    override_count,
    pending_draft_count,
    sanitize,
    strip_tags,
    t,
    t_lines,
    t_plain,
    unknown_tokens,
    visible_text,
)
from sitecopy import editable as _editable

from app.content import registry
from app.utils import slugify

if TYPE_CHECKING:
    from flask import Flask

__all__ = [
    "REGISTRY",
    "brand",
    "category_intro",
    "category_intro_editable",
    "category_label",
    "category_label_editable",
    "category_tagline",
    "category_tagline_editable",
    "ensure_content_schema",
    "field_state",
    "group_states",
    "has_stray_brace",
    "instagram_url",
    "is_preview",
    "override_count",
    "pending_draft_count",
    "register_content",
    "registry",
    "sanitize",
    "strip_tags",
    "t",
    "t_lines",
    "t_plain",
    "tagline",
    "unknown_tokens",
    "visible_text",
    "whatsapp_link",
    "whatsapp_number",
]


# --- registry adapter: main's data -> a sitecopy Registry --------------------
# The dataclasses share a shape (key/label/default/type/hint/max_length for a field;
# key/title/note/fields for a section; key/title/description/preview_path for a group),
# so this is a mechanical re-expression that reuses the exact copy in registry.py — no
# retyping, no drift. Tokens map straight across: TOKEN_FIELDS -> sitecopy `tokens`,
# FIELD_TOKENS -> sitecopy `field_tokens`.


def build_registry() -> _ScRegistry:
    groups = []
    for group in registry.GROUPS:
        sections = tuple(
            _ScSection(
                key=section.key,
                title=section.title,
                note=section.note,
                fields=tuple(
                    _ScField(
                        field.key,
                        field.label,
                        field.default,
                        type=field.type,
                        hint=field.hint,
                        max_length=field.max_length,
                    )
                    for field in section.fields
                ),
            )
            for section in group.sections
        )
        groups.append(
            _ScGroup(
                key=group.key,
                title=group.title,
                description=group.description,
                preview_path=group.preview_path,
                sections=sections,
            )
        )
    return _ScRegistry(
        groups=tuple(groups),
        tokens=registry.TOKEN_FIELDS,
        field_tokens={key: tuple(vals) for key, vals in registry.FIELD_TOKENS.items()},
    )


REGISTRY = build_registry()

_extension = SiteCopy()


# --- brand tokens for the context processor ----------------------------------
# These reach every template as {{ brand }} / {{ tagline }} / {{ instagram_url }} via
# factory's context processor, and back the {brand}/{tagline}/{instagram_url} tokens.


def brand() -> str:
    return str(t_plain("global.brand"))


def tagline() -> str:
    return str(t_plain("global.tagline"))


def instagram_url() -> str:
    return str(t_plain("global.instagram_url"))


def whatsapp_number() -> str:
    """The number as the shop owner writes it (+54 9 11 …) — for display."""
    return str(t_plain("global.whatsapp_number"))


def whatsapp_link(message: str = "") -> str:
    """A wa.me deep link to the shop's number, with `message` already typed in the chat.

    The number is editable copy, so it arrives however it was written (+, spaces,
    hyphens, the Argentine mobile 9): wa.me wants digits only, so that is all we keep.
    An empty/short number means "no WhatsApp" and returns "", which the templates read
    as "don't render the button" — the alternative is a link to wa.me/ that opens an
    error page.
    """
    digits = re.sub(r"\D", "", whatsapp_number())
    if len(digits) < 8:
        return ""
    text = " ".join(message.split())
    url = f"https://wa.me/{digits}"
    return f"{url}?text={quote(text)}" if text else url


# --- app-specific: per-category editable copy --------------------------------
# The canonical category name is identity (URL slug + the value stored on products),
# so it is not editable; only the *label* shown on screen is. A curated category has
# `category.<slug>.{label,tagline,intro}` fields; anything else falls back to the name.


def _category_key(name: str, suffix: str) -> str | None:
    if not name:
        return None
    key = f"category.{slugify(name)}.{suffix}"
    return key if key in registry.FIELDS else None


def category_label(name: str) -> str:
    """Plain label (no edit markers) — for logic, `t()` params, meta tags, breadcrumbs."""
    key = _category_key(name, "label")
    return str(t_plain(key)) if key else (name or "")


def category_tagline(name: str) -> str:
    key = _category_key(name, "tagline")
    return str(t_plain(key)) if key else ""


def category_intro(name: str) -> str | None:
    key = _category_key(name, "intro")
    if key is None:
        return None
    value = str(t_plain(key))
    return value if value.strip() else None


def category_label_editable(name: str):
    """Rendered label with click-to-edit markers (visual editor). Use in templates."""
    key = _category_key(name, "label")
    return _editable(key) if key else (name or "")


def category_tagline_editable(name: str):
    key = _category_key(name, "tagline")
    return _editable(key) if key else ""


def category_intro_editable(name: str):
    key = _category_key(name, "intro")
    if key is None:
        return None
    value = _editable(key)
    return value if str(value).strip() else None


# --- wiring ------------------------------------------------------------------


def is_overridden(key: str) -> bool:
    """True when the admin published a value for `key` (i.e. it's not the code default).

    Lets a template keep its optimized responsive `<picture>` by default and switch to
    the editable single-URL `<img>` only once the image is actually changed. Safe to
    call outside a request / for an unknown key — degrades to False.
    """
    try:
        return bool(field_state(key).get("is_overridden"))
    except Exception:  # noqa: BLE001 — a rendering helper must never raise
        return False


def _editor_pages() -> list[dict[str, str]]:
    """Pages the visual editor may open in its canvas: the home, the static pages, and
    a live product resolved at request time."""
    pages = [
        {"path": "/", "label": "Inicio"},
        {"path": "/nosotras", "label": "Nosotras"},
        {"path": "/contacto", "label": "Contacto"},
        {"path": "/envios", "label": "Envíos"},
        {"path": "/cambios-y-devoluciones", "label": "Cambios"},
        {"path": "/terminos", "label": "Términos"},
        {"path": "/privacidad", "label": "Privacidad"},
    ]
    try:
        from app.repositories import ProductRepository

        published = ProductRepository.get_published()
        if published:
            pages.append({"path": f"/producto/{published[0].id}", "label": "Producto"})
    except Exception:  # noqa: BLE001 — the picker is a convenience, never a hard dep
        pass
    return pages


_SITECOPY_UPLOAD_PREFIX = "sitecopy-uploads"


class BlobFileStore(_ScFileStore):
    """Editor image uploads, into the same object store the product media uses.

    Content-addressed exactly like sitecopy's LocalFileStore — the name is a hash of the
    bytes — so a re-upload is idempotent (one object, one URL, no duplicate entries in
    the version history) and the client's filename, the classic path-traversal vector,
    never reaches storage.
    """

    def __init__(self, store: Any, prefix: str = _SITECOPY_UPLOAD_PREFIX) -> None:
        self._store = store
        self._prefix = prefix.strip("/")

    def save(self, data: bytes, kind: Any) -> str:
        import hashlib

        rel_path = f"{self._prefix}/{hashlib.sha1(data).hexdigest()[:16]}{kind.ext}"
        self._store.put(rel_path, data, kind.content_type)
        return self._store.url_for(rel_path)


def _build_file_store(app: "Flask") -> Any:
    """Where the editor's uploads land — the app's media store, whichever it is.

    The local store keeps sitecopy's own LocalFileStore: it already content-addresses,
    and its `enabled` probe turns uploads off cleanly on a read-only filesystem instead
    of 500ing on the first one.
    """
    import os

    from sitecopy import LocalFileStore

    from app.services import media_store

    store = app.extensions["media_store"]
    if not media_store.is_local():
        return BlobFileStore(store)

    # Uploads land in the persistent media volume and are served by the existing
    # /media route.
    uploads_dir = os.path.join(app.config["MEDIA_ROOT"], _SITECOPY_UPLOAD_PREFIX)
    try:
        os.makedirs(uploads_dir, exist_ok=True)
    except OSError:
        # LocalFileStore.enabled reports this as "uploads unavailable"; creating the
        # directory eagerly would instead take the whole app down at boot.
        pass
    return LocalFileStore(uploads_dir, f"/media/{_SITECOPY_UPLOAD_PREFIX}")


def register_content(app: "Flask") -> None:
    """Install flask-sitecopy on the app (registers `t`/`t_lines`/`t_plain`, the visual
    editor at /admin/content, and the response rewrite), then add the app-specific
    category globals. Must run AFTER Compress(app) so the editor's HTML rewrite sees an
    uncompressed body. Reuses the admin's shared-password session."""
    from sitecopy.storage import SQLAlchemyStore

    from app.auth import is_logged_in as _admin_is_logged_in
    from app.auth import login_required as _admin_login_required
    from app.content.store import CachingTextStore
    from app.factory import db

    _extension.init_app(
        app,
        registry=REGISTRY,
        db=db,
        # sitecopy reloads the overrides once per request; on Neon's free tier that is
        # a connection per rendered page. The wrapper holds them for a short TTL and
        # drops them on every save.
        store=CachingTextStore(SQLAlchemyStore(db)),
        login_required=_admin_login_required,
        is_logged_in=_admin_is_logged_in,
        site_url=app.config.get("SITE_URL", ""),
        brand=brand,
        pages=_editor_pages,
        text_sizes=True,  # every text field gets a "Tamaño" control in the editor
        files=_build_file_store(app),
    )
    # App-specific globals the templates rely on, layered on sitecopy's resolver.
    app.jinja_env.globals["category_label"] = category_label_editable
    app.jinja_env.globals["category_name"] = category_label
    app.jinja_env.globals["content_preview"] = is_preview
    # The floating WhatsApp button: every page builds its own href from the message
    # its `whatsapp_message` block resolves (the product page passes the model and
    # its URL), so the link has to be built at render time, not once per request.
    app.jinja_env.globals["whatsapp_link"] = whatsapp_link
    # For override-aware images: templates keep the responsive <picture> by default and
    # only switch to the editable single-URL <img> when the admin actually changed it.
    app.jinja_env.globals["is_overridden"] = is_overridden

    # Boot-time DDL only where boot is the right place for it (see AUTO_INIT_DB in
    # app/factory.py). A serverless deploy runs `flask init-db` once instead, which
    # calls ensure_content_schema directly.
    if app.config.get("AUTO_INIT_DB", True):
        ensure_content_schema(app)


def ensure_content_schema(app: "Flask") -> None:
    """Create/repair the `site_texts` table (and the media-versions table).

    The in-house editor used to create it via db.create_all() (its SiteText model);
    with that model gone, sitecopy owns the schema. On an existing prod volume the
    table + data are already there and this is a no-op. Same race guard as
    db.create_all(): two workers booting on a fresh volume can both issue CREATE —
    swallow the loser's error instead of crashing.
    """
    from sqlalchemy.exc import OperationalError

    from app.factory import db

    with app.app_context():
        try:
            _extension.ensure_schema()
        except OperationalError:
            db.session.rollback()
