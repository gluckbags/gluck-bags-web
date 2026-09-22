import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request
from flask_compress import Compress
from flask_sqlalchemy import SQLAlchemy
from markupsafe import Markup
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from werkzeug.exceptions import HTTPException

from app.services import media_store

load_dotenv()

# Extension instance defined at module scope so models/repositories can import it
# (`from app.factory import db`) without a circular import.
db = SQLAlchemy()


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection: object, connection_record: object) -> None:
    """WAL + a long busy timeout for SQLite: readers never block the writer, and a
    second writer waits up to 30s instead of failing with 'database is locked'
    (relevant while a video transcode briefly holds the write lock).

    Guarded on the driver rather than on the statements failing: on Postgres a PRAGMA is
    a syntax error that ABORTS the transaction the fresh connection just opened, so
    every later statement on it fails with "current transaction is aborted" — and the
    swallow below would hide the cause.
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
    except Exception:  # noqa: BLE001 — a PRAGMA must never keep the app from connecting
        pass
    finally:
        cursor.close()


def _initialize_schema(data_dir: str, seed_fn: "object") -> None:
    """Create tables + seed exactly once, even when multiple gunicorn workers boot
    against a fresh volume at the same time. A cross-process file lock serializes
    them so they don't race db.create_all() ('table already exists') or double-seed."""
    lock_fh = None
    try:
        import fcntl

        lock_fh = open(os.path.join(data_dir, ".init.lock"), "w")
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
    except (ImportError, OSError):
        lock_fh = None
    try:
        try:
            db.create_all()
        except OperationalError:
            db.session.rollback()  # another worker won the race; tables already exist
        seed_fn()
        _add_missing_columns()
        # Self-heal media that predates the og.jpg/AVIF variants (e.g. products
        # seeded on an older build): regenerate them in place so a deploy needs no
        # manual reprocess on the server. Idempotent + behind this same lock; a
        # backfill hiccup must never block boot.
        try:
            from app.maintenance import backfill_media_variants

            backfill_media_variants()
        except Exception:  # noqa: BLE001
            pass
    finally:
        if lock_fh is not None:
            try:
                import fcntl

                fcntl.flock(lock_fh, fcntl.LOCK_UN)
            except Exception:  # noqa: BLE001
                pass
            lock_fh.close()


def _add_missing_columns() -> None:
    """Add columns this build expects but an older DB file does not have.

    The project has no migration tool: db.create_all() creates missing TABLES but
    never alters an existing one, so a column added to a shipped model would raise
    OperationalError on every read. Idempotent, and a failure here must never block
    boot — the column is additive and nullable.
    """
    from flask import current_app
    from sqlalchemy import text

    if db.engine.dialect.name != "sqlite":
        return  # PRAGMA is SQLite-only; other backends get real migrations

    wanted = {("site_texts", "previous_value"): "TEXT"}
    for (table, column), column_type in wanted.items():
        try:
            rows = db.session.execute(text(f"PRAGMA table_info({table})")).fetchall()
            if not rows or any(row[1] == column for row in rows):
                continue
            db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"))
            db.session.commit()
        except Exception:  # noqa: BLE001 — must never block boot
            db.session.rollback()
            # Silence here is dangerous: the resolver's own guard then swallows the
            # consequence and the site boots green with every override reverted to
            # the code defaults, while writes 500. At least say so.
            current_app.logger.exception(
                "No se pudo agregar la columna %s.%s; los textos editados no se van "
                "a leer y guardar va a fallar", table, column
            )


def _resolve_secret_key(data_dir: str) -> str:
    """Use SECRET_KEY from the env, else a stable key persisted under DATA_DIR.

    Persisting it (instead of a per-process random) keeps admin sessions valid
    across restarts/redeploys without requiring the operator to set anything.

    That fallback needs a writable, persistent DATA_DIR, which a serverless deploy does
    not have. Refuse to boot rather than fall back to a per-process random key: the site
    would come up green and log the admin out on every cold start, which is a far harder
    thing to diagnose than a startup error naming the variable to set.
    """
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    key_path = os.path.join(data_dir, "secret_key")
    try:
        with open(key_path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        pass
    key = os.urandom(32).hex()
    try:
        with open(key_path, "w", encoding="utf-8") as fh:
            fh.write(key)
    except OSError as exc:
        raise RuntimeError(
            f"No hay SECRET_KEY en el entorno y no se puede persistir una en "
            f"{key_path!r} ({exc}). Configurá SECRET_KEY como variable de entorno."
        ) from exc
    return key


# Fonts are deliberately never versioned — see the cache-buster below. Kept in sync
# with scripts/build_static.py, which skips the same extensions in the manifest.
_UNVERSIONED_EXTENSIONS = ("woff2", "woff", "ttf", "otf", "eot")

_STATIC_MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "static_manifest.json")


def _load_static_manifest() -> dict[str, str]:
    """`filename -> content hash`, written by scripts/build_static.py at build time.

    Empty when there is no manifest, which is the normal local-dev state: the
    cache-buster then falls back to the file's mtime.
    """
    try:
        with open(_STATIC_MANIFEST_PATH, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def _normalize_database_url(url: str) -> str:
    """Turn a provider's connection string into one SQLAlchemy accepts.

    Neon (like Heroku) hands out `postgres://…`, a scheme SQLAlchemy 2 dropped, and a
    bare `postgresql://` resolves to psycopg2, which is not installed — psycopg 3 has to
    be named. Doing it here means DATABASE_URL can be pasted from the dashboard as-is,
    which is exactly how it will be set.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def _configure_database(app: Flask, data_dir: str) -> None:
    """Point SQLAlchemy at DATABASE_URL when set, else the SQLite file under DATA_DIR.

    SQLite stays the default so local dev, the test suite and the Docker deploy are
    unchanged; a serverless deploy has no persistent disk and sets DATABASE_URL.
    """
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(data_dir, 'gluck.db')}"
        # Wait (don't fail) if the DB is briefly write-locked by another worker.
        # `timeout` is a sqlite3 connect arg and is rejected by every other driver.
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"connect_args": {"timeout": 30}}
        return

    app.config["SQLALCHEMY_DATABASE_URI"] = _normalize_database_url(database_url)
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        # A pooled connection can be dropped between two invocations (a serverless
        # instance freezes; the provider's pooler recycles). Without pre-ping, the
        # first query after an idle period fails instead of reconnecting.
        "pool_pre_ping": True,
        # Deliberately tiny. Neon bills the time its compute stays up, and it only
        # suspends once nothing is connected — a pool holding an idle connection keeps
        # the meter running between requests. The provider's pooled endpoint does the
        # real pooling, so there is nothing to gain by holding connections here.
        "pool_size": 1,
        "max_overflow": 1,
        "pool_recycle": 60,
    }


def _register_db_cli(app: Flask, data_dir: str) -> None:
    """`flask init-db`: the same bootstrap `create_app` runs when AUTO_INIT_DB is on,
    exposed as an explicit command for deploys that must not do it at boot."""
    import click

    @app.cli.command("init-db")
    def init_db_command() -> None:
        """Create tables, seed the initial products and heal missing media variants."""
        from app.content import ensure_content_schema
        from app.seed import seed_initial_products

        _initialize_schema(data_dir, seed_initial_products)
        # sitecopy owns the site_texts schema and is only initialized once
        # register_content has run, so it cannot ride along inside _initialize_schema.
        ensure_content_schema(app)
        click.echo("Base inicializada: tablas creadas, seed aplicado, media curada.")


def _register_canonical_host(app: Flask) -> None:
    """Enforce one canonical host + HTTPS when FORCE_CANONICAL_HOST is on (prod).

    The site answers on three hosts (apex, www, and the nexttech alias) and over
    plain http; without this every variant is a crawlable duplicate and the canonical
    domain leaks. We 301 any non-canonical host/scheme to SITE_URL (preserving path +
    query) and add HSTS. Gated by a flag so the local/test client (host 'localhost')
    is never redirected."""
    if not app.config["FORCE_CANONICAL_HOST"]:
        return

    canonical = urlsplit(app.config["SITE_URL"])
    canonical_host = canonical.netloc
    canonical_scheme = canonical.scheme or "https"

    @app.before_request
    def _redirect_to_canonical() -> Any:
        # Behind nginx-proxy/Cloudflare the original scheme arrives in this header.
        scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
        if request.host == canonical_host and scheme == canonical_scheme:
            return None
        target = urlunsplit(
            (canonical_scheme, canonical_host, request.path, request.query_string.decode(), "")
        )
        return redirect(target, code=301)

    @app.after_request
    def _hsts(response: Any) -> Any:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
        return response


def _register_url_normalization(app: Flask) -> None:
    """Collapse duplicate slashes and drop a stray trailing slash with a 301, so
    URL variants don't fork into crawlable duplicates or hard 404s.

    `https://host//` -> `/` and `/categoria/tote/` -> `/categoria/tote`. The trailing
    slash is stripped ONLY when the slash-less path actually has a route AND the
    trailing-slash path does not — otherwise we'd fight Flask's own slash redirect
    (e.g. the admin dashboard lives at `/admin/`) and loop. GET/HEAD only."""

    def _has_route(path: str) -> bool:
        adapter = app.url_map.bind(request.host)
        try:
            adapter.match(path, method="GET")
        except HTTPException as exc:  # NotFound -> no route; MethodNotAllowed -> route exists
            return getattr(exc, "code", None) == 405
        return True

    @app.before_request
    def _normalize_url() -> Any:
        if request.method not in ("GET", "HEAD"):
            return None
        path = request.path
        target = re.sub(r"/{2,}", "/", path)  # collapse // -> /
        if len(target) > 1 and target.endswith("/"):
            stripped = target.rstrip("/")
            if _has_route(stripped) and not _has_route(target):
                target = stripped
        if target == path:
            return None
        qs = request.query_string.decode()
        return redirect(target + (f"?{qs}" if qs else ""), code=301)


def create_app() -> Flask:
    app = Flask(__name__)

    # Gzip/Brotli text responses (HTML/CSS/JS) at the app level, so compression
    # works regardless of the reverse proxy.
    Compress(app)

    # --- Persistent storage (SQLite DB + uploaded media) ----------------------
    # DATA_DIR is a mounted volume in Docker (/data) so it survives rebuilds;
    # locally it defaults to ./instance (gitignored).
    data_dir = os.path.abspath(os.environ.get("DATA_DIR", "instance"))
    media_root = os.path.join(data_dir, "media")
    try:
        os.makedirs(media_root, exist_ok=True)
    except OSError:
        # A read-only filesystem, which is the normal shape on a serverless host. When
        # the database and the media store are both remote nothing needs this directory,
        # so failing to create it must not take the app down at import time. The two
        # things that DO need it say so themselves: _resolve_secret_key refuses to boot
        # without SECRET_KEY, and LocalFileStore reports uploads as unavailable.
        pass

    app.config["DATA_DIR"] = data_dir
    app.config["MEDIA_ROOT"] = media_root
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    _configure_database(app, data_dir)
    app.config["SECRET_KEY"] = _resolve_secret_key(data_dir)
    app.config["ADMIN_PASSWORD"] = os.environ.get("ADMIN_PASSWORD", "")
    # Cap uploads. Phone clips fit comfortably; the file is compressed server-side
    # after upload. Kept modest so a single request can't fill the container disk.
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

    # Session-cookie hardening. SameSite=Lax is the CSRF defense for the admin: the
    # session cookie is NOT sent on cross-site POSTs, so a forged form from another
    # site can't act on a logged-in admin (all state changes are POST). HttpOnly
    # keeps JS from reading it. Secure is enabled in production (the site is HTTPS
    # behind Cloudflare) via SESSION_COOKIE_SECURE=1; it stays off locally/in tests
    # so the cookie still works over plain http.
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "0") in (
        "1",
        "true",
        "True",
    )

    app.config["UMAMI_WEBSITE_ID"] = os.environ.get("UMAMI_WEBSITE_ID")

    # Where processed media bytes live (see app/services/media_store.py). "local" is
    # the filesystem under MEDIA_ROOT — the code default, so dev, tests and the Docker
    # deploy are unchanged. "blob" is Vercel Blob, for a read-only serverless FS.
    app.config["MEDIA_STORE"] = os.environ.get("MEDIA_STORE", media_store.SOURCE_LOCAL)
    app.extensions["media_store"] = media_store.build_store(app)

    # Creating tables, seeding and healing media is deploy-time work, not per-request
    # work. It is done at boot because the Docker deploy boots once against a volume;
    # on serverless every cold start would re-run it inside the request that triggered
    # it. Set AUTO_INIT_DB=0 there and run `flask init-db` once against the database.
    app.config["AUTO_INIT_DB"] = os.environ.get("AUTO_INIT_DB", "1") not in (
        "0",
        "false",
        "False",
        "",
    )

    # Where the storefront + cart read products from (see app/services/catalog.py).
    # "tiendanube" = the mirrored TN catalogue (production; cart lines carry TN ids
    # and the checkout resolves them to variants). "admin" = the legacy
    # admin-managed Product table — the code default only so local dev and tests
    # work without TN credentials; docker-compose defaults to tiendanube.
    app.config["CATALOG_SOURCE"] = os.environ.get("CATALOG_SOURCE", "admin").strip().lower()

    # Public/canonical origin for absolute OG, Twitter, canonical and sitemap URLs
    # (no trailing slash). Defaults to the real apex domain; overridable per env.
    app.config["SITE_URL"] = os.environ.get("SITE_URL", "https://gluckbags.com").rstrip("/")

    # When on (production), the app enforces a single canonical host: every request
    # on another host (www, the nexttech alias) or over http is 301-redirected to
    # SITE_URL, and responses carry HSTS. Off by default so the local/test client
    # (host "localhost") is never redirected.
    app.config["FORCE_CANONICAL_HOST"] = os.environ.get("FORCE_CANONICAL_HOST", "0") in (
        "1",
        "true",
        "True",
    )
    _register_canonical_host(app)

    db.init_app(app)

    # Cache static assets aggressively (1 year). This is safe because every static
    # URL gets a `?v=<mtime>` cache-buster (see _static_cache_buster below), so a
    # changed file gets a new URL and clients re-fetch it. The reverse proxy /
    # Cloudflare respects this Cache-Control instead of its default browser TTL.
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = timedelta(days=365)

    # Read once at boot, not per URL: it is a small file and this runs for every static
    # URL in every rendered page.
    static_manifest = _load_static_manifest()

    @app.url_defaults
    def _static_cache_buster(endpoint: str, values: dict[str, Any]) -> None:
        if endpoint != "static" or "filename" not in values:
            return
        # Don't version fonts: they're also referenced from CSS `url()` WITHOUT a
        # `?v=`, so adding it here would make the <link rel=preload> URL differ from
        # the @font-face URL — the preload would be wasted and the font downloaded
        # twice (which delays the font and, with font-display:swap, the LCP).
        if values["filename"].rsplit(".", 1)[-1].lower() in _UNVERSIONED_EXTENSIONS:
            return
        # The build manifest first. Where a CDN serves these files they may not be in
        # the deployed bundle at all, so stat'ing them would fail, the `?v=` would
        # quietly disappear, and SEND_FILE_MAX_AGE_DEFAULT (one year) would pin every
        # client to the pre-deploy asset. mtime remains the local-dev path, where it is
        # the better answer anyway: it changes on save, with no build step to remember.
        version = static_manifest.get(values["filename"])
        if version is not None:
            values["v"] = version
            return
        try:
            mtime = os.stat(os.path.join(app.static_folder, values["filename"])).st_mtime
            values["v"] = int(mtime)
        except OSError:
            pass

    @app.context_processor
    def inject_inliners() -> dict[str, Any]:
        def inline_css(filename: str) -> Markup:
            """Inline a static CSS file as a <style> tag (no render-blocking request).
            Relative url("../…") refs are rewritten to absolute /static/ so they
            still resolve once the CSS lives in the HTML document.

            Minified for the wire (this <style> ships in every HTML response and is
            parsed before first paint): /* */ comments are dropped and newline +
            indentation runs collapse to a single space. Intra-line spacing and
            combinators are left untouched, so it's safe — the repo file stays
            readable; only the inlined copy is compacted."""
            with open(os.path.join(app.static_folder, filename), encoding="utf-8") as fh:
                css = fh.read()
            css = css.replace('url("../', 'url("/static/').replace("url('../", "url('/static/")
            css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
            css = re.sub(r"\s*\n\s*", " ", css).strip()
            return Markup(f"<style>{css}</style>")

        return {"inline_css": inline_css}

    @app.context_processor
    def inject_globals() -> dict[str, Any]:
        # Nothing here may touch the session: that makes Flask add `Vary: Cookie`, and
        # Vercel refuses to cache a response whose Vary names Cookie — every visit would
        # go back to hitting Postgres. The header's cart badge is hydrated by cart.js
        # for that reason.
        from app import content

        return {
            "current_year": datetime.now().year,
            # Editable from the admin (app/content/registry.py); these three are
            # referenced all over the templates, so they stay plain names.
            "brand": content.brand(),
            "tagline": content.tagline(),
            "instagram_url": content.instagram_url(),
            # Public/canonical origin for absolute OG, Twitter and canonical URLs.
            "site_url": app.config["SITE_URL"],
            # Bare host (no scheme), for the Umami data-domains scope.
            "site_host": urlsplit(app.config["SITE_URL"]).netloc,
        }

    with app.app_context():
        # Import here so the models are registered with `db` before create_all,
        # and to avoid import cycles at module load time.
        # Importing these registers the Product/Media models with db.metadata
        # (admin/routes/seed all import app.models), so create_all sees them.
        from app.admin import register_admin
        from app.cart import register_cart
        from app.content import register_content
        from app.routes import register_routes
        from app.seed import seed_initial_products

        if app.config["AUTO_INIT_DB"]:
            _initialize_schema(data_dir, seed_initial_products)

        # Before register_content: Flask runs after_request hooks in reverse
        # registration order, so this one runs AFTER sitecopy's and can see the
        # `no-store` it puts on the editor's responses.
        from app.services.page_cache import register_page_cache

        register_page_cache(app)

        # Editable copy first: every template below renders through `t()`. The visual
        # editor at /admin/content is mounted by flask-sitecopy inside register_content.
        register_content(app)
        register_routes(app)
        register_admin(app)
        register_cart(app)
        _register_db_cli(app, data_dir)

        # Tienda Nube wiring (webhook receiver, /tn/callback, `flask sync-tn` + the
        # hourly sync thread). Isolated in a guard so a failure here — a missing
        # dependency, an import error — degrades only the TN integration and never
        # takes down the core storefront (inert under CATALOG_SOURCE=admin). A boot
        # error here 502'd the whole site once; the guard makes that impossible.
        try:
            from app.services.tn_scheduler import register_cli, start_scheduler
            from app.tiendanube import register_tiendanube

            register_tiendanube(app)
            register_cli(app)
            start_scheduler(app)
        except Exception:  # noqa: BLE001 — the storefront must boot regardless of TN
            app.logger.exception(
                "Tienda Nube wiring failed to initialize; continuing without it"
            )

    # After routes exist, so the trailing-slash normalizer can probe the url_map.
    _register_url_normalization(app)

    @app.errorhandler(404)
    def _not_found(error: object) -> tuple[str, int]:
        # Branded, indexable-safe (noindex) 404 in Spanish, with nav back to the site.
        return render_template("404.html"), 404

    @app.errorhandler(413)
    def _too_large(error: object) -> tuple[str, int]:
        return "El archivo es demasiado grande.", 413

    @app.errorhandler(500)
    def _server_error(error: object) -> tuple[str, int]:
        db.session.rollback()
        return "Ocurrió un error al procesar la solicitud. Probá de nuevo.", 500

    return app
