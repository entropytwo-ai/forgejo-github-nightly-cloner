# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 forgejo-github-nightly-cloner contributors
"""Command line entry point.

Usage::

    python -m scripts.run [--config PATH] [--dry-run] [--log-level LEVEL]
                          {sync,backup,discover,verify,failover,status} [args...]

Exit codes: 0 success, 1 failure, 2 refused (lock held / failover active),
3 discovery safety stop, 4 configuration error.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

if __package__ in (None, ""):  # allow `python scripts/run.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.backup import list_backups, run_backup, today_str, validate_backup_date  # noqa: E402
from scripts.common.config import Config, ConfigError, load_config, require_secrets  # noqa: E402
from scripts.common.forgejo_client import ForgejoClient  # noqa: E402
from scripts.common.github_client import GitHubClient  # noqa: E402
from scripts.common.lock import AcquireLock, LockError  # noqa: E402
from scripts.common.logging_utils import setup_logging  # noqa: E402
from scripts.common.state import StateDB  # noqa: E402
from scripts.discover import DiscoveryError, discover  # noqa: E402
from scripts.failover import convert_to_writable, disable_sync, enable_sync, status as failover_status  # noqa: E402
from scripts.mirror import provision_mirrors  # noqa: E402
from scripts.verify import verify_backup  # noqa: E402

log = logging.getLogger("scripts.run")

EXIT_OK, EXIT_FAIL, EXIT_REFUSED, EXIT_DISCOVERY, EXIT_CONFIG = 0, 1, 2, 3, 4


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(prog="run.py", description="Forgejo standby mirror & GitHub metadata backup")
    parser.add_argument("--config", help="Config file (default: $CONFIG_FILE or /config/config.yml)")
    parser.add_argument("--dry-run", action="store_true", help="Log planned actions without changing anything")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Override LOG_LEVEL")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync", help="Discovery + mirror provisioning + metadata backup + verification")
    sub.add_parser("discover", help="Discovery only")
    p_backup = sub.add_parser("backup", help="Metadata backup only")
    p_backup.add_argument("--date", help="Backup date YYYYMMDD (default: today, UTC)")
    p_verify = sub.add_parser("verify", help="Verify a backup")
    p_verify.add_argument("--date", help="Backup date YYYYMMDD (default: latest)")
    p_fail = sub.add_parser("failover", help="Failover operations")
    p_fail.add_argument("action", choices=["disable", "enable", "status", "convert-repo"])
    p_fail.add_argument("repo", nargs="?", help="Forgejo OWNER/REPO (for convert-repo)")
    sub.add_parser("status", help="Mirror, backup and failover summary")
    return parser


def _github(config: Config) -> GitHubClient:
    return GitHubClient(
        config.github_token,
        base_url=config.github.api_url,
        max_retries=config.sync.max_retries,
        retry_delay=config.sync.retry_delay_seconds,
        rate_limit_buffer=config.sync.rate_limit_buffer,
    )


def _forgejo(config: Config) -> ForgejoClient:
    return ForgejoClient(config.forgejo_url, config.forgejo_token, max_retries=config.sync.max_retries)


def _lock(config: Config) -> AcquireLock:
    return AcquireLock("sync", config.sync.lock_timeout_minutes, config.lock_dir)


def _backup_targets(state: StateDB) -> List[str]:
    names = [m["repo_full_name"] for m in state.list_mirrors() if m["status"] != "error"]
    return names or [r["repo_full_name"] for r in state.get_known_repos()]


def _mirror_names(state: StateDB) -> List[str]:
    return sorted({f"{m['forgejo_owner']}/{m['forgejo_repo']}" for m in state.list_mirrors()
                   if m["status"] in ("active", "disabled")})


def cmd_sync(config: Config, state: StateDB, dry_run: bool) -> int:
    """Full cycle; refuses to run while failover is active."""
    if config.failover_lock_path.exists():
        log.error("Failover lock %s present: sync REFUSED. Run 'failover enable' to resume.", config.failover_lock_path)
        return EXIT_REFUSED
    require_secrets(config)
    with _lock(config):
        github, forgejo = _github(config), _forgejo(config)
        try:
            found = discover(config, github, state)
        except DiscoveryError as exc:
            log.critical("Sync aborted, nothing changed: %s", exc)
            return EXIT_DISCOVERY
        if found.error:
            log.error("Discovery had errors; continuing with %d repositories found", len(found.discovered))
        mirrors = provision_mirrors(config, forgejo, state, found.discovered, dry_run=dry_run)
        result = run_backup(config, github, state, [r.full_name for r in found.discovered], today_str(), dry_run=dry_run)
        ok = result.success and not mirrors.failed and not found.error
        if result.success and result.final_path:
            check = verify_backup(result.final_path)
            ok = ok and check.ok
        log.info("Sync finished: %s (mirrors created=%d skipped=%d failed=%d; backup=%s)", "OK" if ok else "WITH ERRORS",
                 len(mirrors.created), len(mirrors.skipped), len(mirrors.failed), "ok" if result.success else "failed")
        return EXIT_OK if ok else EXIT_FAIL


def cmd_discover(config: Config, state: StateDB) -> int:
    """Discovery only (updates known_repos)."""
    require_secrets(config, forgejo=False)
    with _lock(config):
        try:
            found = discover(config, _github(config), state)
        except DiscoveryError as exc:
            log.critical("%s", exc)
            return EXIT_DISCOVERY
    for repo in found.discovered:
        print(f"include  {repo.full_name}{'  (private)' if repo.private else ''}")
    for repo in found.excluded:
        print(f"exclude  {repo.full_name}  ({found.exclusion_reasons.get(repo.full_name, '')})")
    return EXIT_FAIL if found.error else EXIT_OK


def cmd_backup(config: Config, state: StateDB, date: Optional[str], dry_run: bool) -> int:
    """Metadata backup of all tracked repositories."""
    require_secrets(config, forgejo=False)
    targets = _backup_targets(state)
    if not targets:
        log.error("No repositories known yet; run 'discover' or 'sync' first")
        return EXIT_FAIL
    with _lock(config):
        result = run_backup(config, None if dry_run else _github(config), state, targets, date or today_str(), dry_run=dry_run)
    return EXIT_OK if result.success else EXIT_FAIL


def cmd_verify(config: Config, date: Optional[str]) -> int:
    """Verify a backup (latest by default)."""
    if date:
        validate_backup_date(date)
    else:
        available = list_backups(config.backup_dir)
        if not available:
            log.error("No backups found in %s", config.backup_dir)
            return EXIT_FAIL
        date = available[0]
    result = verify_backup(config.backup_dir / date)
    print(f"Backup {date}: {'OK' if result.ok else 'FAILED'}")
    for err in result.errors:
        print(f"  ERROR   {err}")
    for warn in result.warnings:
        print(f"  WARNING {warn}")
    return EXIT_OK if result.ok else EXIT_FAIL


def cmd_failover(config: Config, state: StateDB, action: str, repo: Optional[str], dry_run: bool) -> int:
    """Failover operations."""
    if action == "status":
        print(json.dumps(failover_status(config.state_dir, state), indent=2))
        return EXIT_OK
    require_secrets(config, github=False)
    forgejo = _forgejo(config)
    if action == "convert-repo":
        if not repo or "/" not in repo:
            log.error("convert-repo requires OWNER/REPO (Forgejo name)")
            return EXIT_FAIL
        owner, name = repo.split("/", 1)
        if dry_run:
            log.info("[dry-run] Would disable sync and convert %s to writable", repo)
            return EXIT_OK
        convert_to_writable(forgejo, owner, name, state)
        return EXIT_OK
    repos = _mirror_names(state) or [f"{config.forgejo.owner}/{r['name']}" for r in forgejo.list_repos(config.forgejo.owner) if r.get("mirror")]
    if dry_run:
        log.info("[dry-run] Would %s sync on %d mirrors: %s", action, len(repos), ", ".join(repos))
        return EXIT_OK
    if action == "disable":
        result = disable_sync(forgejo, repos, config.state_dir, state)
    else:
        result = enable_sync(forgejo, repos, config.state_dir, state, config.forgejo.mirror_interval)
    print(f"{action}: changed={len(result.changed)} skipped={len(result.skipped)} failed={len(result.failed)}")
    for name, err in result.failed.items():
        print(f"  FAILED {name}: {err}")
    return EXIT_FAIL if result.failed else EXIT_OK


def cmd_status(config: Config, state: StateDB) -> int:
    """Print a summary of mirrors, backups and failover state."""
    fo = failover_status(config.state_dir, state)
    print(f"Failover active : {fo['failover_active']}")
    print(f"Mirrors         : {json.dumps(fo['mirrors_by_status'])}")
    last = state.last_discovery_run()
    print(f"Last discovery  : {last['timestamp'] + ' (' + str(last['discovered_count']) + ' repos)' if last else 'never'}")
    print(f"Known repos     : {len(state.get_known_repos())}")
    backups = list_backups(config.backup_dir)
    print(f"Backups ({len(backups)})    : {', '.join(backups[:10]) or 'none'}")
    for date in backups[:3]:
        manifest = config.backup_dir / date / "manifest.json"
        try:
            summary = json.loads(manifest.read_text(encoding="utf-8")).get("summary", {})
        except (OSError, ValueError):
            summary = {"error": "manifest unreadable"}
        print(f"  {date}: {json.dumps(summary)}")
    staging = sorted(p.name for p in config.backup_dir.glob("*-staging")) if config.backup_dir.is_dir() else []
    if staging:
        print(f"Failed/in-progress staging dirs: {', '.join(staging)}")
    errors = state.list_mirrors("error")
    for row in errors[:20]:
        print(f"  mirror error {row['repo_full_name']}: {row['last_error']}")
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse arguments, load config, init logging and state, dispatch."""
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        setup_logging(args.log_level or os.environ.get("LOG_LEVEL", "INFO"))
        log.error("Configuration error: %s", exc)
        return EXIT_CONFIG
    if args.log_level:
        config.log_level = args.log_level
    dry_run = bool(args.dry_run or config.dry_run)
    setup_logging(config.log_level, config.log_format, config.secrets())
    if dry_run:
        log.info("DRY-RUN mode: no changes will be made")
    try:
        with StateDB(config.state_db_path) as state:
            if args.command == "sync":
                return cmd_sync(config, state, dry_run)
            if args.command == "discover":
                return cmd_discover(config, state)
            if args.command == "backup":
                return cmd_backup(config, state, args.date, dry_run)
            if args.command == "verify":
                return cmd_verify(config, args.date)
            if args.command == "failover":
                return cmd_failover(config, state, args.action, args.repo, dry_run)
            return cmd_status(config, state)
    except LockError as exc:
        log.error("Refusing to run: %s", exc)
        return EXIT_REFUSED
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return EXIT_CONFIG
    except Exception:
        log.exception("Unhandled error")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
