# deploy-pulse

Tracks GitHub Actions workflow health across my repositories — success rates, failure streaks, duration trends, and flaky runs — scores each workflow RED/YELLOW/GREEN, renders a static dashboard, and sends me a weekly digest over email and Slack.

## Why I built it

I wanted the same visibility into my personal project deployments that I have at work — success rates, failure trends, flaky tests, time-to-deploy. This polls GitHub Actions, scores health, and gives me a weekly digest so I can catch degradation before it becomes a mess.

The nice bit: it runs *as* a GitHub Actions workflow that monitors my other workflows. It's turtles all the way down.

## What it does

- **Collects** workflow runs for a configured list of repos/workflows over the last 30 days, storing them in SQLite. Uses conditional requests (ETag) so repeated polls stay cheap and rate-limit friendly.
- **Computes** per-workflow metrics: 7d/30d success rate, current failure streak, average duration, duration trend (is it getting slower?), and flaky-run count (pass → fail → pass in the same day).
- **Scores** each workflow into a traffic light:
  - 🟢 **GREEN** — success rate ≥ 90%, no failure streak, duration stable
  - 🟡 **YELLOW** — success rate 70–90%, *or* duration up > 20%, *or* 2+ flaky runs
  - 🔴 **RED** — success rate < 70%, *or* 3+ consecutive failures
- **Detects trends** by comparing this week against last week, and calls out degrading success rates, slowing pipelines, and newly flaky workflows.
- **Renders** a self-contained static dashboard (Jinja2 + Chart.js) suitable for GitHub Pages.
- **Notifies** via a weekly digest to Slack (incoming webhook) and/or email (SendGrid).

## Health scoring

The scoring is deliberately simple and the boundaries are exact (and tested):

```python
def score(metrics: WorkflowMetrics) -> HealthStatus:
    if metrics.success_rate_7d < 0.70 or metrics.failure_streak >= 3:
        return HealthStatus.RED
    if (metrics.success_rate_7d < 0.90 or
        metrics.duration_trend > 0.20 or
        metrics.flaky_count_7d >= 2):
        return HealthStatus.YELLOW
    return HealthStatus.GREEN
```

Success rate uses a strict `<`, so a workflow sitting at *exactly* 70% is YELLOW (not RED), and one at *exactly* 90% is GREEN (not YELLOW). All thresholds are overridable in `config.yml`.

## Architecture

```
                    ┌──────────────────────────────────────────┐
                    │        GitHub Actions (cron, weekly)       │
                    └──────────────────────────────────────────┘
                                       │
        ┌──────────────┬───────────────┼───────────────┬──────────────┐
        ▼              ▼               ▼               ▼              ▼
   collector.py     store.py       metrics.py       health.py      trends.py
   poll Actions ─▶  SQLite     ─▶  success rate ─▶  RED/YELLOW  ─▶  week-over-week
   API (httpx)      (runs +        streaks,          /GREEN         change alerts
   ETag cache       ETags)         duration, flaky
                                       │
                        ┌──────────────┴───────────────┐
                        ▼                               ▼
                   dashboard.py                     notify.py
                   Jinja2 + Chart.js                SendGrid email +
                   → docs/index.html                Slack webhook
                        │
                        ▼
                  GitHub Pages
```

Each stage is an independent, testable module. `store.py` is the seam between
collection and everything downstream — collect once, then metrics/health/trends/
dashboard/notify all read from the same SQLite database.

### The `WorkflowMetrics` model

```python
@dataclass
class WorkflowMetrics:
    workflow: str
    repo: str
    success_rate_7d: float
    success_rate_30d: float
    failure_streak: int        # consecutive failures (current)
    avg_duration_7d: float     # seconds
    avg_duration_30d: float
    duration_trend: float      # % change (positive = getting slower)
    flaky_count_7d: int        # pass→fail→pass in same day
    total_runs_7d: int
    last_run_status: str
    last_run_at: datetime
```

## Running locally

```bash
# 1. Set up a virtualenv and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env          # fill in GITHUB_TOKEN (+ Slack/SendGrid if wanted)
$EDITOR config.yml            # list the repos/workflows to watch

# 3. Run the pipeline
python -m src.collector       # poll GitHub Actions -> SQLite
python -m src.dashboard       # render docs/index.html
python -m src.notify          # send the weekly digest

# Then just open docs/index.html in a browser.
```

You only need a GitHub token with read access to Actions (`actions:read`, or the
broader `repo` scope for private repos). Slack and SendGrid are optional — if they
aren't configured, those channels are skipped silently.

### With Docker

```bash
docker build -t deploy-pulse .
docker run --rm \
  -e GITHUB_TOKEN=ghp_xxx \
  -e SLACK_WEBHOOK_URL=https://hooks.slack.com/... \
  -v "$PWD/data:/data" \
  deploy-pulse
```

### As a scheduled GitHub Action

`.github/workflows/pulse.yml` runs the whole thing every Monday at 9am UTC (and on
manual dispatch): collect → render → notify → publish the dashboard to GitHub Pages.
Add `SLACK_WEBHOOK_URL` and `SENDGRID_API_KEY` as repository secrets to enable
notifications; `GITHUB_TOKEN` is provided automatically.

## Configuration

Everything lives in `config.yml`. Secrets are referenced as `${ENV_VAR}` and expanded
at load time, so they never sit in the file.

```yaml
repos:
  - owner: sakariyesagadi
    name: football-organiser
    workflows: ["ci.yml", "deploy.yml"]
  - owner: sakariyesagadi
    name: life-dashboard
    workflows: ["test.yml"]

scoring:
  red_threshold: 0.70           # success rate below this => RED
  yellow_threshold: 0.90        # success rate below this => YELLOW
  streak_red: 3                 # this many consecutive failures => RED
  duration_increase_yellow: 0.20  # duration up > 20% => YELLOW
  flaky_yellow: 2               # 2+ flaky runs => YELLOW

notifications:
  slack_webhook: ${SLACK_WEBHOOK_URL}
  email:
    to: sakariye.sagadi@gmail.com
    from: deploy-pulse@example.com
    sendgrid_key: ${SENDGRID_API_KEY}

schedule: "0 9 * * 1"  # Weekly Monday 9am UTC
```

## Tests

```bash
pip install -r requirements.txt
pytest                 # or: pytest --cov=src
```

The suite covers API response parsing / pagination / rate-limit handling
(`test_collector.py`), metric math (`test_metrics.py`), every scoring boundary
including the exact 70% and 90% edges (`test_health.py`), trend detection
(`test_trends.py`), and the storage/dashboard/notification layers
(`test_store_and_output.py`). A sample GitHub API response lives in
`tests/fixtures/workflow_runs.json`.

## Stack

- **Language:** Python 3.11+ (type hints and docstrings throughout)
- **GitHub API:** [httpx](https://www.python-httpx.org/) against the REST API v3, with ETag conditional requests
- **Storage:** SQLite (standard-library `sqlite3`)
- **Dashboard:** static HTML via Jinja2 + [Chart.js](https://www.chartjs.org/)
- **Notifications:** SendGrid (email) + Slack incoming webhook
- **Config:** YAML with `${ENV}` expansion
- **Deployment:** GitHub Actions cron → GitHub Pages, or Docker/local cron
- **Tests:** pytest with `httpx.MockTransport`
