"""Health scoring: map :class:`WorkflowMetrics` onto RED/YELLOW/GREEN.

Thresholds come from :class:`~src.config.ScoringConfig` but default to the
canonical deploy-pulse values so the module is usable standalone.

Boundary semantics (important, and covered by tests):
    * success rate is compared with strict ``<`` — exactly ``0.70`` is *not*
      RED, exactly ``0.90`` is *not* YELLOW.
    * failure streak and flaky count use ``>=`` — the threshold value trips it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .config import ScoringConfig
from .metrics import WorkflowMetrics


class HealthStatus(str, Enum):
    """Traffic-light health status for a workflow."""

    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"

    @property
    def emoji(self) -> str:
        """Return the traffic-light emoji for this status."""
        return {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}[self.value]


@dataclass
class HealthResult:
    """A health status plus the human-readable reasons behind it."""

    status: HealthStatus
    reasons: list[str]


def score(metrics: WorkflowMetrics, scoring: ScoringConfig | None = None) -> HealthStatus:
    """Score a workflow's metrics into a :class:`HealthStatus`.

    RED wins over YELLOW wins over GREEN. Mirrors the reference logic:

        RED    if success_rate_7d < red_threshold OR failure_streak >= streak_red
        YELLOW if success_rate_7d < yellow_threshold
                  OR duration_trend > duration_increase_yellow
                  OR flaky_count_7d >= flaky_yellow
        GREEN  otherwise
    """
    cfg = scoring or ScoringConfig()

    if metrics.success_rate_7d < cfg.red_threshold or metrics.failure_streak >= cfg.streak_red:
        return HealthStatus.RED
    if (
        metrics.success_rate_7d < cfg.yellow_threshold
        or metrics.duration_trend > cfg.duration_increase_yellow
        or metrics.flaky_count_7d >= cfg.flaky_yellow
    ):
        return HealthStatus.YELLOW
    return HealthStatus.GREEN


def score_with_reasons(
    metrics: WorkflowMetrics, scoring: ScoringConfig | None = None
) -> HealthResult:
    """Score a workflow and explain *why* it landed where it did."""
    cfg = scoring or ScoringConfig()
    status = score(metrics, cfg)
    reasons: list[str] = []

    if metrics.success_rate_7d < cfg.red_threshold:
        reasons.append(
            f"7d success rate {metrics.success_rate_7d:.0%} below RED threshold "
            f"{cfg.red_threshold:.0%}"
        )
    elif metrics.success_rate_7d < cfg.yellow_threshold:
        reasons.append(
            f"7d success rate {metrics.success_rate_7d:.0%} below YELLOW threshold "
            f"{cfg.yellow_threshold:.0%}"
        )

    if metrics.failure_streak >= cfg.streak_red:
        reasons.append(f"{metrics.failure_streak} consecutive failures")

    if metrics.duration_trend > cfg.duration_increase_yellow:
        reasons.append(f"duration up {metrics.duration_trend:.0%} vs previous week")

    if metrics.flaky_count_7d >= cfg.flaky_yellow:
        reasons.append(f"{metrics.flaky_count_7d} flaky runs this week")

    if status is HealthStatus.GREEN and not reasons:
        reasons.append("all signals within healthy thresholds")

    return HealthResult(status=status, reasons=reasons)
