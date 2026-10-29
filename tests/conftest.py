# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 forgejo-github-nightly-cloner contributors
"""Shared pytest fixtures."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.common.config import Config, build_config  # noqa: E402
from scripts.common.forgejo_client import ForgejoClient  # noqa: E402
from scripts.common.github_client import GitHubClient, RepoInfo  # noqa: E402
from scripts.common.state import StateDB  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
# Realistic-looking fake tokens so redaction is exercised. Not real credentials.
GITHUB_TOKEN = "ghp_TESTtoken0123456789abcdefghijklmnopqr"
FORGEJO_TOKEN = "f0rgej0testtoken0123456789abcdef01234567"


def load_fixture(name: str) -> Any:
    """Load a JSON fixture from ``tests/fixtures``."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_repo(full_name: str, **overrides: Any) -> RepoInfo:
    """Build a :class:`RepoInfo` for tests."""
    owner, name = full_name.split("/", 1)
    values: Dict[str, Any] = dict(
        name=name,
        owner=owner,
        full_name=full_name,
        private=False,
        fork=False,
        archived=False,
        clone_url=f"https://github.com/{full_name}.git",
        description=f"{name} description",
    )
    values.update(overrides)
    return RepoInfo(**values)


@pytest.fixture
def temp_backup_dir(tmp_path: Path) -> Path:
    """Empty backup root."""
    path = tmp_path / "backups"
    path.mkdir()
    return path


@pytest.fixture
def mock_config(tmp_path: Path, temp_backup_dir: Path) -> Config:
    """Valid configuration pointing all paths into ``tmp_path``."""
    raw = {
        "github": {
            "accounts": ["example-user"],
            "organizations": ["example-org"],
            "exclude_repos": ["*/archived-*", "example-user/personal-*"],
            "include_private": True,
            "include_forks": False,
            "include_archived": False,
        },
        "forgejo": {"owner": "backup-user", "mirror_interval": "24h", "lfs": True},
        "backup": {"retention_days": 30},
        "sync": {"max_retries": 2, "retry_delay_seconds": 0, "rate_limit_buffer": 10, "lock_timeout_minutes": 5},
    }
    env = {
        "GITHUB_TOKEN": GITHUB_TOKEN,
        "FORGEJO_TOKEN": FORGEJO_TOKEN,
        "FORGEJO_URL": "http://forgejo.test:3000",
        "STATE_DIR": str(tmp_path / "state"),
        "BACKUP_DIR": str(temp_backup_dir),
        "LOCK_DIR": str(tmp_path / "locks"),
    }
    return build_config(raw, env)


@pytest.fixture
def temp_state_db(tmp_path: Path):
    """Fresh SQLite state database."""
    db = StateDB(tmp_path / "state" / "state.db")
    yield db
    db.close()


@pytest.fixture
def mock_github_client() -> MagicMock:
    """GitHub client mock constrained to the real interface."""
    return MagicMock(spec=GitHubClient)


@pytest.fixture
def mock_forgejo_client() -> MagicMock:
    """Forgejo client mock constrained to the real interface."""
    return MagicMock(spec=ForgejoClient)


@pytest.fixture
def org_repos() -> List[RepoInfo]:
    """RepoInfo objects built from the org fixture."""
    return [RepoInfo.from_api(r) for r in load_fixture("github_org_repos.json")]
