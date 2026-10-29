# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 forgejo-github-nightly-cloner contributors
"""SQLite state database (default ``/state/state.db``) for resumable, auditable runs."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

SCHEMA = """
CREATE TABLE IF NOT EXISTS mirrors (
    repo_full_name TEXT PRIMARY KEY,
    forgejo_owner  TEXT NOT NULL,
    forgejo_repo   TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('active', 'disabled', 'error', 'writable', 'skipped')),
    created_at     TEXT NOT NULL,
    last_synced    TEXT,
    last_error     TEXT
);
CREATE TABLE IF NOT EXISTS backups (
    repo_full_name  TEXT NOT NULL,
    backup_date     TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('pending', 'complete', 'failed', 'inaccessible')),
    backup_path     TEXT,
    last_error      TEXT,
    items_backed_up TEXT,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (repo_full_name, backup_date)
);
CREATE TABLE IF NOT EXISTS known_repos (
    repo_full_name     TEXT PRIMARY KEY,
    first_seen         TEXT NOT NULL,
    last_seen          TEXT NOT NULL,
    last_discovery_run TEXT
);
CREATE TABLE IF NOT EXISTS discovery_runs (
    run_id           TEXT PRIMARY KEY,
    discovered_count INTEGER NOT NULL,
    timestamp        TEXT NOT NULL
);
"""

_UNSET = object()


def utcnow() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class StateDB:
    """Small repository-style wrapper around the SQLite state database."""

    def __init__(self, path: Union[str, Path] = "/state/state.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        """Close the connection."""
        self._conn.close()

    def __enter__(self) -> "StateDB":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cursor

    def _query(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    # -------------------------------------------------------------- mirrors
    def upsert_mirror(
        self,
        repo_full_name: str,
        forgejo_owner: str,
        forgejo_repo: str,
        status: str,
        last_synced: Optional[str] = None,
        last_error: Any = _UNSET,
    ) -> None:
        """Insert or update a mirror row. ``last_error`` is cleared when status is active unless given."""
        existing = self.get_mirror(repo_full_name)
        if last_error is _UNSET:
            last_error = None if status == "active" else (existing or {}).get("last_error")
        if existing is None:
            self._execute(
                "INSERT INTO mirrors (repo_full_name, forgejo_owner, forgejo_repo, status, created_at, last_synced, last_error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (repo_full_name, forgejo_owner, forgejo_repo, status, utcnow(), last_synced, last_error),
            )
        else:
            self._execute(
                "UPDATE mirrors SET forgejo_owner=?, forgejo_repo=?, status=?, last_synced=COALESCE(?, last_synced),"
                " last_error=? WHERE repo_full_name=?",
                (forgejo_owner, forgejo_repo, status, last_synced, last_error, repo_full_name),
            )

    def get_mirror(self, repo_full_name: str) -> Optional[Dict[str, Any]]:
        """Return a mirror row or ``None``."""
        rows = self._query("SELECT * FROM mirrors WHERE repo_full_name=?", (repo_full_name,))
        return rows[0] if rows else None

    def list_mirrors(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """List mirrors, optionally filtered by status."""
        if status:
            return self._query("SELECT * FROM mirrors WHERE status=? ORDER BY repo_full_name", (status,))
        return self._query("SELECT * FROM mirrors ORDER BY repo_full_name")

    # -------------------------------------------------------------- backups
    def upsert_backup(
        self,
        repo_full_name: str,
        backup_date: str,
        status: str,
        backup_path: Optional[str] = None,
        last_error: Optional[str] = None,
        items_backed_up: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Insert or replace the backup record for ``(repo, date)``."""
        self._execute(
            "INSERT INTO backups (repo_full_name, backup_date, status, backup_path, last_error, items_backed_up, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(repo_full_name, backup_date) DO UPDATE SET status=excluded.status,"
            " backup_path=excluded.backup_path, last_error=excluded.last_error,"
            " items_backed_up=excluded.items_backed_up, updated_at=excluded.updated_at",
            (
                repo_full_name,
                backup_date,
                status,
                backup_path,
                last_error,
                json.dumps(items_backed_up or {}, sort_keys=True),
                utcnow(),
            ),
        )

    @staticmethod
    def _decode_backup(row: Dict[str, Any]) -> Dict[str, Any]:
        row["items_backed_up"] = json.loads(row.get("items_backed_up") or "{}")
        return row

    def get_backup(self, repo_full_name: str, backup_date: str) -> Optional[Dict[str, Any]]:
        """Return the backup record for ``(repo, date)`` or ``None``."""
        rows = self._query(
            "SELECT * FROM backups WHERE repo_full_name=? AND backup_date=?", (repo_full_name, backup_date)
        )
        return self._decode_backup(rows[0]) if rows else None

    def list_backups(self, backup_date: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """List backup records filtered by date and/or status."""
        clauses, params = [], []
        if backup_date:
            clauses.append("backup_date=?")
            params.append(backup_date)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._query(f"SELECT * FROM backups{where} ORDER BY backup_date DESC, repo_full_name", params)
        return [self._decode_backup(r) for r in rows]

    # ---------------------------------------------------------- known repos
    def upsert_known_repo(self, repo_full_name: str, run_id: Optional[str] = None, seen_at: Optional[str] = None) -> None:
        """Record that a repository was seen during discovery."""
        now = seen_at or utcnow()
        self._execute(
            "INSERT INTO known_repos (repo_full_name, first_seen, last_seen, last_discovery_run) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(repo_full_name) DO UPDATE SET last_seen=excluded.last_seen,"
            " last_discovery_run=excluded.last_discovery_run",
            (repo_full_name, now, now, run_id),
        )

    def get_known_repos(self) -> List[Dict[str, Any]]:
        """Return all repositories ever discovered."""
        return self._query("SELECT * FROM known_repos ORDER BY repo_full_name")

    def record_discovery_run(self, run_id: str, discovered_count: int, timestamp: Optional[str] = None) -> None:
        """Persist a summary row for a discovery run."""
        self._execute(
            "INSERT OR REPLACE INTO discovery_runs (run_id, discovered_count, timestamp) VALUES (?, ?, ?)",
            (run_id, int(discovered_count), timestamp or utcnow()),
        )

    def last_discovery_run(self) -> Optional[Dict[str, Any]]:
        """Return the most recent discovery run or ``None``."""
        rows = self._query("SELECT * FROM discovery_runs ORDER BY timestamp DESC LIMIT 1")
        return rows[0] if rows else None
