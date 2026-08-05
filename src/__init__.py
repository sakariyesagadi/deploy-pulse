"""deploy-pulse — GitHub Actions workflow health tracking.

Polls the GitHub Actions API across configured repositories, computes
per-workflow health metrics, scores them RED/YELLOW/GREEN, renders a static
dashboard, and sends weekly digests over email (SendGrid) and Slack.
"""

__version__ = "0.1.0"
