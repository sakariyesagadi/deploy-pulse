"""Tests for the storage layer, dashboard rendering, and notification payloads."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from src.dashboard import build_view_model, render_dashboard
from src.notify import build_digest, build_slack_blocks, render_email_html, send_slack
from src.store import Store
from tests.conftest import days_ago, make_run

NOW = datetime(2024, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


def _seed(store: Store) -> None:
    """Insert a small mix of healthy and failing runs into the store."""
    rows = []
    for i in range(10):
        rows.append(
            {
                "id": i + 1,
                "repo": "acme/app",
                "workflow": "ci.yml",
                "status": "completed",
                "conclusion": "success" if i % 5 else "failure",
                "head_branch": "main",
                "event": "push",
                "duration_s": 60.0 + i,
                "created_at": days_ago(i * 0.5, NOW).isoformat(),
                "updated_at": days_ago(i * 0.5, NOW).isoformat(),
                "run_number": i + 1,
                "html_url": "https://example.com",
            }
        )
    store.upsert_runs(rows)


def test_store_roundtrip(store):
    _seed(store)
    runs = store.get_runs("acme/app", "ci.yml")
    assert len(runs) == 10
    assert store.distinct_workflows() == [("acme/app", "ci.yml")]


def test_store_upsert_is_idempotent(store):
    _seed(store)
    _seed(store)  # upsert again
    assert len(store.get_runs("acme/app", "ci.yml")) == 10


def test_etag_roundtrip(store):
    assert store.get_etag("acme/app:ci.yml") is None
    store.set_etag("acme/app:ci.yml", '"xyz"')
    assert store.get_etag("acme/app:ci.yml") == '"xyz"'


def test_dashboard_renders_html(store):
    _seed(store)
    vm = build_view_model(store, now=NOW)
    assert vm["total"] == 1
    html = render_dashboard(vm)
    assert "deploy-pulse" in html
    assert "ci.yml" in html
    assert "<canvas" in html


def test_dashboard_empty(store):
    vm = build_view_model(store, now=NOW)
    assert vm["total"] == 0
    html = render_dashboard(vm)
    assert "No workflow data yet" in html


def test_digest_and_email(store):
    _seed(store)
    digest = build_digest(store, now=NOW)
    assert digest.counts["GREEN"] + digest.counts["YELLOW"] + digest.counts["RED"] == 1
    html = render_email_html(digest)
    assert "weekly digest" in html
    blocks = build_slack_blocks(digest)
    assert blocks["blocks"][0]["type"] == "header"


def test_send_slack_success(store):
    _seed(store)
    digest = build_digest(store, now=NOW)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="ok"))
    client = httpx.Client(transport=transport)
    assert send_slack("https://hooks.slack.com/test", digest, client) is True


def test_send_slack_failure(store):
    _seed(store)
    digest = build_digest(store, now=NOW)
    transport = httpx.MockTransport(lambda req: httpx.Response(500, text="boom"))
    client = httpx.Client(transport=transport)
    assert send_slack("https://hooks.slack.com/test", digest, client) is False
