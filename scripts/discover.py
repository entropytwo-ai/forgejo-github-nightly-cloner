# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 forgejo-github-nightly-cloner contributors
"""Repository discovery.

Lists repositories for every configured GitHub account and organization,
applies filters and records results in the ``known_repos`` table.

Safety: an empty or failed discovery is *never* interpreted as "all
repositories were deleted". If repositories were previously known and the
current run fails or returns nothing (globally or for a source that used to
have repositories), :class:`DiscoveryError` is raised and nothing is changed.
"""

from __future__ import annotations

import fnmatch
import logging
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from scripts.common.config import Config
from scripts.common.github_client import GitHubClient, GitHubError, RepoInfo
from scripts.common.state import StateDB, utcnow

log = logging.getLogger(__name__)


class DiscoveryError(RuntimeError):
    """Discovery result cannot be trusted; existing mirrors must be preserved."""


@dataclass
class DiscoveryResult:
    """Outcome of a discovery run."""

    discovered: List[RepoInfo] = field(default_factory=list)
    excluded: List[RepoInfo] = field(default_factory=list)
    error: Optional[str] = None
    run_id: str = ""
    exclusion_reasons: Dict[str, str] = field(default_factory=dict)


def is_excluded_by_pattern(full_name: str, patterns: List[str]) -> Optional[str]:
    """Return the first glob pattern (case-insensitive) that matches ``owner/repo``."""
    lowered = full_name.lower()
    for pattern in patterns:
        if fnmatch.fnmatchcase(lowered, pattern.lower()):
            return pattern
    return None


def exclusion_reason(repo: RepoInfo, config: Config) -> Optional[str]:
    """Return why ``repo`` should be excluded, or ``None`` to include it."""
    gh = config.github
    pattern = is_excluded_by_pattern(repo.full_name, gh.exclude_repos)
    if pattern:
        return f"matches exclude pattern '{pattern}'"
    if repo.fork and not gh.include_forks:
        return "fork (include_forks=false)"
    if repo.archived and not gh.include_archived:
        return "archived (include_archived=false)"
    if repo.private and not gh.include_private:
        return "private (include_private=false)"
    return None


def _collect(github: GitHubClient, config: Config) -> Tuple[List[Tuple[str, List[RepoInfo]]], List[str]]:
    """Query every source; returns ``[(source, repos)]`` and error strings."""
    results: List[Tuple[str, List[RepoInfo]]] = []
    errors: List[str] = []
    sources = [("user", a) for a in config.github.accounts] + [("org", o) for o in config.github.organizations]
    for kind, name in sources:
        try:
            repos = github.list_user_repos(name) if kind == "user" else github.list_org_repos(name)
            log.info("Discovered %d repositories for %s %s", len(repos), kind, name)
            results.append((name, repos))
        except GitHubError as exc:
            message = f"{kind} '{name}': {type(exc).__name__}: {exc}"
            log.error("Discovery failed for %s", message)
            errors.append(message)
    return results, errors


def discover(config: Config, github: GitHubClient, state: StateDB, run_id: Optional[str] = None) -> DiscoveryResult:
    """Discover repositories and update ``known_repos``.

    Raises:
        DiscoveryError: when the result is untrustworthy and repositories were
            previously known (empty result, failed source, or a source that
            suddenly returns zero repositories).
    """
    run_id = run_id or f"discover-{utcnow()}-{uuid.uuid4().hex[:8]}"
    known = {r["repo_full_name"].lower() for r in state.get_known_repos()}
    sources, errors = _collect(github, config)

    all_repos: Dict[str, RepoInfo] = {}
    for _, repos in sources:
        for repo in repos:
            all_repos.setdefault(repo.full_name.lower(), repo)

    problems: List[str] = list(errors)
    if known:
        if not all_repos:
            problems.append("GitHub returned 0 repositories but previously known repositories exist")
        for source, repos in sources:
            prefix = f"{source.lower()}/"
            if not repos and any(k.startswith(prefix) for k in known):
                problems.append(f"source '{source}' returned 0 repositories but had known repositories before")
        if problems:
            message = "Discovery is untrustworthy; preserving existing mirrors: " + "; ".join(problems)
            log.critical(message)
            raise DiscoveryError(message)

    result = DiscoveryResult(run_id=run_id, error="; ".join(errors) or None)
    for key in sorted(all_repos):
        repo = all_repos[key]
        reason = exclusion_reason(repo, config)
        if reason:
            result.excluded.append(repo)
            result.exclusion_reasons[repo.full_name] = reason
            log.debug("Excluding %s: %s", repo.full_name, reason)
        else:
            result.discovered.append(repo)

    if errors:
        # First run (nothing known yet): report partial results but do not record them as authoritative.
        log.error("Discovery completed with errors; results not recorded: %s", result.error)
        return result

    now = utcnow()
    for repo in result.discovered:
        state.upsert_known_repo(repo.full_name, run_id=run_id, seen_at=now)
    state.record_discovery_run(run_id, len(result.discovered), now)

    current: Set[str] = {r.full_name.lower() for r in result.discovered}
    missing = sorted(known - current)
    if missing:
        log.warning(
            "%d previously known repositories were not discovered this run (deleted, renamed, excluded or access "
            "revoked). Their mirrors and backups are kept: %s",
            len(missing),
            ", ".join(missing[:20]) + (" ..." if len(missing) > 20 else ""),
        )
    log.info("Discovery %s: %d included, %d excluded", run_id, len(result.discovered), len(result.excluded))
    return result
