"""Configuration loading for deploy-pulse.

Loads the YAML config file, expands ``${ENV_VAR}`` references against the
process environment, and validates the result into typed dataclasses.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Matches ${VAR} or $VAR style references inside string values.
_ENV_PATTERN = re.compile(r"\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|\$(?P<bare>[A-Za-z_][A-Za-z0-9_]*)")

DEFAULT_CONFIG_PATH = "config.yml"


class ConfigError(ValueError):
    """Raised when the configuration is missing required fields or malformed."""


@dataclass(frozen=True)
class RepoConfig:
    """A single repository to monitor."""

    owner: str
    name: str
    workflows: list[str] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        """Return the ``owner/name`` slug used by the GitHub API."""
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class ScoringConfig:
    """Thresholds that drive health scoring."""

    red_threshold: float = 0.70
    yellow_threshold: float = 0.90
    streak_red: int = 3
    duration_increase_yellow: float = 0.20
    flaky_yellow: int = 2


@dataclass(frozen=True)
class EmailConfig:
    """Email digest settings."""

    to: str | None = None
    from_: str | None = None
    sendgrid_key: str | None = None

    @property
    def enabled(self) -> bool:
        """True when enough is configured to send an email."""
        return bool(self.to and self.from_ and self.sendgrid_key)


@dataclass(frozen=True)
class NotificationConfig:
    """Notification channel settings."""

    slack_webhook: str | None = None
    email: EmailConfig = field(default_factory=EmailConfig)


@dataclass(frozen=True)
class Config:
    """Top-level deploy-pulse configuration."""

    repos: list[RepoConfig]
    scoring: ScoringConfig
    notifications: NotificationConfig
    schedule: str = "0 9 * * 1"


def _expand_env(value: Any, environ: dict[str, str]) -> Any:
    """Recursively expand ``${ENV_VAR}`` references in strings.

    Unset variables expand to an empty string so that optional notification
    channels degrade gracefully rather than raising.
    """
    if isinstance(value, str):
        def _replace(match: re.Match[str]) -> str:
            name = match.group("braced") or match.group("bare")
            return environ.get(name, "")

        return _ENV_PATTERN.sub(_replace, value)
    if isinstance(value, list):
        return [_expand_env(item, environ) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item, environ) for key, item in value.items()}
    return value


def _parse_repos(raw: Any) -> list[RepoConfig]:
    """Validate and build the list of repos to monitor."""
    if not isinstance(raw, list) or not raw:
        raise ConfigError("`repos` must be a non-empty list")
    repos: list[RepoConfig] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"repos[{index}] must be a mapping")
        owner = entry.get("owner")
        name = entry.get("name")
        if not owner or not name:
            raise ConfigError(f"repos[{index}] requires both `owner` and `name`")
        workflows = entry.get("workflows") or []
        if not isinstance(workflows, list):
            raise ConfigError(f"repos[{index}].workflows must be a list")
        repos.append(RepoConfig(owner=str(owner), name=str(name), workflows=[str(w) for w in workflows]))
    return repos


def _parse_scoring(raw: Any) -> ScoringConfig:
    """Build the scoring config, falling back to defaults for missing keys."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError("`scoring` must be a mapping")
    defaults = ScoringConfig()
    return ScoringConfig(
        red_threshold=float(raw.get("red_threshold", defaults.red_threshold)),
        yellow_threshold=float(raw.get("yellow_threshold", defaults.yellow_threshold)),
        streak_red=int(raw.get("streak_red", defaults.streak_red)),
        duration_increase_yellow=float(
            raw.get("duration_increase_yellow", defaults.duration_increase_yellow)
        ),
        flaky_yellow=int(raw.get("flaky_yellow", defaults.flaky_yellow)),
    )


def _parse_notifications(raw: Any) -> NotificationConfig:
    """Build the notification config."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError("`notifications` must be a mapping")
    email_raw = raw.get("email") or {}
    if not isinstance(email_raw, dict):
        raise ConfigError("`notifications.email` must be a mapping")
    # Empty strings (from unset env vars) are normalised to None.
    slack = raw.get("slack_webhook") or None
    email = EmailConfig(
        to=email_raw.get("to") or None,
        from_=email_raw.get("from") or None,
        sendgrid_key=email_raw.get("sendgrid_key") or None,
    )
    return NotificationConfig(slack_webhook=slack, email=email)


def load_config(
    path: str | Path | None = None,
    environ: dict[str, str] | None = None,
) -> Config:
    """Load and validate configuration from a YAML file.

    Args:
        path: Path to the config file. Defaults to ``$DEPLOY_PULSE_CONFIG`` or
            ``config.yml``.
        environ: Environment mapping used for ``${VAR}`` expansion. Defaults to
            ``os.environ``.

    Returns:
        A validated :class:`Config`.

    Raises:
        ConfigError: If the file is missing or malformed.
    """
    environ = os.environ.copy() if environ is None else environ
    if path is None:
        path = environ.get("DEPLOY_PULSE_CONFIG", DEFAULT_CONFIG_PATH)
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    if not isinstance(raw, dict):
        raise ConfigError("Config root must be a mapping")

    raw = _expand_env(raw, environ)

    return Config(
        repos=_parse_repos(raw.get("repos")),
        scoring=_parse_scoring(raw.get("scoring")),
        notifications=_parse_notifications(raw.get("notifications")),
        schedule=str(raw.get("schedule", "0 9 * * 1")),
    )
