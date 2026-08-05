"""SQLite storage layer for deploy-pulse.

Persists raw workflow-run data plus ETag cache entries used for conditional
GitHub API requests. All timestamps are stored as ISO-8601 UTC strings.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = "deploy_pulse.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_runs (
    id            INTEGER PRIMARY KEY,          -- GitHub run id (globally unique)
    repo          TEXT NOT NULL,                -- "owner/name"
    workflow      TEXT NOT NULL,                -- workflow file name or display name
    status        TEXT,                         -- queued | in_progress | completed
    conclusion    TEXT,                         -- success | failure | cancelled | ...
    head_branch   TEXT,
    event         TEXT,
    duration_s    REAL,                         -- run duration in seconds
    created_at    TEXT NOT NULL,                -- ISO-8601 UTC
    updated_at    TEXT,                         -- ISO-8601 UTC
    run_number    INTEGER,
    html_url      TEXT,
    collected_at  TEXT NOT NULL                 -- when we stored the row
);

CREATE INDEX IF NOT EXISTS idx_runs_repo_workflow
    ON workflow_runs (repo, workflow, created_at);

CREATE TABLE IF NOT EXISTS etags (
    resource   TEXT PRIMARY KEY,               -- e.g. "owner/name:ci.yml"
    etag       TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _utcnow_iso() -> str:
    """Return the current time as an ISO-8601 UTC string."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WorkflowRun:
    """A single normalised workflow run row."""

    id: int
    repo: str
    workflow: str
    status: str | None
    conclusion: str | None
    head_branch: str | None
    event: str | None
    duration_s: float | None
    created_at: datetime
    updated_at: datetime | None
    run_number: int | None
    html_url: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WorkflowRun":
        """Build a :class:`WorkflowRun` from a database row."""
        return cls(
            id=row["id"],
            repo=row["repo"],
            workflow=row["workflow"],
            status=row["status"],
            conclusion=row["conclusion"],
            head_branch=row["head_branch"],
            event=row["event"],
            duration_s=row["duration_s"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]) if row["updated_at"] else None,
            run_number=row["run_number"],
            html_url=row["html_url"],
        )


def _parse_dt(value: str) -> datetime:
    """Parse an ISO-8601 string into a timezone-aware UTC datetime."""
    # GitHub returns e.g. "2024-05-01T09:00:00Z"; normalise the trailing Z.
    text = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class Store:
    """Thin wrapper over a SQLite connection with the deploy-pulse schema."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        """Open (or create) the database at ``db_path`` and ensure the schema."""
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Context manager that commits on success and rolls back on error."""
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ------------------------------------------------------------------
    # Workflow runs
    # ------------------------------------------------------------------
    def upsert_run(
        self,
        *,
        id: int,
        repo: str,
        workflow: str,
        status: str | None,
        conclusion: str | None,
        head_branch: str | None,
        event: str | None,
        duration_s: float | None,
        created_at: str,
        updated_at: str | None,
        run_number: int | None,
        html_url: str | None,
    ) -> None:
        """Insert or replace a single workflow run keyed on its GitHub id."""
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO workflow_runs (
                    id, repo, workflow, status, conclusion, head_branch, event,
                    duration_s, created_at, updated_at, run_number, html_url,
                    collected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    conclusion=excluded.conclusion,
                    duration_s=excluded.duration_s,
                    updated_at=excluded.updated_at,
                    collected_at=excluded.collected_at
                """,
                (
                    id, repo, workflow, status, conclusion, head_branch, event,
                    duration_s, created_at, updated_at, run_number, html_url,
                    _utcnow_iso(),
                ),
            )

    def upsert_runs(self, rows: list[dict]) -> int:
        """Upsert many runs. Returns the number of rows processed."""
        count = 0
        for row in rows:
            self.upsert_run(**row)
            count += 1
        return count

    def get_runs(
        self,
        repo: str,
        workflow: str,
        since: datetime | None = None,
    ) -> list[WorkflowRun]:
        """Return runs for a repo/workflow, newest first, optionally since a time."""
        query = (
            "SELECT * FROM workflow_runs WHERE repo = ? AND workflow = ?"
        )
        params: list[object] = [repo, workflow]
        if since is not None:
            query += " AND created_at >= ?"
            params.append(since.astimezone(timezone.utc).isoformat())
        query += " ORDER BY created_at DESC"
        rows = self._conn.execute(query, params).fetchall()
        return [WorkflowRun.from_row(row) for row in rows]

    def distinct_workflows(self) -> list[tuple[str, str]]:
        """Return distinct ``(repo, workflow)`` pairs present in storage."""
        rows = self._conn.execute(
            "SELECT DISTINCT repo, workflow FROM workflow_runs ORDER BY repo, workflow"
        ).fetchall()
        return [(row["repo"], row["workflow"]) for row in rows]

    # ------------------------------------------------------------------
    # ETag cache
    # ------------------------------------------------------------------
    def get_etag(self, resource: str) -> str | None:
        """Return a cached ETag for a resource, if any."""
        row = self._conn.execute(
            "SELECT etag FROM etags WHERE resource = ?", (resource,)
        ).fetchone()
        return row["etag"] if row else None

    def set_etag(self, resource: str, etag: str) -> None:
        """Store/refresh the ETag for a resource."""
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO etags (resource, etag, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(resource) DO UPDATE SET
                    etag=excluded.etag, updated_at=excluded.updated_at
                """,
                (resource, etag, _utcnow_iso()),
            )
