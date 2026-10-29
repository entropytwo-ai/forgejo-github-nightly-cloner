# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 forgejo-github-nightly-cloner contributors
"""Mirror provisioning: ensure each discovered GitHub repository has a Forgejo pull mirror.

The operation is idempotent: existing mirrors are skipped, existing
non-mirror repositories are never overwritten, and one repository's failure
does not stop the others.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from scripts.common.config import Config
from scripts.common.forgejo_client import ForgejoClient, ForgejoConflictError, ForgejoError
from scripts.common.github_client import RepoInfo
from scripts.common.state import StateDB, utcnow

log = logging.getLogger(__name__)


@dataclass
class MirrorResult:
    """Outcome of a provisioning run (keys are GitHub ``owner/repo`` names)."""

    created: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    failed: Dict[str, str] = field(default_factory=dict)


def forgejo_repo_name(repo: RepoInfo, template: str) -> str:
    """Render the Forgejo repository name for ``repo`` (e.g. ``{owner}-{repo}``)."""
    return template.format(owner=repo.owner, repo=repo.name)


def _normalize_url(url: Optional[str]) -> str:
    url = (url or "").strip().lower().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if "@" in url.split("//", 1)[-1]:  # strip any embedded credentials
        scheme, rest = url.split("//", 1) if "//" in url else ("", url)
        url = f"{scheme}//{rest.split('@', 1)[1]}"
    return url


def provision_mirrors(
    config: Config,
    forgejo: ForgejoClient,
    state: StateDB,
    repos: Iterable[RepoInfo],
    dry_run: bool = False,
) -> MirrorResult:
    """Create missing mirrors for ``repos`` in ``config.forgejo.owner``."""
    result = MirrorResult()
    owner = config.forgejo.owner
    claimed: Dict[str, str] = {}

    for repo in repos:
        name = forgejo_repo_name(repo, config.forgejo.repo_name_template)
        try:
            other = claimed.get(name.lower())
            if other and other != repo.full_name:
                raise ForgejoError(
                    f"name collision: '{name}' is also used by {other}; set forgejo.repo_name_template to e.g. '{{owner}}-{{repo}}'"
                )
            claimed[name.lower()] = repo.full_name
            existing_state = state.get_mirror(repo.full_name)
            if existing_state and existing_state["status"] == "writable":
                log.info("Skipping %s: converted to writable during failover", repo.full_name)
                result.skipped.append(repo.full_name)
                continue

            if dry_run:
                existing = forgejo.get_repo(owner, name)
                if existing is None:
                    log.info("[dry-run] Would create mirror %s/%s from %s", owner, name, repo.clone_url)
                    result.created.append(repo.full_name)
                else:
                    log.info("[dry-run] %s/%s already exists; would skip", owner, name)
                    result.skipped.append(repo.full_name)
                continue

            existing = forgejo.get_repo(owner, name)
            if existing is not None:
                if not existing.get("mirror"):
                    log.warning("%s/%s exists but is NOT a mirror; leaving it untouched", owner, name)
                    state.upsert_mirror(repo.full_name, owner, name, "skipped", last_error="exists as non-mirror repository")
                    result.skipped.append(repo.full_name)
                    continue
                original = existing.get("original_url")
                if original and _normalize_url(original) != _normalize_url(repo.clone_url):
                    raise ForgejoError(f"name collision: {owner}/{name} mirrors {original}, not {repo.clone_url}")
                log.debug("Mirror %s/%s already exists; skipping", owner, name)
                status = "disabled" if existing_state and existing_state["status"] == "disabled" else "active"
                state.upsert_mirror(repo.full_name, owner, name, status, last_synced=existing.get("mirror_updated"))
                result.skipped.append(repo.full_name)
                continue

            log.info("Creating mirror %s/%s from %s", owner, name, repo.full_name)
            try:
                forgejo.create_mirror(
                    owner=owner,
                    repo_name=name,
                    clone_url=repo.clone_url,
                    github_token=config.github_token or None,
                    private=repo.private or config.forgejo.force_private,
                    lfs=config.forgejo.lfs,
                    mirror_interval=config.forgejo.mirror_interval,
                    description=repo.description,
                )
            except ForgejoConflictError:
                # Created concurrently or by a previous interrupted run: treat as existing.
                log.info("Mirror %s/%s already exists (409); skipping", owner, name)
                state.upsert_mirror(repo.full_name, owner, name, "active")
                result.skipped.append(repo.full_name)
                continue
            state.upsert_mirror(repo.full_name, owner, name, "active", last_synced=utcnow())
            result.created.append(repo.full_name)
        except Exception as exc:  # per-repo isolation: record and continue
            message = f"{type(exc).__name__}: {exc}"
            log.error("Mirror provisioning failed for %s: %s", repo.full_name, message)
            if not dry_run:
                try:
                    state.upsert_mirror(repo.full_name, owner, name, "error", last_error=message[:1000])
                except Exception:  # pragma: no cover - state DB failure must not mask original error
                    log.exception("Could not record mirror error for %s", repo.full_name)
            result.failed[repo.full_name] = message

    log.info(
        "Mirror provisioning%s: %d created, %d skipped, %d failed",
        " (dry-run)" if dry_run else "",
        len(result.created),
        len(result.skipped),
        len(result.failed),
    )
    return result
