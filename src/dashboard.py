"""Static dashboard generation.

Renders a self-contained HTML dashboard (Jinja2 + Chart.js from a CDN) into the
``docs/`` directory so it can be published to GitHub Pages. Also emits a
``data.json`` sidecar with the raw view model for debugging / reuse.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import ScoringConfig, load_config
from .health import score_with_reasons
from .metrics import WorkflowMetrics, compute_all
from .store import Store, WorkflowRun

logger = logging.getLogger("deploy_pulse.dashboard")

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "index.html"


def _daily_series(runs: list[WorkflowRun], now: datetime, days: int = 30) -> dict[str, list]:
    """Build per-day success-rate and average-duration series for charting.

    Returns a dict with ``labels`` (ISO dates, oldest first), ``success_rate``
    (0-100 per day, ``None`` where no runs), and ``avg_duration`` (seconds).
    """
    labels: list[str] = []
    success_series: list[float | None] = []
    duration_series: list[float | None] = []

    for offset in range(days - 1, -1, -1):
        day = (now - timedelta(days=offset)).date()
        labels.append(day.isoformat())
        day_runs = [r for r in runs if r.created_at.date() == day]
        scoreable = [r for r in day_runs if r.conclusion not in {None, "cancelled", "skipped", "stale", "neutral", "action_required"}]
        if scoreable:
            successes = sum(1 for r in scoreable if r.conclusion == "success")
            success_series.append(round(100 * successes / len(scoreable), 1))
        else:
            success_series.append(None)
        durations = [r.duration_s for r in day_runs if r.duration_s is not None]
        duration_series.append(round(sum(durations) / len(durations), 1) if durations else None)

    return {
        "labels": labels,
        "success_rate": success_series,
        "avg_duration": duration_series,
    }


def build_view_model(
    store: Store,
    scoring: ScoringConfig | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the full data structure passed to the dashboard template."""
    cfg = scoring or ScoringConfig()
    now = now or datetime.now(timezone.utc)
    all_metrics = compute_all(store, now=now)

    cards: list[dict[str, Any]] = []
    counts = {"GREEN": 0, "YELLOW": 0, "RED": 0}

    for metrics in sorted(all_metrics, key=lambda m: (m.repo, m.workflow)):
        health = score_with_reasons(metrics, cfg)
        counts[health.status.value] += 1
        runs = store.get_runs(metrics.repo, metrics.workflow, since=now - timedelta(days=30))
        cards.append(
            {
                "metrics": _metrics_to_dict(metrics),
                "status": health.status.value,
                "emoji": health.status.emoji,
                "reasons": health.reasons,
                "series": _daily_series(runs, now),
            }
        )

    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M UTC"),
        "counts": counts,
        "total": len(cards),
        "cards": cards,
    }


def _metrics_to_dict(metrics: WorkflowMetrics) -> dict[str, Any]:
    """Serialise metrics for template/JSON use (datetimes -> ISO strings)."""
    data = asdict(metrics)
    data["last_run_at"] = metrics.last_run_at.isoformat()
    # Pre-format a few display-friendly fields.
    data["success_rate_7d_pct"] = round(100 * metrics.success_rate_7d, 1)
    data["success_rate_30d_pct"] = round(100 * metrics.success_rate_30d, 1)
    data["duration_trend_pct"] = round(100 * metrics.duration_trend, 1)
    data["avg_duration_7d_str"] = _fmt_duration(metrics.avg_duration_7d)
    data["avg_duration_30d_str"] = _fmt_duration(metrics.avg_duration_30d)
    return data


def _fmt_duration(seconds: float) -> str:
    """Format seconds as a compact ``Xm Ys`` / ``Ys`` string."""
    if seconds <= 0:
        return "—"
    minutes, secs = divmod(int(round(seconds)), 60)
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def render_dashboard(view_model: dict[str, Any], template_dir: Path = TEMPLATE_DIR) -> str:
    """Render the dashboard HTML string from a view model."""
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("dashboard.html")
    return template.render(**view_model)


def generate(
    store: Store,
    output_path: Path | str = DEFAULT_OUTPUT,
    scoring: ScoringConfig | None = None,
    now: datetime | None = None,
) -> Path:
    """Build the view model, render HTML, and write it (plus data.json)."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    view_model = build_view_model(store, scoring=scoring, now=now)
    html = render_dashboard(view_model)
    output_path.write_text(html, encoding="utf-8")
    (output_path.parent / "data.json").write_text(
        json.dumps(view_model, indent=2), encoding="utf-8"
    )
    logger.info("Wrote dashboard to %s (%s workflows)", output_path, view_model["total"])
    return output_path


def main() -> int:
    """CLI entry point: render the dashboard from the current database."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = load_config()
    db_path = os.environ.get("DEPLOY_PULSE_DB", "deploy_pulse.db")
    with Store(db_path) as store:
        generate(store, scoring=config.scoring)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
