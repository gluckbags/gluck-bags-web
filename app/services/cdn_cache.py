"""Purging the CDN cache when the content behind a page changes.

`app/services/page_cache.py` caches the public pages for a day. That TTL is only safe
because of this module: whenever a product or an editable text changes, the tags of the
pages that render it are invalidated and the next visit regenerates them. The TTL is the
safety net for a purge that failed, not the freshness mechanism.

Invalidating marks the entries stale — the next visitor is served the old page instantly
while Vercel revalidates in the background — so a purge never exposes anyone to a slow
page, and a stampede cannot hit Postgres.

Nothing here is allowed to raise: it runs right after a webhook's commit and inside the
admin's save path, and a CDN that did not get the message is a stale page, not a failed
sale or a lost edit.
"""

from __future__ import annotations

import os
from typing import Iterable, Optional, Sequence

import requests
from flask import current_app

_ENDPOINT = "https://api.vercel.com/v1/edge-cache/invalidate-by-tags"
# The API takes at most 16 tags per call.
_MAX_TAGS_PER_CALL = 16
_TIMEOUT_SECONDS = 3


def _setting(name: str) -> str:
    """Config first, environment second, so tests can set it on the app."""
    try:
        value = current_app.config.get(name)
    except RuntimeError:  # outside an app context (CLI, background thread)
        value = None
    return str(value or os.environ.get(name, "")).strip()


def _log_warning(message: str, *args: object) -> None:
    try:
        current_app.logger.warning(message, *args)
    except RuntimeError:
        pass


def _batches(tags: Sequence[str]) -> Iterable[Sequence[str]]:
    for start in range(0, len(tags), _MAX_TAGS_PER_CALL):
        yield tags[start : start + _MAX_TAGS_PER_CALL]


def purge(tags: Sequence[str]) -> bool:
    """Invalidate these cache tags on Vercel's CDN. True when all calls succeeded.

    Without a token this is a no-op: dev, tests and any deploy that has not been wired
    up keep working, they just do not purge anything.
    """
    unique = list(dict.fromkeys(tag for tag in tags if tag))
    if not unique:
        return False

    token = _setting("VERCEL_PURGE_TOKEN")
    project = _setting("VERCEL_PROJECT_ID")
    if not token or not project:
        return False

    params = {"projectIdOrName": project}
    team = _setting("VERCEL_TEAM_ID")
    if team:
        params["teamId"] = team
    target = _setting("VERCEL_PURGE_TARGET") or "production"

    ok = True
    for batch in _batches(unique):
        try:
            response = requests.post(
                _ENDPOINT,
                params=params,
                json={"tags": list(batch), "target": target},
                headers={"Authorization": f"Bearer {token}"},
                timeout=_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — a failed purge must not fail the write
            _log_warning("CDN purge failed for %s: %s", batch, exc)
            ok = False
            continue
        if not response.ok:
            _log_warning("CDN purge rejected for %s: HTTP %s", batch, response.status_code)
            ok = False
    return ok


def purge_catalog(tn_id: Optional[int] = None) -> bool:
    """Invalidate the pages that list products, plus one product's own page."""
    from app.services.page_cache import TAG_CATALOG, TAG_PRODUCT

    tags = [TAG_CATALOG]
    if tn_id is not None:
        tags.append(TAG_PRODUCT.format(tn_id=tn_id))
    return purge(tags)


def purge_content() -> bool:
    """Invalidate every page, since the editable copy is rendered on all of them."""
    from app.services.page_cache import TAG_CONTENT

    return purge([TAG_CONTENT])
