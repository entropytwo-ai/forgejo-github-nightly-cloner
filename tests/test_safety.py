# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 forgejo-github-nightly-cloner contributors
"""Execution lock, failover lock, config and CLI safety behaviour."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from scripts import run
from scripts.common.config import ConfigError, build_config, load_config
from scripts.common.lock import AcquireLock, LockError
from scripts.failover import disable_sync, enable_sync, read_lock
from tests.conftest import FORGEJO_TOKEN, GITHUB_TOKEN


def test_second_lock_attempt_fails(tmp_path: Path):
    with AcquireLock("sync", 5, tmp_path):
        with pytest.raises(LockError, match="already running"):
            AcquireLock("sync", 5, tmp_path).acquire()
    # Released afterwards
    with AcquireLock("sync", 5, tmp_path) as lock:
        assert lock.path.exists()
    assert not (tmp_path / "forgejo-sync-sync.lock").exists()


def test_stale_lock_is_replaced(tmp_path: Path):
    path = tmp_path / "forgejo-sync-sync.lock"
    path.write_text(json.dumps({"pid": 999_999_999, "acquired_at": 0}))
    with AcquireLock("sync", 5, tmp_path) as lock:
        assert json.loads(lock.path.read_text())["pid"] == os.getpid()


def test_failover_disable_and_enable(tmp_path: Path, mock_forgejo_client, temp_state_db):
    temp_state_db.upsert_mirror("gh/a", "backup-user", "a", "active")
    temp_state_db.upsert_mirror("gh/b", "backup-user", "b", "writable")
    result = disable_sync(mock_forgejo_client, ["backup-user/a"], tmp_path, temp_state_db)
    assert result.changed == ["backup-user/a"]
    assert read_lock(tmp_path)["repos"] == ["backup-user/a"]
    mock_forgejo_client.set_mirror_enabled.assert_called_with("backup-user", "a", False)
    assert temp_state_db.get_mirror("gh/a")["status"] == "disabled"

    mock_forgejo_client.get_repo.return_value = {"mirror": True}
    result = enable_sync(mock_forgejo_client, ["backup-user/a", "backup-user/b"], tmp_path, temp_state_db, "24h")
    assert result.changed == ["backup-user/a"] and result.skipped == ["backup-user/b"]
    assert read_lock(tmp_path) is None
    assert temp_state_db.get_mirror("gh/a")["status"] == "active"


def test_enable_keeps_lock_on_failure(tmp_path: Path, mock_forgejo_client):
    disable_sync(mock_forgejo_client, [], tmp_path)
    mock_forgejo_client.get_repo.side_effect = RuntimeError("down")
    result = enable_sync(mock_forgejo_client, ["o/r"], tmp_path)
    assert result.failed and read_lock(tmp_path) is not None


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yml"
    cfg.write_text(yaml.safe_dump({"github": {"organizations": ["example-org"]}, "forgejo": {"owner": "backup-user"}}))
    return cfg


def _env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_TOKEN)
    monkeypatch.setenv("FORGEJO_TOKEN", FORGEJO_TOKEN)
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("LOCK_DIR", str(tmp_path / "locks"))


def test_sync_refused_when_failover_lock_present(tmp_path: Path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "failover.lock").write_text("{}")
    called = []
    monkeypatch.setattr(run, "discover", lambda *a, **k: called.append(1))
    assert run.main(["--config", str(_write_config(tmp_path)), "sync"]) == run.EXIT_REFUSED
    assert not called
    assert "sync REFUSED" in capsys.readouterr().out


def test_sync_refused_when_already_running(tmp_path: Path, monkeypatch):
    _env(monkeypatch, tmp_path)
    called = []
    monkeypatch.setattr(run, "discover", lambda *a, **k: called.append(1))
    with AcquireLock("sync", 5, tmp_path / "locks"):
        assert run.main(["--config", str(_write_config(tmp_path)), "sync"]) == run.EXIT_REFUSED
    assert not called


def test_status_command_runs(tmp_path: Path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    assert run.main(["--config", str(_write_config(tmp_path)), "status"]) == 0
    out = capsys.readouterr().out
    assert "Failover active : False" in out
    assert GITHUB_TOKEN not in out


def test_config_env_overlay_and_validation(tmp_path: Path):
    cfg = load_config(str(_write_config(tmp_path)), {"BACKUP_RETENTION_DAYS": "7", "DRY_RUN": "true", "GITHUB_TOKEN": "x"})
    assert cfg.backup.retention_days == 7 and cfg.dry_run is True
    with pytest.raises(ConfigError):
        build_config({"github": {}, "forgejo": {"owner": "x"}}, {})
    with pytest.raises(ConfigError):
        build_config({"github": {"accounts": ["a"]}, "forgejo": {"owner": "x"}, "backup": {"metadata_types": ["wiki"]}}, {})
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "missing.yml"), {})
