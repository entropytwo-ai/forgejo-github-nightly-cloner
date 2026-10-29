# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 forgejo-github-nightly-cloner contributors
"""Failover helpers.

``disable_sync`` writes ``failover.lock`` *first* (so the scheduler stops even if
some API calls fail) and then sets ``mirror_interval=0`` on every mirror.
``enable_sync`` restores the interval on mirrors that are still mirrors and
removes the lock. Repositories converted to writable are never re-enabled.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from scripts.common.forgejo_client import ForgejoClient
from scripts.common.state import StateDB, utcnow

log = logging.getLogger(__name__)

LOCK_NAME = "failover.lock"


@dataclass
class FailoverResult:
    """Outcome of an enable/disable operation (``owner/repo`` Forgejo names)."""

    changed: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    failed: Dict[str, str] = field(default_factory=dict)


def lock_path(state_dir: Union[str, Path]) -> Path:
    """Path of the failover lock inside ``state_dir``."""
    return Path(state_dir) / LOCK_NAME


def read_lock(state_dir: Union[str, Path]) -> Optional[Dict[str, Any]]:
    """Return the failover lock contents, ``{}`` if unreadable, or ``None`` if absent."""
    path = lock_path(state_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _split(name: str) -> tuple:
    owner, sep, repo = name.partition("/")
    if not sep or not owner or not repo:
        raise ValueError(f"Expected OWNER/REPO, got {name!r}")
    return owner, repo


def disable_sync(
    forgejo_client: ForgejoClient,
    repos: Sequence[str],
    state_dir: Union[str, Path] = "/state",
    state: Optional[StateDB] = None,
    reason: str = "manual failover",
) -> FailoverResult:
    """Write the failover lock, then disable periodic sync on all given Forgejo mirrors."""
    path = lock_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"disabled_at": utcnow(), "reason": reason, "host": socket.gethostname(), "pid": os.getpid(),
               "repos": list(repos)}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.warning("Failover lock written to %s; scheduled syncs are now blocked", path)

    result = FailoverResult()
    for name in repos:
        try:
            owner, repo = _split(name)
            forgejo_client.set_mirror_enabled(owner, repo, False)
            result.changed.append(name)
            if state is not None:
                _update_state(state, owner, repo, "disabled")
        except Exception as exc:
            result.failed[name] = f"{type(exc).__name__}: {exc}"
            log.error("Could not disable sync for %s: %s", name, exc)
    log.info("Sync disabled on %d mirrors (%d failed)", len(result.changed), len(result.failed))
    return result


def enable_sync(
    forgejo_client: ForgejoClient,
    repos: Sequence[str],
    state_dir: Union[str, Path] = "/state",
    state: Optional[StateDB] = None,
    interval: str = "24h",
) -> FailoverResult:
    """Re-enable periodic sync on mirrors and remove the failover lock if all succeeded.

    Repositories marked ``writable`` in the state DB, or that are no longer
    mirrors in Forgejo, are skipped so local changes cannot be overwritten.
    """
    result = FailoverResult()
    for name in repos:
        try:
            owner, repo = _split(name)
            if state is not None and _state_status(state, owner, repo) == "writable":
                result.skipped.append(name)
                continue
            info = forgejo_client.get_repo(owner, repo)
            if info is None or not info.get("mirror"):
                log.warning("Not re-enabling %s: %s", name, "missing" if info is None else "no longer a mirror")
                result.skipped.append(name)
                continue
            forgejo_client.set_mirror_enabled(owner, repo, True, interval)
            result.changed.append(name)
            if state is not None:
                _update_state(state, owner, repo, "active")
        except Exception as exc:
            result.failed[name] = f"{type(exc).__name__}: {exc}"
            log.error("Could not enable sync for %s: %s", name, exc)
    if result.failed:
        log.error("Failover lock kept because %d mirrors could not be re-enabled", len(result.failed))
    else:
        lock_path(state_dir).unlink(missing_ok=True)
        log.info("Failover lock removed; scheduled syncs will resume")
    return result


def convert_to_writable(
    forgejo_client: ForgejoClient, owner: str, repo: str, state: Optional[StateDB] = None
) -> None:
    """Stop syncing and convert a mirror to a writable repository for local development."""
    forgejo_client.set_mirror_enabled(owner, repo, False)
    if state is not None:
        _update_state(state, owner, repo, "disabled")
    forgejo_client.convert_mirror_to_writable(owner, repo)
    if state is not None:
        _update_state(state, owner, repo, "writable")
    log.warning("%s/%s is now writable; it will never be re-synced from GitHub automatically", owner, repo)


def status(state_dir: Union[str, Path] = "/state", state: Optional[StateDB] = None) -> Dict[str, Any]:
    """Report failover state and mirror counts by status."""
    lock = read_lock(state_dir)
    counts: Dict[str, int] = {}
    if state is not None:
        for row in state.list_mirrors():
            counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"failover_active": lock is not None, "lock": lock, "mirrors_by_status": counts}


def _rows_for(state: StateDB, owner: str, repo: str) -> List[Dict[str, Any]]:
    return [r for r in state.list_mirrors() if r["forgejo_owner"] == owner and r["forgejo_repo"] == repo]


def _state_status(state: StateDB, owner: str, repo: str) -> Optional[str]:
    rows = _rows_for(state, owner, repo)
    return rows[0]["status"] if rows else None


def _update_state(state: StateDB, owner: str, repo: str, new_status: str) -> None:
    for row in _rows_for(state, owner, repo):
        state.upsert_mirror(row["repo_full_name"], owner, repo, new_status, last_error=row.get("last_error"))
