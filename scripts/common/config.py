# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 forgejo-github-nightly-cloner contributors
"""Configuration loading.

The YAML file (``CONFIG_FILE``, default ``/config/config.yml``) describes *what*
to mirror. Secrets and deployment-specific values come from environment
variables and are overlaid on top of the file. Secrets are never read from the
YAML file so that the config can be committed or shared safely.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import yaml

DEFAULT_CONFIG_FILE = "/config/config.yml"

SUPPORTED_METADATA_TYPES: List[str] = [
    "issues",
    "pull_requests",
    "labels",
    "milestones",
    "releases",
    "comments",
    "topics",
    "collaborators",
]

_INTERVAL_RE = re.compile(r"^(\d+h)?(\d+m)?(\d+s)?$")
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class ConfigError(ValueError):
    """Raised when the configuration is missing or invalid."""


@dataclass
class GitHubConfig:
    """Which GitHub repositories to discover."""

    accounts: List[str] = field(default_factory=list)
    organizations: List[str] = field(default_factory=list)
    exclude_repos: List[str] = field(default_factory=list)
    include_private: bool = True
    include_forks: bool = False
    include_archived: bool = False
    api_url: str = "https://api.github.com"


@dataclass
class ForgejoConfig:
    """Where and how mirrors are created in Forgejo."""

    owner: str = "backup-user"
    mirror_interval: str = "24h"
    lfs: bool = True
    force_private: bool = False
    repo_name_template: str = "{repo}"


@dataclass
class BackupConfig:
    """Metadata backup behaviour."""

    retention_days: int = 30
    min_keep: int = 2
    max_failed_repos: int = 0
    metadata_types: List[str] = field(default_factory=lambda: list(SUPPORTED_METADATA_TYPES))


@dataclass
class SyncConfig:
    """Retry, rate-limit and locking behaviour."""

    max_retries: int = 3
    retry_delay_seconds: int = 30
    rate_limit_buffer: int = 100
    batch_size: int = 50
    lock_timeout_minutes: int = 120


@dataclass
class Config:
    """Fully resolved runtime configuration."""

    github: GitHubConfig
    forgejo: ForgejoConfig
    backup: BackupConfig
    sync: SyncConfig
    github_token: str = ""
    forgejo_url: str = "http://forgejo:3000"
    forgejo_token: str = ""
    dry_run: bool = False
    log_level: str = "INFO"
    log_format: str = "text"
    state_dir: Path = Path("/state")
    backup_dir: Path = Path("/backups")
    lock_dir: Path = Path("/tmp")
    config_file: Optional[Path] = None

    @property
    def state_db_path(self) -> Path:
        """Path of the SQLite state database."""
        return self.state_dir / "state.db"

    @property
    def failover_lock_path(self) -> Path:
        """Path of the failover lock file that blocks scheduled syncs."""
        return self.state_dir / "failover.lock"

    def secrets(self) -> List[str]:
        """Return all secret values so the logger can redact them."""
        return [s for s in (self.github_token, self.forgejo_token) if s]


def parse_bool(value: Any) -> bool:
    """Parse common truthy/falsy spellings (``true``, ``1``, ``yes`` ...)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    raise ConfigError(f"Cannot interpret {value!r} as a boolean")


def _as_list(value: Any, name: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise ConfigError(f"'{name}' must be a list")
    return [str(v).strip() for v in value if str(v).strip()]


def _as_int(value: Any, name: str, minimum: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'{name}' must be an integer, got {value!r}") from exc
    if number < minimum:
        raise ConfigError(f"'{name}' must be >= {minimum}, got {number}")
    return number


def _section(raw: Mapping[str, Any], name: str) -> Dict[str, Any]:
    value = raw.get(name) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"Section '{name}' must be a mapping")
    return dict(value)


def build_config(raw: Mapping[str, Any], env: Mapping[str, str]) -> Config:
    """Build and validate a :class:`Config` from parsed YAML and environment."""
    gh = _section(raw, "github")
    fj = _section(raw, "forgejo")
    bk = _section(raw, "backup")
    sy = _section(raw, "sync")

    github = GitHubConfig(
        accounts=_as_list(gh.get("accounts"), "github.accounts"),
        organizations=_as_list(gh.get("organizations"), "github.organizations"),
        exclude_repos=_as_list(gh.get("exclude_repos"), "github.exclude_repos"),
        include_private=parse_bool(gh.get("include_private", True)),
        include_forks=parse_bool(gh.get("include_forks", False)),
        include_archived=parse_bool(gh.get("include_archived", False)),
        api_url=str(env.get("GITHUB_API_URL") or gh.get("api_url") or "https://api.github.com").rstrip("/"),
    )
    forgejo = ForgejoConfig(
        owner=str(fj.get("owner") or "").strip(),
        mirror_interval=str(fj.get("mirror_interval", "24h")).strip(),
        lfs=parse_bool(fj.get("lfs", True)),
        force_private=parse_bool(fj.get("force_private", False)),
        repo_name_template=str(fj.get("repo_name_template") or "{repo}"),
    )
    backup = BackupConfig(
        retention_days=_as_int(bk.get("retention_days", 30), "backup.retention_days", 1),
        min_keep=_as_int(bk.get("min_keep", 2), "backup.min_keep", 2),
        max_failed_repos=_as_int(bk.get("max_failed_repos", 0), "backup.max_failed_repos", 0),
        metadata_types=_as_list(bk.get("metadata_types"), "backup.metadata_types")
        or list(SUPPORTED_METADATA_TYPES),
    )
    sync = SyncConfig(
        max_retries=_as_int(sy.get("max_retries", 3), "sync.max_retries", 0),
        retry_delay_seconds=_as_int(sy.get("retry_delay_seconds", 30), "sync.retry_delay_seconds", 0),
        rate_limit_buffer=_as_int(sy.get("rate_limit_buffer", 100), "sync.rate_limit_buffer", 0),
        batch_size=_as_int(sy.get("batch_size", 50), "sync.batch_size", 1),
        lock_timeout_minutes=_as_int(sy.get("lock_timeout_minutes", 120), "sync.lock_timeout_minutes", 1),
    )

    # Environment overlay (env always wins over the file).
    if env.get("BACKUP_RETENTION_DAYS"):
        backup.retention_days = _as_int(env["BACKUP_RETENTION_DAYS"], "BACKUP_RETENTION_DAYS", 1)
    if env.get("FORGEJO_OWNER"):
        forgejo.owner = env["FORGEJO_OWNER"].strip()

    config = Config(
        github=github,
        forgejo=forgejo,
        backup=backup,
        sync=sync,
        github_token=env.get("GITHUB_TOKEN", "").strip(),
        forgejo_url=env.get("FORGEJO_URL", "http://forgejo:3000").strip().rstrip("/"),
        forgejo_token=env.get("FORGEJO_TOKEN", "").strip(),
        dry_run=parse_bool(env.get("DRY_RUN", "false")),
        log_level=env.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        log_format=env.get("LOG_FORMAT", "text").strip().lower() or "text",
        state_dir=Path(env.get("STATE_DIR", "/state")),
        backup_dir=Path(env.get("BACKUP_DIR", "/backups")),
        lock_dir=Path(env.get("LOCK_DIR", "/tmp")),
    )
    validate_config(config)
    return config


def validate_config(config: Config) -> None:
    """Validate structural requirements. Secrets are checked by :func:`require_secrets`."""
    if not config.github.accounts and not config.github.organizations:
        raise ConfigError("Configure at least one entry in github.accounts or github.organizations")
    if not config.forgejo.owner:
        raise ConfigError("forgejo.owner is required")
    if not _INTERVAL_RE.match(config.forgejo.mirror_interval) or config.forgejo.mirror_interval == "":
        raise ConfigError(f"forgejo.mirror_interval must look like '24h', '8h30m' (got {config.forgejo.mirror_interval!r})")
    if "{repo}" not in config.forgejo.repo_name_template:
        raise ConfigError("forgejo.repo_name_template must contain '{repo}'")
    unknown = sorted(set(config.backup.metadata_types) - set(SUPPORTED_METADATA_TYPES))
    if unknown:
        raise ConfigError(f"Unsupported backup.metadata_types: {', '.join(unknown)}")
    if config.log_level not in _VALID_LOG_LEVELS:
        raise ConfigError(f"LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}")
    if config.log_format not in {"text", "json"}:
        raise ConfigError("LOG_FORMAT must be 'text' or 'json'")
    if not config.forgejo_url.startswith(("http://", "https://")):
        raise ConfigError("FORGEJO_URL must start with http:// or https://")


def require_secrets(config: Config, github: bool = True, forgejo: bool = True) -> None:
    """Ensure the tokens needed by a command are present (values are never echoed)."""
    missing = []
    if github and not config.github_token:
        missing.append("GITHUB_TOKEN")
    if forgejo and not config.forgejo_token:
        missing.append("FORGEJO_TOKEN")
    if missing:
        raise ConfigError(f"Missing required environment variable(s): {', '.join(missing)}")


def load_config(path: Optional[str] = None, env: Optional[Mapping[str, str]] = None) -> Config:
    """Load the YAML config file and overlay environment variables.

    Args:
        path: Explicit config path. Falls back to ``$CONFIG_FILE`` then ``/config/config.yml``.
        env: Environment mapping (defaults to ``os.environ``); injectable for tests.
    """
    env = dict(os.environ if env is None else env)
    config_path = Path(path or env.get("CONFIG_FILE") or DEFAULT_CONFIG_FILE)
    if not config_path.is_file():
        raise ConfigError(f"Config file not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Top level of {config_path} must be a mapping")
    config = build_config(raw, env)
    config.config_file = config_path
    return config
