"""Tests for the CDN tag purging (app.services.cdn_cache).

The page cache holds a page for a day, so a product or a text that changes is only
visible because this fires. Two properties matter: it must actually call Vercel with the
right tags, and it must never turn a failed purge into a failed webhook or a lost edit —
a stale page is recoverable, a dropped write is not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from app.services import cdn_cache

if TYPE_CHECKING:
    from flask import Flask


class _FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.ok = 200 <= status_code < 300


class _Recorder:
    """Stands in for requests.post."""

    def __init__(self, response: Any = None, raises: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response or _FakeResponse()
        self._raises = raises

    def __call__(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"url": url, **kwargs})
        if self._raises is not None:
            raise self._raises
        return self._response


@pytest.fixture
def configured(app: "Flask") -> "Flask":
    app.config["VERCEL_PURGE_TOKEN"] = "test-token"
    app.config["VERCEL_PROJECT_ID"] = "prj_test"
    app.config["VERCEL_TEAM_ID"] = "team_test"
    return app


def test_purge_is_a_noop_without_a_token(app: "Flask", monkeypatch: Any) -> None:
    """An unconfigured deploy still works; it just does not purge."""
    recorder = _Recorder()
    monkeypatch.setattr(cdn_cache.requests, "post", recorder)
    app.config["VERCEL_PURGE_TOKEN"] = ""
    app.config["VERCEL_PROJECT_ID"] = ""
    monkeypatch.delenv("VERCEL_PURGE_TOKEN", raising=False)
    monkeypatch.delenv("VERCEL_PROJECT_ID", raising=False)

    with app.app_context():
        assert cdn_cache.purge(["catalog"]) is False
    assert recorder.calls == []


def test_purge_calls_vercel_with_the_tags(configured: "Flask", monkeypatch: Any) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(cdn_cache.requests, "post", recorder)

    with configured.app_context():
        assert cdn_cache.purge(["catalog", "product-7"]) is True

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["url"].endswith("/v1/edge-cache/invalidate-by-tags")
    assert call["params"]["projectIdOrName"] == "prj_test"
    assert call["params"]["teamId"] == "team_test"
    assert call["json"] == {"tags": ["catalog", "product-7"], "target": "production"}
    assert call["headers"]["Authorization"] == "Bearer test-token"


def test_purge_batches_at_the_api_limit(configured: "Flask", monkeypatch: Any) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(cdn_cache.requests, "post", recorder)
    tags = [f"product-{n}" for n in range(20)]

    with configured.app_context():
        cdn_cache.purge(tags)

    assert [len(call["json"]["tags"]) for call in recorder.calls] == [16, 4]


def test_purge_deduplicates_tags(configured: "Flask", monkeypatch: Any) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(cdn_cache.requests, "post", recorder)

    with configured.app_context():
        cdn_cache.purge(["catalog", "catalog", "", "site"])

    assert recorder.calls[0]["json"]["tags"] == ["catalog", "site"]


def test_a_network_failure_does_not_propagate(configured: "Flask", monkeypatch: Any) -> None:
    monkeypatch.setattr(
        cdn_cache.requests, "post", _Recorder(raises=RuntimeError("connection reset"))
    )
    with configured.app_context():
        assert cdn_cache.purge(["catalog"]) is False


def test_a_rejected_purge_does_not_propagate(configured: "Flask", monkeypatch: Any) -> None:
    monkeypatch.setattr(
        cdn_cache.requests, "post", _Recorder(response=_FakeResponse(403))
    )
    with configured.app_context():
        assert cdn_cache.purge(["catalog"]) is False


def test_purge_catalog_includes_the_product_tag(configured: "Flask", monkeypatch: Any) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(cdn_cache.requests, "post", recorder)

    with configured.app_context():
        cdn_cache.purge_catalog(99)

    assert recorder.calls[0]["json"]["tags"] == ["catalog", "product-99"]
