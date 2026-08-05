"""Tests for metric calculation — success rate, streaks, duration, flakiness."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.metrics import (
    average_duration,
    compute_metrics,
    duration_trend,
    failure_streak,
    flaky_count,
    success_rate,
)
from tests.conftest import days_ago, make_run

NOW = datetime(2024, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


def test_success_rate_basic():
    runs = [
        make_run(id=1, conclusion="success"),
        make_run(id=2, conclusion="success"),
        make_run(id=3, conclusion="failure"),
        make_run(id=4, conclusion="success"),
    ]
    assert success_rate(runs) == pytest.approx(0.75)


def test_success_rate_ignores_cancelled_and_skipped():
    runs = [
        make_run(id=1, conclusion="success"),
        make_run(id=2, conclusion="cancelled"),
        make_run(id=3, conclusion="skipped"),
        make_run(id=4, conclusion="failure"),
    ]
    # Only success + failure count -> 1/2
    assert success_rate(runs) == pytest.approx(0.5)


def test_success_rate_empty_is_healthy():
    assert success_rate([]) == 1.0
    assert success_rate([make_run(id=1, conclusion="cancelled")]) == 1.0


def test_failure_streak_counts_from_newest():
    # newest-first ordering
    runs = [
        make_run(id=5, conclusion="failure", created_at=days_ago(0, NOW)),
        make_run(id=4, conclusion="failure", created_at=days_ago(1, NOW)),
        make_run(id=3, conclusion="failure", created_at=days_ago(2, NOW)),
        make_run(id=2, conclusion="success", created_at=days_ago(3, NOW)),
    ]
    assert failure_streak(runs) == 3


def test_failure_streak_broken_by_success():
    runs = [
        make_run(id=3, conclusion="success"),
        make_run(id=2, conclusion="failure"),
        make_run(id=1, conclusion="failure"),
    ]
    assert failure_streak(runs) == 0


def test_failure_streak_transparent_to_cancelled():
    runs = [
        make_run(id=4, conclusion="failure"),
        make_run(id=3, conclusion="cancelled"),  # ignored, doesn't break streak
        make_run(id=2, conclusion="failure"),
        make_run(id=1, conclusion="success"),
    ]
    assert failure_streak(runs) == 2


def test_average_duration():
    runs = [
        make_run(id=1, duration_s=100.0),
        make_run(id=2, duration_s=200.0),
        make_run(id=3, duration_s=None),  # excluded
    ]
    assert average_duration(runs) == pytest.approx(150.0)


def test_average_duration_empty():
    assert average_duration([]) == 0.0


def test_duration_trend_positive():
    recent = [make_run(id=1, duration_s=120.0)]
    previous = [make_run(id=2, duration_s=100.0)]
    assert duration_trend(recent, previous) == pytest.approx(0.20)


def test_duration_trend_no_baseline():
    recent = [make_run(id=1, duration_s=120.0)]
    assert duration_trend(recent, []) == 0.0


def test_flaky_count_detects_pass_fail_pass_same_day():
    day = datetime(2024, 5, 8, tzinfo=timezone.utc)
    runs = [
        make_run(id=1, conclusion="success", created_at=day.replace(hour=9)),
        make_run(id=2, conclusion="failure", created_at=day.replace(hour=10)),
        make_run(id=3, conclusion="success", created_at=day.replace(hour=11)),
    ]
    assert flaky_count(runs) == 1


def test_flaky_count_ignores_cross_day():
    runs = [
        make_run(id=1, conclusion="success", created_at=datetime(2024, 5, 8, 9, tzinfo=timezone.utc)),
        make_run(id=2, conclusion="failure", created_at=datetime(2024, 5, 8, 23, tzinfo=timezone.utc)),
        make_run(id=3, conclusion="success", created_at=datetime(2024, 5, 9, 1, tzinfo=timezone.utc)),
    ]
    # fail then pass are on different days -> not flaky
    assert flaky_count(runs) == 0


def test_compute_metrics_windows():
    runs = [
        # within 7d
        make_run(id=1, conclusion="success", created_at=days_ago(1, NOW), duration_s=100.0),
        make_run(id=2, conclusion="failure", created_at=days_ago(2, NOW), duration_s=110.0),
        # previous 7d window (7-14 days ago)
        make_run(id=3, conclusion="success", created_at=days_ago(9, NOW), duration_s=80.0),
        # within 30d only
        make_run(id=4, conclusion="success", created_at=days_ago(20, NOW), duration_s=90.0),
    ]
    m = compute_metrics("acme/app", "ci.yml", runs, now=NOW)
    assert m is not None
    assert m.total_runs_7d == 2
    assert m.success_rate_7d == pytest.approx(0.5)
    # 30d success: 3 success / 4 total
    assert m.success_rate_30d == pytest.approx(0.75)
    assert m.last_run_status == "success"
    # duration trend: recent avg (105) vs previous (80)
    assert m.duration_trend == pytest.approx((105 - 80) / 80)


def test_compute_metrics_empty_returns_none():
    assert compute_metrics("acme/app", "ci.yml", [], now=NOW) is None
