# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 forgejo-github-nightly-cloner contributors
"""File-based execution lock with stale-lock detection.

The lock file ``{lock_dir}/forgejo-sync-{name}.lock`` contains the owner PID
and acquisition time. It is created atomically with ``O_CREAT | O_EXCL``. An
existing lock is considered stale (and is replaced) when its PID no longer
exists, or when it is unreadable and older than ``timeout_minutes``. A lock
held by a *live* process is never stolen; if it is older than the timeout a
"possibly hung" error is raised so an operator can investigate.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import time
from pathlib import Path
from types import TracebackType
from typing import Optional, Type, Union

log = logging.getLogger(__name__)


class LockError(RuntimeError):
    """Raised when the lock cannot be acquired."""


def pid_running(pid: int) -> bool:
    """Return True if a process with ``pid`` exists."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


class AcquireLock:
    """Context manager guaranteeing that only one job named ``name`` runs at a time."""

    def __init__(self, name: str, timeout_minutes: int = 120, lock_dir: Union[str, Path] = "/tmp") -> None:
        self.name = name
        self.timeout_seconds = max(1, int(timeout_minutes)) * 60
        self.path = Path(lock_dir) / f"forgejo-sync-{name}.lock"
        self.acquired = False

    def _read(self) -> Optional[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _try_create(self) -> bool:
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "acquired_at": time.time(), "name": self.name}, handle)
        return True

    def acquire(self) -> "AcquireLock":
        """Acquire the lock or raise :class:`LockError`."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            if self._try_create():
                self.acquired = True
                log.debug("Acquired lock %s", self.path)
                return self
            info = self._read()
            try:
                age = time.time() - self.path.stat().st_mtime
            except FileNotFoundError:
                continue  # released in the meantime; retry
            if info is None:
                if age < self.timeout_seconds:
                    raise LockError(f"Lock {self.path} exists but is unreadable; retry later")
                log.warning("Removing unreadable lock %s older than timeout", self.path)
            else:
                pid = int(info.get("pid") or 0)
                if pid_running(pid):
                    if age > self.timeout_seconds:
                        raise LockError(
                            f"Another '{self.name}' job (PID {pid}) has held the lock for {age / 60:.0f} minutes "
                            f"(timeout {self.timeout_seconds // 60}); it may be hung. Investigate before removing {self.path}."
                        )
                    raise LockError(f"Another '{self.name}' job is already running (PID {pid}); lock: {self.path}")
                log.warning("Removing stale lock %s (PID %s not running)", self.path, pid)
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        raise LockError(f"Could not acquire lock {self.path}")

    def release(self) -> None:
        """Release the lock if this instance owns it."""
        if not self.acquired:
            return
        info = self._read()
        if info is None or int(info.get("pid") or 0) == os.getpid():
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.acquired = False

    def __enter__(self) -> "AcquireLock":
        return self.acquire()

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.release()
