"""Trend detection across time windows.

Compares the most recent 7-day window against the previous 7-day window to
surface *changes* worth calling out in a digest — degrading success rate,
slowing pipelines, and newly flaky workflows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from . import metrics as metrics_mod
from .config import ScoringConfig
from .store import Store, WorkflowRun


class TrendSeverity(str, Enum):
    """Severity of a trend alert."""

    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class TrendAlert:
    """A single detected trend for one workflow."""

    repo: str
    workflow: str
    kind: str            # "success_rate" | "duration" | "flaky"
    severity: TrendSeverity
    description: str


def _split_windows(
    runs: list[WorkflowRun], now: datetime
) -> tuple[list[WorkflowRun], list[WorkflowRun]]:
    """Split runs into (recent 7d, previous 7d) windows."""
    cutoff_7d = now - timedelta(days=7)
    cutoff_14d = now - timedelta(days=14)
    recent = [r for r in runs if r.created_at >= cutoff_7d]
    previous = [r for r in runs if cutoff_14d <= r.created_at < cutoff_7d]
    return recent, previous


def detect_for_workflow(
    repo: str,
    workflow: str,
    runs: list[WorkflowRun],
    scoring: ScoringConfig | None = None,
    now: datetime | None = None,
) -> list[TrendAlert]:
    """Detect trend alerts for a single workflow's runs."""
    cfg = scoring or ScoringConfig()
    now = now or datetime.now(timezone.utc)
    recent, previous = _split_windows(runs, now)

    alerts: list[TrendAlert] = []

    # Need a prior baseline to talk about a "trend" at all.
    if not previous:
        return alerts

    # --- Success rate degradation -------------------------------------
    recent_sr = metrics_mod.success_rate(recent)
    prev_sr = metrics_mod.success_rate(previous)
    if recent_sr < prev_sr:
        drop = prev_sr - recent_sr
        # A drop that pushes us under the RED threshold is critical.
        severity = (
            TrendSeverity.CRITICAL
            if recent_sr < cfg.red_threshold
            else TrendSeverity.WARNING
        )
        if drop >= 0.05:  # ignore tiny wobble
            alerts.append(
                TrendAlert(
                    repo=repo,
                    workflow=workflow,
                    kind="success_rate",
                    severity=severity,
                    description=(
                        f"Success rate dropped {drop:.0%} "
                        f"({prev_sr:.0%} -> {recent_sr:.0%})"
                    ),
                )
            )

    # --- Duration increase --------------------------------------------
    trend = metrics_mod.duration_trend(recent, previous)
    if trend > cfg.duration_increase_yellow:
        severity = (
            TrendSeverity.CRITICAL if trend >= 1.0 else TrendSeverity.WARNING
        )
        recent_avg = metrics_mod.average_duration(recent)
        prev_avg = metrics_mod.average_duration(previous)
        alerts.append(
            TrendAlert(
                repo=repo,
                workflow=workflow,
                kind="duration",
                severity=severity,
                description=(
                    f"Average duration up {trend:.0%} "
                    f"({prev_avg:.0f}s -> {recent_avg:.0f}s)"
                ),
            )
        )

    # --- Newly flaky ---------------------------------------------------
    recent_flaky = metrics_mod.flaky_count(recent)
    prev_flaky = metrics_mod.flaky_count(previous)
    if recent_flaky > prev_flaky and recent_flaky >= cfg.flaky_yellow:
        alerts.append(
            TrendAlert(
                repo=repo,
                workflow=workflow,
                kind="flaky",
                severity=TrendSeverity.WARNING,
                description=(
                    f"Flaky runs increased ({prev_flaky} -> {recent_flaky} this week)"
                ),
            )
        )

    return alerts


def detect_all(
    store: Store,
    scoring: ScoringConfig | None = None,
    now: datetime | None = None,
) -> list[TrendAlert]:
    """Detect trend alerts across every ``(repo, workflow)`` in the store."""
    now = now or datetime.now(timezone.utc)
    alerts: list[TrendAlert] = []
    for repo, workflow in store.distinct_workflows():
        runs = store.get_runs(repo, workflow, since=now - timedelta(days=14))
        alerts.extend(detect_for_workflow(repo, workflow, runs, scoring, now))
    return alerts
