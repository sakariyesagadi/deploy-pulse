"""Metric calculation for workflow runs.

Turns raw stored runs into a :class:`WorkflowMetrics` summary: success rates,
failure streaks, average durations, duration trend, and flaky-run counts.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .store import Store, WorkflowRun

# GitHub conclusions treated as terminal for success-rate purposes.
_SUCCESS = "success"
_FAILURE_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}
# Runs with these conclusions are ignored for success rate (not a real signal).
_IGNORED_CONCLUSIONS = {"cancelled", "skipped", "stale", "neutral", "action_required", None}


@dataclass
class WorkflowMetrics:
    """Computed health metrics for a single workflow."""

    workflow: str
    repo: str
    success_rate_7d: float
    success_rate_30d: float
    failure_streak: int        # consecutive failures (current)
    avg_duration_7d: float     # seconds
    avg_duration_30d: float
    duration_trend: float      # % change (positive = getting slower)
    flaky_count_7d: int        # pass->fail->pass in same day
    total_runs_7d: int
    last_run_status: str
    last_run_at: datetime


def _now() -> datetime:
    """Return the current UTC time (isolated for testability)."""
    return datetime.now(timezone.utc)


def _is_completed(run: WorkflowRun) -> bool:
    """True if a run has finished (has a conclusion we can score)."""
    return run.conclusion is not None


def _is_success(run: WorkflowRun) -> bool:
    """True if a run concluded successfully."""
    return run.conclusion == _SUCCESS


def _is_failure(run: WorkflowRun) -> bool:
    """True if a run concluded in a failure state."""
    return run.conclusion in _FAILURE_CONCLUSIONS


def success_rate(runs: list[WorkflowRun]) -> float:
    """Fraction of scoreable runs that succeeded.

    Cancelled/skipped/neutral runs are excluded from both numerator and
    denominator. Returns ``1.0`` when there are no scoreable runs (nothing has
    failed, so treat as healthy).
    """
    scoreable = [r for r in runs if r.conclusion not in _IGNORED_CONCLUSIONS]
    if not scoreable:
        return 1.0
    successes = sum(1 for r in scoreable if _is_success(r))
    return successes / len(scoreable)


def failure_streak(runs: list[WorkflowRun]) -> int:
    """Count current consecutive failures from the most recent run backwards.

    Runs must be ordered newest-first. Ignored conclusions (cancelled/skipped)
    are transparent — they neither extend nor break the streak. A success
    breaks the streak.
    """
    streak = 0
    for run in runs:
        if run.conclusion in _IGNORED_CONCLUSIONS:
            continue
        if _is_failure(run):
            streak += 1
        else:
            break
    return streak


def average_duration(runs: list[WorkflowRun]) -> float:
    """Mean duration in seconds over runs that recorded a duration."""
    durations = [r.duration_s for r in runs if r.duration_s is not None]
    if not durations:
        return 0.0
    return sum(durations) / len(durations)


def duration_trend(recent: list[WorkflowRun], previous: list[WorkflowRun]) -> float:
    """Fractional change in average duration between two windows.

    Positive means the recent window is slower. Returns ``0.0`` when the
    previous window has no measurable duration to compare against.
    """
    prev_avg = average_duration(previous)
    if prev_avg <= 0:
        return 0.0
    recent_avg = average_duration(recent)
    return (recent_avg - prev_avg) / prev_avg


def flaky_count(runs: list[WorkflowRun]) -> int:
    """Count flaky episodes: a pass -> fail -> pass sequence within one day.

    Groups completed runs by calendar day (UTC), orders each day oldest-first,
    and counts ``success ... failure ... success`` transitions. Each failure
    that is bracketed by successes on the same day counts as one flaky episode.
    """
    by_day: dict[str, list[WorkflowRun]] = defaultdict(list)
    for run in runs:
        if not _is_completed(run):
            continue
        if run.conclusion in _IGNORED_CONCLUSIONS:
            continue
        day = run.created_at.astimezone(timezone.utc).date().isoformat()
        by_day[day].append(run)

    flaky = 0
    for day_runs in by_day.values():
        ordered = sorted(day_runs, key=lambda r: r.created_at)
        seen_success_before = False
        pending_failure = False
        for run in ordered:
            if _is_success(run):
                if pending_failure and seen_success_before:
                    flaky += 1
                    pending_failure = False
                seen_success_before = True
            elif _is_failure(run):
                if seen_success_before:
                    pending_failure = True
    return flaky


def compute_metrics(
    repo: str,
    workflow: str,
    runs: list[WorkflowRun],
    now: datetime | None = None,
) -> WorkflowMetrics | None:
    """Compute :class:`WorkflowMetrics` from a workflow's runs.

    Args:
        repo: ``owner/name`` slug.
        workflow: Workflow file name / display name.
        runs: Runs for this workflow (any order; sorted internally).
        now: Reference "now" for windowing (defaults to current UTC).

    Returns:
        Metrics, or ``None`` if there are no runs at all.
    """
    if not runs:
        return None
    now = now or _now()
    ordered = sorted(runs, key=lambda r: r.created_at, reverse=True)

    cutoff_7d = now - timedelta(days=7)
    cutoff_14d = now - timedelta(days=14)
    cutoff_30d = now - timedelta(days=30)

    runs_7d = [r for r in ordered if r.created_at >= cutoff_7d]
    runs_prev_7d = [r for r in ordered if cutoff_14d <= r.created_at < cutoff_7d]
    runs_30d = [r for r in ordered if r.created_at >= cutoff_30d]

    last_run = ordered[0]
    last_status = last_run.conclusion or last_run.status or "unknown"

    return WorkflowMetrics(
        workflow=workflow,
        repo=repo,
        success_rate_7d=success_rate(runs_7d),
        success_rate_30d=success_rate(runs_30d),
        failure_streak=failure_streak(ordered),
        avg_duration_7d=average_duration(runs_7d),
        avg_duration_30d=average_duration(runs_30d),
        duration_trend=duration_trend(runs_7d, runs_prev_7d),
        flaky_count_7d=flaky_count(runs_7d),
        total_runs_7d=len(runs_7d),
        last_run_status=last_status,
        last_run_at=last_run.created_at,
    )


def compute_all(store: Store, now: datetime | None = None) -> list[WorkflowMetrics]:
    """Compute metrics for every ``(repo, workflow)`` pair in the store."""
    results: list[WorkflowMetrics] = []
    now = now or _now()
    for repo, workflow in store.distinct_workflows():
        runs = store.get_runs(repo, workflow, since=now - timedelta(days=30))
        metrics = compute_metrics(repo, workflow, runs, now=now)
        if metrics is not None:
            results.append(metrics)
    return results
