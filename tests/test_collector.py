"""Tests for the GitHub Actions collector — parsing, pagination, rate limits."""

from __future__ import annotations

import httpx
import pytest

from src.collector import (
    Collector,
    _duration_seconds,
    _is_rate_limited,
    normalise_run,
    parse_runs_response,
)
from src.config import RepoConfig
from src.store import Store


def test_normalise_run_extracts_fields(workflow_runs_payload):
    run = workflow_runs_payload["workflow_runs"][0]
    row = normalise_run(run, "sakariyesagadi/football-organiser", "ci.yml")
    assert row["id"] == 9001
    assert row["repo"] == "sakariyesagadi/football-organiser"
    assert row["workflow"] == "ci.yml"
    assert row["conclusion"] == "success"
    assert row["head_branch"] == "main"
    assert row["event"] == "push"
    assert row["duration_s"] == pytest.approx(270.0)  # 09:15:10 -> 09:19:40


def test_duration_prefers_run_started_at():
    run = {
        "created_at": "2024-05-06T09:00:00Z",
        "run_started_at": "2024-05-06T09:15:00Z",
        "updated_at": "2024-05-06T09:20:00Z",
    }
    # 09:15 -> 09:20 == 300s (queue time excluded)
    assert _duration_seconds(run) == pytest.approx(300.0)


def test_duration_handles_missing_timestamps():
    assert _duration_seconds({"created_at": "2024-05-06T09:00:00Z"}) is None
    assert _duration_seconds({}) is None


def test_parse_runs_response_counts(workflow_runs_payload):
    rows = parse_runs_response(workflow_runs_payload, "acme/app", "ci.yml")
    assert len(rows) == 6
    assert all(r["repo"] == "acme/app" for r in rows)


def test_is_rate_limited():
    resp = httpx.Response(403, headers={"X-RateLimit-Remaining": "0"})
    assert _is_rate_limited(resp) is True
    resp2 = httpx.Response(403, headers={"X-RateLimit-Remaining": "42"})
    assert _is_rate_limited(resp2) is False


def _mock_transport(payload, *, etag="\"abc123\"", status=200):
    """Build an httpx MockTransport that serves one page then an empty page."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # If client sent a conditional request that matches, return 304.
        if request.headers.get("If-None-Match") == etag and calls["n"] > 1:
            return httpx.Response(304)
        if calls["n"] == 1:
            return httpx.Response(status, json=payload, headers={"ETag": etag})
        return httpx.Response(200, json={"workflow_runs": []}, headers={"ETag": etag})

    return httpx.MockTransport(handler), calls


def test_collect_workflow_stores_runs(tmp_path, workflow_runs_payload):
    # Make all fixture runs "recent" by moving cutoff far back via lookback.
    transport, calls = _mock_transport(workflow_runs_payload)
    client = httpx.Client(base_url="https://api.github.com", transport=transport)
    store = Store(tmp_path / "c.db")
    collector = Collector(store, token="t", client=client, lookback_days=100000)
    repo = RepoConfig(owner="sakariyesagadi", name="football-organiser", workflows=["ci.yml"])

    stored = collector.collect_workflow(repo, "ci.yml")
    # 6 runs in the fixture, all within the window
    assert stored == 6
    # ETag was captured
    assert store.get_etag("sakariyesagadi/football-organiser:ci.yml") == '"abc123"'

    runs = store.get_runs("sakariyesagadi/football-organiser", "ci.yml")
    assert len(runs) == 6
    store.close()


def test_collect_workflow_handles_304(tmp_path, workflow_runs_payload):
    etag = '"cached"'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("If-None-Match") == etag:
            return httpx.Response(304)
        return httpx.Response(200, json=workflow_runs_payload, headers={"ETag": etag})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(base_url="https://api.github.com", transport=transport)
    store = Store(tmp_path / "c.db")
    # Pre-seed the ETag so the first request is conditional and short-circuits.
    store.set_etag("acme/app:ci.yml", etag)
    collector = Collector(store, token="t", client=client, lookback_days=100000)
    repo = RepoConfig(owner="acme", name="app", workflows=["ci.yml"])

    stored = collector.collect_workflow(repo, "ci.yml")
    assert stored == 0  # nothing changed
    store.close()


def test_collect_workflow_handles_404(tmp_path):
    transport = httpx.MockTransport(lambda req: httpx.Response(404, json={"message": "Not Found"}))
    client = httpx.Client(base_url="https://api.github.com", transport=transport)
    store = Store(tmp_path / "c.db")
    collector = Collector(store, token="t", client=client)
    repo = RepoConfig(owner="acme", name="app", workflows=["missing.yml"])

    assert collector.collect_workflow(repo, "missing.yml") == 0
    store.close()


def test_collect_workflow_stops_on_rate_limit(tmp_path):
    transport = httpx.MockTransport(
        lambda req: httpx.Response(403, headers={"X-RateLimit-Remaining": "0"}, json={})
    )
    client = httpx.Client(base_url="https://api.github.com", transport=transport)
    store = Store(tmp_path / "c.db")
    collector = Collector(store, token="t", client=client)
    repo = RepoConfig(owner="acme", name="app", workflows=["ci.yml"])

    assert collector.collect_workflow(repo, "ci.yml") == 0
    store.close()
