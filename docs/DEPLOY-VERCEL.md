# Deploying on Vercel

The app runs as a single Python function. Everything that used to live on the container's
disk lives somewhere else: the database in Postgres, uploaded media in Vercel Blob, the
static assets on the CDN, and the hourly Tienda Nube sync in a GitHub Actions schedule.

The Docker/Jenkins deploy is unaffected — every switch below defaults to the old
behaviour, so the same code runs both ways.

## What changes, and the flag that changes it

| Concern | Docker | Vercel | Flag |
| --- | --- | --- | --- |
| Database | SQLite under `DATA_DIR` | Postgres | `DATABASE_URL` |
| Uploaded media | `DATA_DIR/media`, served by `/media` | Vercel Blob's CDN | `MEDIA_STORE` |
| Schema creation | at boot | `flask init-db`, once | `AUTO_INIT_DB` |
| Static assets | served by Flask | `public/`, served by the CDN | build step |
| TN sync | daemon thread | webhooks, plus a manual GitHub Actions run | `TN_SYNC_ENABLED` |
| Public pages | rendered per request | cached on the CDN, purged by tag | `PAGE_CACHE_TTL` |
| Admin uploads | multipart POST | browser → Blob, direct | follows `MEDIA_STORE` |
| ffmpeg | system package | `imageio-ffmpeg` wheel | `FFMPEG_BINARY` |

## Files

- `wsgi.py` — the entrypoint Vercel looks for (a top-level `app`).
- `vercel.json` — the build command and `maxDuration`.
- `.vercelignore` — what stays out of the upload. This is the control that works:
  `excludeFiles` in `vercel.json` is **not honored** by the zero-config Flask preset —
  verified with `vercel build`, where the bundle came out byte-identical (2749 files)
  with it, without it, and with a single trivial `tests/**` pattern.
- `requirements-dev.txt` — pytest and playwright. They are out of `requirements.txt`
  because that is what the bundle installs, and playwright alone is 109 MB.
- `.python-version` — 3.12, matching local.
- `scripts/build_static.py` — publishes `app/static/` to `public/static/` and writes the
  cache-busting manifest. Runs as the build command.

## One-time setup

0. **Set `VERCEL_SUPPORT_LARGE_FUNCTIONS=1`** as a project environment variable. The
   bundle is ~309 MB — a static ffmpeg build (78 MB), the HEIC/AVIF codecs behind
   Pillow, and the Postgres driver — which is over the standard limit. Without this the
   build fails outright with `exceeds the maximum function size`.
1. **Create the project** and point it at this repository.
2. **Create a Postgres database** and put its pooled connection string in
   `DATABASE_URL`. The string can be pasted exactly as the provider gives it —
   `postgres://` and `postgresql://` are both rewritten to the psycopg driver.
3. **Create a Blob store**, connect it to the project, and confirm
   `BLOB_READ_WRITE_TOKEN` is in the environment.
4. **Set the environment variables** (below), then deploy.
5. **Create the schema**: `vercel env pull && flask init-db`, or run it locally against
   the same `DATABASE_URL`. This is deliberately not automatic — see `AUTO_INIT_DB`.
6. **Migrate the data** from the current SQLite volume:

   ```bash
   python scripts/migrate_sqlite_to_postgres.py \
       --sqlite ./gluck.db --postgres "$DATABASE_URL"
   ```

   `site_texts` is the only table that cannot be rebuilt from elsewhere — it holds the
   copy edited from `/admin/content`. The Tienda Nube mirror repopulates itself on the
   next sync.
7. **Add the sync workflow's secrets** in GitHub: `SYNC_TN_URL` (the deployment's
   `/internal/sync-tn`) and `CRON_SECRET` (the same value as in the deployment).

## Environment variables

The Vercel project's environment variables are the source of truth for the live site.
Infisical is still wired into `Jenkinsfile`/`docker-compose.yml`, which are the ROLLBACK
path to the Pi — a separate store holding the pre-migration values. Nothing syncs between
them, so a rotated secret has to be changed in whichever one you intend to run, and a
rollback will come up with the OLD admin password and session key.

Required:

```
VERCEL_SUPPORT_LARGE_FUNCTIONS=1
DATABASE_URL              the Postgres connection string
BLOB_READ_WRITE_TOKEN     from the Blob store
SECRET_KEY                any long random string — see below
ADMIN_PASSWORD            the admin panel password
MEDIA_STORE=blob
AUTO_INIT_DB=0
TN_SYNC_ENABLED=0
CRON_SECRET               shared with the GitHub Actions workflow
VERCEL_PURGE_TOKEN        a Vercel token with access to this project (CDN purge)
VERCEL_PROJECT_ID         prj_… of this project
VERCEL_TEAM_ID            team_… that owns it
```

Carried over from the current deploy:

```
SITE_URL=https://gluckbags.com
FORCE_CANONICAL_HOST=1
SESSION_COOKIE_SECURE=1
CATALOG_SOURCE=tiendanube
TN_STORE_ID, TN_ACCESS_TOKEN, TN_CLIENT_ID, TN_CLIENT_SECRET
UMAMI_WEBSITE_ID
```

`SECRET_KEY` is not optional here. With a writable disk the app persists a generated key
under `DATA_DIR`; with a read-only one there is nowhere to put it, and a per-process key
would log the admin out on every cold start. The app refuses to boot instead.

## Why each switch exists

**`AUTO_INIT_DB=0`.** Creating tables, seeding and healing media variants happens in the
app factory. That is right for a container that boots once against a volume and wrong
here, where a cold start happens inside a request. `flask init-db` does the same work on
demand — including the `site_texts` table, which flask-sitecopy creates separately.

**`MEDIA_STORE=blob`.** The `/media` route then serves nothing (Blob's CDN owns the
bytes) and `Media.path` resolves to a Blob URL. That URL is derived from the store id
inside the token, so rendering a page makes no API calls.

**`TN_SYNC_ENABLED=0`.** The in-process scheduler needs a process that outlives the
request. `.github/workflows/sync-tiendanube.yml` calls `/internal/sync-tn` instead, on
demand — it has no schedule. The mirror is kept current by the Tienda Nube webhooks;
the workflow is the manual reconciliation for whatever they miss. It used to run
hourly, and that is part of what exhausted Neon's free tier on 2026-09-22: every
connection keeps the compute awake for five more minutes, so 24 runs a day burned
CU-hours for visits that never happened.

**The CDN cache.** `app/services/page_cache.py` marks the public pages cacheable and
`app/services/cdn_cache.py` purges them by tag when a product or a text changes. This is
the main defence of the database: a cached visit never reaches the function. Two things
break it, both worth knowing before touching a public view — reading the session makes
Flask send `Vary: Cookie`, and anything that sets a cookie disqualifies the response.
Vercel refuses to cache either. That is why the header's cart badge is hydrated by
`cart.js` instead of rendered server side.

**`VERCEL_PURGE_TOKEN` / `VERCEL_PROJECT_ID` / `VERCEL_TEAM_ID`.** What the purge needs.
Without them nothing is purged and `PAGE_CACHE_TTL` becomes the only freshness
guarantee, which is why an edit that does not show up within a day points here first.

**Admin uploads.** A function's request body is capped at 4.5 MB — less than one phone
photo. When `MEDIA_STORE` is not local, the admin form asks
`POST /admin/media/upload-token` for a short-lived, single-file token, PUTs the file
straight to Blob, and posts only the pathname. The server reads it back, runs the same
Pillow/ffmpeg pipeline, and deletes the staged original. The token's constraints
(destination path, size, content types) are inside its signed payload, so the browser
holding it cannot widen them.

**ffmpeg.** Resolved once per process: `FFMPEG_BINARY` if set, else a system `ffmpeg`
(what the Docker image installs), else the static build shipped by the `imageio-ffmpeg`
wheel — which is how the encoder reaches a host with no system packages.

## Deploys

Automatic: the project is connected to this GitHub repository, so a push to `main`
deploys to production and any other branch gets a preview. `vercel deploy --prod` from a
checkout still works for an out-of-band deploy.

The Jenkins trigger that deployed to the Pi is gone; the Pi itself is left running as the
rollback. Note that `vercel git connect` fails with a confusing "make sure there aren't
any typos" if the local `origin` is stale — this repository moved from `CROCDC/` to
`gluckbags/`, and the CLI reads the remote rather than following GitHub's redirect.

## Cutover

The DNS zone stays in Cloudflare, so the `tienda.gluckbags.com` redirect and the Single
Redirect rule for the old Tienda Nube storefront are untouched.

**Done on 2026-09-03.** `gluckbags.com` and `www` are A records to `216.198.79.1` and
`64.29.17.1`, DNS-only (grey cloud) in Cloudflare. What the cutover actually taught:

- `site_texts` had 26 rows of copy edited from `/admin/content` — including the "15% OFF"
  and "6 cuotas" promotions. Cutting without migrating it would have silently reverted
  the shop's own words to the code defaults. Migrate it FIRST and diff the rendered text
  of both sites before touching DNS; it is the only table that cannot be rebuilt.
- Expect roughly two minutes of `525` while Vercel issues the certificate. It cannot
  issue one before DNS points at it, so there is no way to pre-warm this.
- `gluck.nexttech.com.ar` is NOT a usable rollback view: the Pi has
  `FORCE_CANONICAL_HOST=1`, so it 301s to gluckbags.com. The real rollback is restoring
  the two Cloudflare records to `CNAME 392770c8-a3e4-4b8e-8899-5b76b552b737.cfargotunnel.com`,
  proxied.
- `tienda.gluckbags.com` is a separate, unproxied hostname pointing at Tienda Nube. The
  apex going grey does not affect it; verified before and after.
- Set `FORCE_CANONICAL_HOST=1` only AFTER the DNS flip. With it on beforehand, the
  `*.vercel.app` URL 301s to a domain that still resolves elsewhere, and the deployment
  becomes impossible to test.

## Testing

The suite runs against SQLite and the local filesystem by default, so nothing here
changes how you run it — except that pytest and playwright now live in
`requirements-dev.txt`:

```bash
pip install -r requirements-dev.txt

# The Blob adapter, against an in-memory stand-in for the Blob API
pytest tests/test_media_store.py tests/test_direct_upload.py tests/test_serverless_boot.py

# The Postgres-specific behaviour, against a real Postgres
docker run -d --name gluck-pg -e POSTGRES_PASSWORD=gluck -e POSTGRES_USER=gluck \
    -e POSTGRES_DB=gluck -p 55432:5432 postgres:16-alpine
TEST_DATABASE_URL=postgresql://gluck:gluck@localhost:55432/gluck \
    pytest tests/test_postgres.py tests/test_migrate_to_postgres.py
```

The Postgres tests skip themselves when `TEST_DATABASE_URL` is unset.
