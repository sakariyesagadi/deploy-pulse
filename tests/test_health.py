"""Tests for health scoring — with a focus on the exact threshold boundaries.

The reference logic uses strict ``<`` on success rate, so *exactly* 0.70 is not
RED and *exactly* 0.90 is not YELLOW. These edge cases are asserted explicitly.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.config import ScoringConfig
from src.health import HealthStatus, score, score_with_reasons
from src.metrics import WorkflowMetrics

NOW = datetime(2024, 5, 10, tzinfo=timezone.utc)


def metrics(
    *,
    success_rate_7d: float = 1.0,
    failure_streak: int = 0,
    duration_trend: float = 0.0,
    flaky_count_7d: int = 0,
) -> WorkflowMetrics:
    """Build a WorkflowMetrics with only the scoring-relevant fields set."""
    return WorkflowMetrics(
        workflow="ci.yml",
        repo="acme/app",
        success_rate_7d=success_rate_7d,
        success_rate_30d=success_rate_7d,
        failure_streak=failure_streak,
        avg_duration_7d=60.0,
        avg_duration_30d=60.0,
        duration_trend=duration_trend,
        flaky_count_7d=flaky_count_7d,
        total_runs_7d=10,
        last_run_status="success",
        last_run_at=NOW,
    )


# --- Success-rate boundaries -----------------------------------------

def test_exactly_90_percent_is_green():
    # 0.90 is NOT < 0.90 -> not YELLOW; healthy otherwise -> GREEN
    assert score(metrics(success_rate_7d=0.90)) is HealthStatus.GREEN


def test_just_below_90_percent_is_yellow():
    assert score(metrics(success_rate_7d=0.8999)) is HealthStatus.YELLOW


def test_between_70_and_90_is_yellow():
    assert score(metrics(success_rate_7d=0.80)) is HealthStatus.YELLOW


def test_exactly_70_percent_is_yellow_not_red():
    # 0.70 is NOT < 0.70 -> not RED; but IS < 0.90 -> YELLOW
    assert score(metrics(success_rate_7d=0.70)) is HealthStatus.YELLOW


def test_just_below_70_percent_is_red():
    assert score(metrics(success_rate_7d=0.6999)) is HealthStatus.RED


def test_well_below_70_is_red():
    assert score(metrics(success_rate_7d=0.5)) is HealthStatus.RED


# --- Failure streak --------------------------------------------------

def test_streak_of_3_is_red():
    assert score(metrics(success_rate_7d=1.0, failure_streak=3)) is HealthStatus.RED


def test_streak_of_2_with_perfect_rate_is_green():
    assert score(metrics(success_rate_7d=1.0, failure_streak=2)) is HealthStatus.GREEN


# --- Duration trend --------------------------------------------------

def test_exactly_20_percent_duration_trend_is_green():
    # trend > 0.20 is required for YELLOW; exactly 0.20 does not trip it
    assert score(metrics(duration_trend=0.20)) is HealthStatus.GREEN


def test_just_above_20_percent_duration_trend_is_yellow():
    assert score(metrics(duration_trend=0.2001)) is HealthStatus.YELLOW


# --- Flaky count -----------------------------------------------------

def test_two_flaky_runs_is_yellow():
    assert score(metrics(flaky_count_7d=2)) is HealthStatus.YELLOW


def test_one_flaky_run_is_green():
    assert score(metrics(flaky_count_7d=1)) is HealthStatus.GREEN


# --- Priority / combinations -----------------------------------------

def test_red_beats_yellow():
    # low success rate (RED) AND flaky (YELLOW) -> RED wins
    m = metrics(success_rate_7d=0.5, flaky_count_7d=5)
    assert score(m) is HealthStatus.RED


def test_all_healthy_is_green():
    assert score(metrics()) is HealthStatus.GREEN


# --- Custom thresholds -----------------------------------------------

def test_custom_thresholds_respected():
    cfg = ScoringConfig(red_threshold=0.5, yellow_threshold=0.8, streak_red=5)
    # 0.60 is above custom red (0.5) but below custom yellow (0.8) -> YELLOW
    assert score(metrics(success_rate_7d=0.60), cfg) is HealthStatus.YELLOW
    # streak of 4 is below custom streak_red 5, perfect rate -> GREEN
    assert score(metrics(success_rate_7d=1.0, failure_streak=4), cfg) is HealthStatus.GREEN


# --- Reasons ---------------------------------------------------------

def test_score_with_reasons_red():
    result = score_with_reasons(metrics(success_rate_7d=0.5, failure_streak=3))
    assert result.status is HealthStatus.RED
    assert any("success rate" in r for r in result.reasons)
    assert any("consecutive failures" in r for r in result.reasons)


def test_score_with_reasons_green_has_message():
    result = score_with_reasons(metrics())
    assert result.status is HealthStatus.GREEN
    assert result.reasons  # non-empty explanatory message


def test_emoji_mapping():
    assert HealthStatus.GREEN.emoji == "🟢"
    assert HealthStatus.YELLOW.emoji == "🟡"
    assert HealthStatus.RED.emoji == "🔴"
