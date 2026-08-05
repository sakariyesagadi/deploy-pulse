"""GitHub Actions API polling.

Fetches workflow runs for each configured repo/workflow over the last 30 days,
normalises them, and persists them via :class:`~src.store.Store`. Conditional
requests (ETag) are used where practical to stay friendly with rate limits.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import httpx

from .config import Config, RepoConfig, load_config
from .store import Store

logger = logging.getLogger("deploy_pulse.collector")

GITHUB_API = "https://api.github.com"
DEFAULT_LOOKBACK_DAYS = 30
PER_PAGE = 100
MAX_PAGES = 5  # 100 * 5 == 500 runs per workflow ceiling


def _headers(token: str | None, etag: str | None = None) -> dict[str, str]:
    """Build request headers, including auth and a conditional ETag if given."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "deploy-pulse",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if etag:
        headers["If-None-Match"] = etag
    return headers


def _duration_seconds(run: dict[str, Any]) -> float | None:
    """Compute run duration in seconds from created/updated timestamps.

    ``run_started_at`` is preferred over ``created_at`` when present (it excludes
    queue time), falling back gracefully otherwise.
    """
    start_raw = run.get("run_started_at") or run.get("created_at")
    end_raw = run.get("updated_at")
    if not start_raw or not end_raw:
        return None
    try:
        start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = (end - start).total_seconds()
    return delta if delta >= 0 else None


def normalise_run(run: dict[str, Any], repo: str, workflow: str) -> dict[str, Any]:
    """Convert a raw GitHub run object into a Store upsert payload."""
    return {
        "id": int(run["id"]),
        "repo": repo,
        "workflow": workflow,
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "head_branch": run.get("head_branch"),
        "event": run.get("event"),
        "duration_s": _duration_seconds(run),
        "created_at": run["created_at"],
        "updated_at": run.get("updated_at"),
        "run_number": run.get("run_number"),
        "html_url": run.get("html_url"),
    }


def parse_runs_response(
    payload: dict[str, Any], repo: str, workflow: str
) -> list[dict[str, Any]]:
    """Parse a ``/actions/workflows/{id}/runs`` response into upsert payloads."""
    runs = payload.get("workflow_runs", [])
    return [normalise_run(run, repo, workflow) for run in runs]


class Collector:
    """Polls the GitHub Actions API and writes runs into the store."""

    def __init__(
        self,
        store: Store,
        token: str | None = None,
        client: httpx.Client | None = None,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    ) -> None:
        """Create a collector.

        Args:
            store: Storage backend.
            token: GitHub token. Falls back to ``$GITHUB_TOKEN``.
            client: Optional pre-built httpx client (useful for testing).
            lookback_days: How far back to collect runs.
        """
        self.store = store
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self._client = client or httpx.Client(base_url=GITHUB_API, timeout=30.0)
        self._owns_client = client is None
        self.lookback_days = lookback_days

    def close(self) -> None:
        """Close the owned HTTP client, if we created it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "Collector":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _cutoff(self) -> datetime:
        """Return the earliest ``created_at`` we care about."""
        return datetime.now(timezone.utc) - timedelta(days=self.lookback_days)

    def collect_workflow(self, repo: RepoConfig, workflow: str) -> int:
        """Collect runs for a single workflow file. Returns rows stored.

        Uses the ``created`` filter to bound the query server-side and stops
        paginating once runs fall outside the lookback window. A conditional
        request (ETag) on the first page short-circuits when nothing changed.
        """
        resource = f"{repo.full_name}:{workflow}"
        cutoff = self._cutoff()
        created_filter = f">={cutoff.date().isoformat()}"
        path = f"/repos/{repo.full_name}/actions/workflows/{workflow}/runs"

        stored = 0
        for page in range(1, MAX_PAGES + 1):
            etag = self.store.get_etag(resource) if page == 1 else None
            params = {
                "per_page": PER_PAGE,
                "page": page,
                "created": created_filter,
            }
            try:
                response = self._client.get(
                    path, params=params, headers=_headers(self.token, etag)
                )
            except httpx.HTTPError as exc:  # network-level failure
                logger.warning("Request failed for %s (page %s): %s", resource, page, exc)
                break

            if response.status_code == 304:
                logger.info("No changes for %s (ETag match)", resource)
                break
            if response.status_code == 404:
                logger.warning("Workflow not found: %s", resource)
                break
            if response.status_code == 403 and _is_rate_limited(response):
                logger.warning("Rate limited on %s; stopping early", resource)
                break
            if response.status_code >= 400:
                logger.warning(
                    "Unexpected %s for %s: %s",
                    response.status_code, resource, response.text[:200],
                )
                break

            if page == 1 and (new_etag := response.headers.get("ETag")):
                self.store.set_etag(resource, new_etag)

            payload = response.json()
            rows = parse_runs_response(payload, repo.full_name, workflow)
            if not rows:
                break

            # Only keep runs inside the window; stop once we page past it.
            in_window = [
                row for row in rows
                if datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")) >= cutoff
            ]
            stored += self.store.upsert_runs(in_window)

            if len(in_window) < len(rows) or len(rows) < PER_PAGE:
                break  # reached the far edge of the window / last page

        logger.info("Stored %s runs for %s", stored, resource)
        return stored

    def collect_repo(self, repo: RepoConfig) -> int:
        """Collect all configured workflows for a repo. Returns rows stored."""
        total = 0
        for workflow in repo.workflows:
            total += self.collect_workflow(repo, workflow)
        return total

    def collect_all(self, repos: Iterable[RepoConfig]) -> int:
        """Collect every workflow across every repo. Returns rows stored."""
        total = 0
        for repo in repos:
            total += self.collect_repo(repo)
        return total


def _is_rate_limited(response: httpx.Response) -> bool:
    """Detect a primary rate-limit 403 via the remaining-requests header."""
    remaining = response.headers.get("X-RateLimit-Remaining")
    return remaining == "0"


def main() -> int:
    """CLI entry point: load config, collect all repos, report totals."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config: Config = load_config()
    db_path = os.environ.get("DEPLOY_PULSE_DB", "deploy_pulse.db")
    with Store(db_path) as store, Collector(store) as collector:
        total = collector.collect_all(config.repos)
    logger.info("Collection complete: %s runs stored", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
