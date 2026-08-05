"""Shared pytest fixtures and helpers for deploy-pulse tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.store import Store, WorkflowRun

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def workflow_runs_payload() -> dict:
    """The sample GitHub API workflow_runs.json response as a dict."""
    return json.loads((FIXTURES / "workflow_runs.json").read_text(encoding="utf-8"))


@pytest.fixture
def store(tmp_path) -> Store:
    """A fresh Store backed by a temp-file SQLite database."""
    db = tmp_path / "test.db"
    s = Store(db)
    yield s
    s.close()


def make_run(
    *,
    id: int,
    repo: str = "acme/app",
    workflow: str = "ci.yml",
    conclusion: str | None = "success",
    status: str | None = "completed",
    duration_s: float | None = 60.0,
    created_at: datetime | None = None,
) -> WorkflowRun:
    """Build a WorkflowRun for tests with sensible defaults."""
    created_at = created_at or datetime.now(timezone.utc)
    return WorkflowRun(
        id=id,
        repo=repo,
        workflow=workflow,
        status=status,
        conclusion=conclusion,
        head_branch="main",
        event="push",
        duration_s=duration_s,
        created_at=created_at,
        updated_at=created_at,
        run_number=id,
        html_url=f"https://github.com/{repo}/actions/runs/{id}",
    )


def days_ago(n: float, base: datetime | None = None) -> datetime:
    """Return a UTC datetime ``n`` days before ``base`` (default: now)."""
    base = base or datetime.now(timezone.utc)
    return base - timedelta(days=n)
