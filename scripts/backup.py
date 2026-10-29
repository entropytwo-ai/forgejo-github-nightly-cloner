# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 forgejo-github-nightly-cloner contributors
"""Nightly metadata backup with a staging -> validate -> promote strategy.

1. Everything is written to ``{backup_root}/{date}-staging/``.
2. A manifest with per-type counts and SHA-256 checksums is generated.
3. The staging tree is verified (:func:`scripts.verify.verify_backup`).
4. Only if verification passes and the failure budget is respected is the
   staging directory atomically renamed to ``{backup_root}/{date}/``.
5. Retention runs only after a successful promotion and always keeps at
   least ``min_keep`` (>= 2) backups.

A failed run leaves the staging directory for inspection and never touches
existing backups.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from scripts.common.config import Config
from scripts.common.github_client import AuthError, ForbiddenError, GitHubClient, GitHubError, NotFoundError
from scripts.common.state import StateDB
from scripts.verify import compute_checksums, verify_backup, write_checksums_file

log = logging.getLogger(__name__)

BACKUP_DIR_RE = re.compile(r"^\d{8}$")
STAGING_DIR_RE = re.compile(r"^(\d{8})-staging$")

UNSUPPORTED_TYPES = ["discussions", "projects", "code_scanning", "packages", "environments"]
COVERAGE_NOTES = "GitHub Actions workflows are preserved as git data but require adaptation for Forgejo runner"


@dataclass
class BackupResult:
    """Outcome of a backup run."""

    success: bool
    backup_date: str
    final_path: Optional[Path] = None
    staging_path: Optional[Path] = None
    manifest: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)


def _comments(github: GitHubClient, owner: str, repo: str) -> List[Dict[str, Any]]:
    issue = [dict(c, comment_type="issue") for c in github.list_issue_comments(owner, repo)]
    review = [dict(c, comment_type="review") for c in github.list_pr_comments(owner, repo)]
    return issue + review


FETCHERS: Dict[str, Callable[[GitHubClient, str, str], List[Any]]] = {
    "issues": lambda g, o, r: g.list_issues(o, r),
    "pull_requests": lambda g, o, r: g.list_pull_requests(o, r),
    "labels": lambda g, o, r: g.list_labels(o, r),
    "milestones": lambda g, o, r: g.list_milestones(o, r),
    "releases": lambda g, o, r: g.list_releases(o, r),
    "comments": _comments,
    "topics": lambda g, o, r: g.get_topics(o, r),
    "collaborators": lambda g, o, r: g.list_collaborators(o, r),
}


def validate_backup_date(value: str) -> str:
    """Ensure ``value`` is a real ``YYYYMMDD`` date."""
    try:
        datetime.strptime(value, "%Y%m%d")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"backup date must be YYYYMMDD, got {value!r}") from exc
    return value


def today_str() -> str:
    """Today's date (UTC) as ``YYYYMMDD``."""
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def backup_repository(
    github: GitHubClient, full_name: str, metadata_types: Sequence[str], staging: Path
) -> Dict[str, Any]:
    """Export all metadata types of one repository into ``staging/owner/repo``.

    Returns the manifest entry ``{"status", "items", "error", "inaccessible_types"}``.
    401/403/404 mark a type inaccessible; other API errors fail the repository.
    """
    owner, _, repo = full_name.partition("/")
    items: Dict[str, Optional[int]] = {}
    inaccessible: Dict[str, str] = {}
    repo_dir = staging / owner / repo
    for item_type in metadata_types:
        try:
            data = FETCHERS[item_type](github, owner, repo)
        except (AuthError, ForbiddenError, NotFoundError) as exc:
            log.warning("%s: %s inaccessible (%s)", full_name, item_type, type(exc).__name__)
            items[item_type] = None
            inaccessible[item_type] = f"{type(exc).__name__}: {exc}"
            continue
        except GitHubError as exc:
            log.error("%s: failed to export %s: %s", full_name, item_type, exc)
            return {"status": "failed", "items": items, "error": f"{item_type}: {type(exc).__name__}: {exc}",
                    "inaccessible_types": inaccessible}
        _write_json(repo_dir / f"{item_type}.json", data)
        items[item_type] = len(data)
        log.debug("%s: exported %d %s", full_name, len(data), item_type)

    if metadata_types and len(inaccessible) == len(metadata_types):
        return {"status": "inaccessible", "items": items, "error": "; ".join(sorted(set(inaccessible.values()))),
                "inaccessible_types": inaccessible}
    return {"status": "succeeded", "items": items, "error": None, "inaccessible_types": inaccessible}


def build_manifest(backup_date: str, metadata_types: Sequence[str], repositories: Dict[str, Dict[str, Any]],
                   checksums: Dict[str, str]) -> Dict[str, Any]:
    """Assemble the manifest document."""
    statuses = [r["status"] for r in repositories.values()]
    return {
        "backup_date": backup_date,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": len(repositories),
            "succeeded": statuses.count("succeeded"),
            "failed": statuses.count("failed"),
            "inaccessible": statuses.count("inaccessible"),
            "skipped": statuses.count("skipped"),
        },
        "coverage": {"captured": list(metadata_types), "unsupported": list(UNSUPPORTED_TYPES), "notes": COVERAGE_NOTES},
        "repositories": repositories,
        "checksums": checksums,
    }


def promote(staging: Path, final: Path) -> None:
    """Atomically move ``staging`` to ``final``; an existing ``final`` is swapped out, then removed."""
    if not final.exists():
        os.rename(staging, final)
        return
    displaced = final.with_name(f"{final.name}.replaced-{datetime.now(timezone.utc).strftime('%H%M%S%f')}")
    os.rename(final, displaced)
    try:
        os.rename(staging, final)
    except OSError:
        os.rename(displaced, final)  # roll back so the previous backup stays in place
        raise
    shutil.rmtree(displaced, ignore_errors=True)


def list_backups(backup_root: Path) -> List[str]:
    """Completed backup directory names (``YYYYMMDD``), newest first."""
    if not backup_root.is_dir():
        return []
    return sorted((p.name for p in backup_root.iterdir() if p.is_dir() and BACKUP_DIR_RE.match(p.name)), reverse=True)


def apply_retention(backup_root: Path, retention_days: int, min_keep: int = 2,
                    today: Optional[date] = None, protect: Sequence[str] = ()) -> List[str]:
    """Delete backups older than ``retention_days`` while keeping the newest ``max(min_keep, 2)``.

    Stale ``-staging`` directories older than the retention window are removed too.
    Returns the names of deleted directories.
    """
    today = today or datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=retention_days)
    keep = max(2, min_keep)
    deleted: List[str] = []
    backups = list_backups(backup_root)
    for name in backups[keep:]:
        if name in protect:
            continue
        if datetime.strptime(name, "%Y%m%d").date() < cutoff:
            shutil.rmtree(backup_root / name)
            deleted.append(name)
            log.info("Retention: deleted backup %s (older than %d days)", name, retention_days)
    for path in backup_root.iterdir() if backup_root.is_dir() else []:
        match = STAGING_DIR_RE.match(path.name)
        if match and path.is_dir() and datetime.strptime(match.group(1), "%Y%m%d").date() < cutoff:
            shutil.rmtree(path)
            deleted.append(path.name)
            log.info("Retention: deleted stale staging directory %s", path.name)
    return deleted


def run_backup(
    config: Config,
    github: Optional[GitHubClient],
    state: StateDB,
    repos: Sequence[str],
    backup_date: Optional[str] = None,
    dry_run: bool = False,
    backup_root: Optional[Path] = None,
    today: Optional[date] = None,
) -> BackupResult:
    """Back up metadata of ``repos`` (``owner/repo`` names) for ``backup_date``."""
    backup_date = validate_backup_date(backup_date or today_str())
    root = Path(backup_root or config.backup_dir)
    staging = root / f"{backup_date}-staging"
    final = root / backup_date
    types = list(config.backup.metadata_types)
    repos = sorted(dict.fromkeys(repos))
    result = BackupResult(success=False, backup_date=backup_date, staging_path=staging)

    if dry_run:
        for name in repos:
            log.info("[dry-run] Would back up %s: %s -> %s", name, ", ".join(types), final / name)
        log.info("[dry-run] Would write %d repositories to %s via %s", len(repos), final, staging)
        result.success = True
        return result
    if github is None:
        raise ValueError("A GitHub client is required for a real backup")

    if staging.exists():
        log.warning("Removing leftover staging directory %s from an earlier failed run", staging)
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    repositories: Dict[str, Dict[str, Any]] = {}
    for index, name in enumerate(repos, 1):
        log.info("Backing up %s (%d/%d)", name, index, len(repos))
        state.upsert_backup(name, backup_date, "pending", str(final / name))
        try:
            entry = backup_repository(github, name, types, staging)
        except Exception as exc:  # unexpected (e.g. disk full) - isolate per repo
            log.exception("Unexpected error backing up %s", name)
            entry = {"status": "failed", "items": {}, "error": f"{type(exc).__name__}: {exc}", "inaccessible_types": {}}
        repositories[name] = entry
        db_status = {"succeeded": "complete", "failed": "failed", "inaccessible": "inaccessible"}.get(entry["status"], "failed")
        state.upsert_backup(name, backup_date, db_status, str(final / name), entry.get("error"), entry.get("items"))

    manifest = build_manifest(backup_date, types, repositories, compute_checksums(staging))
    _write_json(staging / "manifest.json", manifest)
    write_checksums_file(staging, manifest["checksums"])
    result.manifest = manifest

    summary = manifest["summary"]
    verification = verify_backup(staging, write_missing_checksums=False)
    result.errors.extend(verification.errors)
    if summary["failed"] > config.backup.max_failed_repos:
        result.errors.append(
            f"{summary['failed']} repositories failed (allowed: {config.backup.max_failed_repos})"
        )
    if summary["total"] > 0 and summary["succeeded"] == 0:
        result.errors.append("no repository was backed up successfully (token or network problem?)")

    if result.errors:
        log.error("Backup %s FAILED; staging kept at %s; existing backups untouched: %s",
                  backup_date, staging, "; ".join(result.errors))
        for name, entry in repositories.items():
            if entry["status"] == "succeeded":
                state.upsert_backup(name, backup_date, "failed", str(staging / name), "backup run not promoted", entry["items"])
        return result

    promote(staging, final)
    result.success = True
    result.final_path = final
    result.staging_path = None
    log.info("Backup %s promoted to %s (%s)", backup_date, final, json.dumps(summary))

    result.deleted = apply_retention(root, config.backup.retention_days, config.backup.min_keep,
                                     today=today, protect=[backup_date])
    return result
