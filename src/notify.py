"""Weekly digest notifications over email (SendGrid) and Slack webhook.

Builds a digest from current metrics, health scores, and trend alerts, then
delivers it to whichever channels are configured. Missing configuration for a
channel is skipped silently (with a log line) rather than treated as an error.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import Config, NotificationConfig, ScoringConfig, load_config
from .health import HealthStatus, score_with_reasons
from .metrics import compute_all
from .store import Store
from .trends import TrendAlert, detect_all

logger = logging.getLogger("deploy_pulse.notify")

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
SENDGRID_ENDPOINT = "https://api.sendgrid.com/v3/mail/send"

# Ordering so the worst offenders float to the top of digests.
_STATUS_ORDER = {HealthStatus.RED: 0, HealthStatus.YELLOW: 1, HealthStatus.GREEN: 2}


@dataclass
class DigestData:
    """Everything needed to render a weekly digest."""

    generated_at: str
    counts: dict[str, int]
    workflows: list[dict[str, Any]]
    trends: list[dict[str, Any]]

    @property
    def headline_status(self) -> str:
        """Overall status: RED if any red, else YELLOW if any yellow, else GREEN."""
        if self.counts.get("RED"):
            return "RED"
        if self.counts.get("YELLOW"):
            return "YELLOW"
        return "GREEN"


def build_digest(
    store: Store,
    scoring: ScoringConfig | None = None,
    now: datetime | None = None,
) -> DigestData:
    """Assemble the digest data model from the store."""
    cfg = scoring or ScoringConfig()
    now = now or datetime.now(timezone.utc)

    counts = {"GREEN": 0, "YELLOW": 0, "RED": 0}
    workflows: list[dict[str, Any]] = []

    for metrics in compute_all(store, now=now):
        health = score_with_reasons(metrics, cfg)
        counts[health.status.value] += 1
        workflows.append(
            {
                "repo": metrics.repo,
                "workflow": metrics.workflow,
                "status": health.status,
                "status_value": health.status.value,
                "emoji": health.status.emoji,
                "success_rate_7d": metrics.success_rate_7d,
                "success_rate_7d_pct": round(100 * metrics.success_rate_7d, 1),
                "failure_streak": metrics.failure_streak,
                "flaky_count_7d": metrics.flaky_count_7d,
                "duration_trend_pct": round(100 * metrics.duration_trend, 1),
                "total_runs_7d": metrics.total_runs_7d,
                "reasons": health.reasons,
            }
        )

    workflows.sort(key=lambda w: (_STATUS_ORDER[w["status"]], w["repo"], w["workflow"]))

    trends = [
        {
            "repo": alert.repo,
            "workflow": alert.workflow,
            "kind": alert.kind,
            "severity": alert.severity.value,
            "description": alert.description,
        }
        for alert in detect_all(store, cfg, now)
    ]

    return DigestData(
        generated_at=now.strftime("%Y-%m-%d %H:%M UTC"),
        counts=counts,
        workflows=workflows,
        trends=trends,
    )


def render_email_html(digest: DigestData, template_dir: Path = TEMPLATE_DIR) -> str:
    """Render the HTML email body for a digest."""
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("digest.html")
    return template.render(
        generated_at=digest.generated_at,
        counts=digest.counts,
        headline_status=digest.headline_status,
        workflows=digest.workflows,
        trends=digest.trends,
    )


def build_slack_blocks(digest: DigestData) -> dict[str, Any]:
    """Build a Slack Block Kit payload summarising the digest."""
    emoji = {"RED": "🔴", "YELLOW": "🟡", "GREEN": "🟢"}[digest.headline_status]
    header = f"{emoji} deploy-pulse weekly digest — {digest.generated_at}"

    summary = (
        f"*{digest.counts['GREEN']}* green · "
        f"*{digest.counts['YELLOW']}* yellow · "
        f"*{digest.counts['RED']}* red"
    )

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header}},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
    ]

    # Only surface non-green workflows in Slack to keep it scannable.
    attention = [w for w in digest.workflows if w["status_value"] != "GREEN"]
    if attention:
        blocks.append({"type": "divider"})
        lines = []
        for w in attention[:15]:
            reason = "; ".join(w["reasons"]) if w["reasons"] else ""
            lines.append(f"{w['emoji']} *{w['repo']}* / `{w['workflow']}` — {reason}")
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}
        )
    else:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "All workflows healthy. 🎉"},
            }
        )

    return {"blocks": blocks, "text": header}


def send_slack(
    webhook_url: str,
    digest: DigestData,
    client: httpx.Client | None = None,
) -> bool:
    """Post the digest to a Slack incoming webhook. Returns success."""
    payload = build_slack_blocks(digest)
    owns_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        response = client.post(webhook_url, json=payload)
        if response.status_code >= 400:
            logger.warning("Slack webhook failed: %s %s", response.status_code, response.text[:200])
            return False
        logger.info("Posted digest to Slack")
        return True
    except httpx.HTTPError as exc:
        logger.warning("Slack webhook error: %s", exc)
        return False
    finally:
        if owns_client:
            client.close()


def send_email(
    email_to: str,
    email_from: str,
    sendgrid_key: str,
    digest: DigestData,
    client: httpx.Client | None = None,
) -> bool:
    """Send the digest as an HTML email via the SendGrid v3 API. Returns success."""
    html = render_email_html(digest)
    subject = (
        f"deploy-pulse weekly digest — "
        f"{digest.counts['RED']} red / {digest.counts['YELLOW']} yellow"
    )
    payload = {
        "personalizations": [{"to": [{"email": email_to}]}],
        "from": {"email": email_from},
        "subject": subject,
        "content": [{"type": "text/html", "value": html}],
    }
    headers = {
        "Authorization": f"Bearer {sendgrid_key}",
        "Content-Type": "application/json",
    }
    owns_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        response = client.post(SENDGRID_ENDPOINT, json=payload, headers=headers)
        if response.status_code >= 400:
            logger.warning("SendGrid failed: %s %s", response.status_code, response.text[:200])
            return False
        logger.info("Sent digest email to %s", email_to)
        return True
    except httpx.HTTPError as exc:
        logger.warning("SendGrid error: %s", exc)
        return False
    finally:
        if owns_client:
            client.close()


def dispatch(
    digest: DigestData,
    notifications: NotificationConfig,
    client: httpx.Client | None = None,
) -> dict[str, bool]:
    """Send the digest to every configured channel. Returns per-channel results."""
    results: dict[str, bool] = {}

    if notifications.slack_webhook:
        results["slack"] = send_slack(notifications.slack_webhook, digest, client)
    else:
        logger.info("Slack webhook not configured; skipping")

    email = notifications.email
    if email.enabled:
        results["email"] = send_email(
            email.to, email.from_, email.sendgrid_key, digest, client  # type: ignore[arg-type]
        )
    else:
        logger.info("Email not fully configured; skipping")

    return results


def main() -> int:
    """CLI entry point: build the digest and dispatch to configured channels."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config: Config = load_config()
    db_path = os.environ.get("DEPLOY_PULSE_DB", "deploy_pulse.db")
    with Store(db_path) as store:
        digest = build_digest(store, scoring=config.scoring)
    results = dispatch(digest, config.notifications)
    logger.info("Digest dispatch results: %s", results or "no channels configured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
