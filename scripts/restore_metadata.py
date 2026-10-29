# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 forgejo-github-nightly-cloner contributors
"""Import backed-up GitHub metadata into a Forgejo repository.

Restores labels, milestones, issues (with their conversation comments) and
releases from ``{backup}/{owner}/{repo}/*.json`` into a target Forgejo repo.
Pull requests are imported as issues labelled ``imported-pull-request``
because Forgejo cannot recreate a PR without its source branch.

Idempotent: labels/milestones are matched by name/title, issues by a hidden
``<!-- github-import: owner/repo#N -->`` marker, releases by tag.

Usage::

    python -m scripts.restore_metadata --backup /backups/20261028 \\
        --source octo-org/app --target backup-user/app [--apply]

Without ``--apply`` the script only prints what it would do (dry-run).
Original authors and timestamps cannot be set through the API; they are
recorded in the body of each imported item.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.common.forgejo_client import ForgejoClient, ForgejoError  # noqa: E402
from scripts.common.logging_utils import setup_logging  # noqa: E402

log = logging.getLogger("scripts.restore_metadata")
PR_LABEL = "imported-pull-request"


def _load(repo_dir: Path, item_type: str) -> List[Dict[str, Any]]:
    path = repo_dir / f"{item_type}.json"
    if not path.is_file():
        log.info("No %s.json in backup; skipping", item_type)
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def _marker(source: str, number: Any) -> str:
    return f"<!-- github-import: {source}#{number} -->"


def _provenance(item: Dict[str, Any], kind: str) -> str:
    user = (item.get("user") or {}).get("login", "unknown")
    return f"> Imported from GitHub {kind} [{item.get('html_url', '')}]({item.get('html_url', '')}) by @{user} on {item.get('created_at', '?')}\n\n"


class Restorer:
    """Performs (or plans) the import into ``target_owner/target_repo``."""

    def __init__(self, client: ForgejoClient, source: str, target: str, apply: bool) -> None:
        self.client = client
        self.source = source
        self.owner, _, self.repo = target.partition("/")
        self.apply = apply
        self.stats: Dict[str, int] = {}

    def _bump(self, key: str) -> None:
        self.stats[key] = self.stats.get(key, 0) + 1

    def _list(self, path: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page = 1
        while True:
            batch = self.client.request("GET", path, params={"page": page, "limit": 50}) or []
            items.extend(batch)
            if len(batch) < 50:
                return items
            page += 1

    def _post(self, path: str, body: Dict[str, Any], what: str) -> Optional[Dict[str, Any]]:
        if not self.apply:
            log.info("[dry-run] would create %s", what)
            self._bump(f"would_create_{path.rsplit('/', 1)[-1]}")
            return None
        created = self.client.request("POST", path, json_body=body)
        self._bump(f"created_{path.rsplit('/', 1)[-1]}")
        return created

    def labels(self, labels: Sequence[Dict[str, Any]]) -> Dict[str, int]:
        """Create missing labels; returns name -> Forgejo label id."""
        base = f"/repos/{self.owner}/{self.repo}/labels"
        existing = {l["name"]: l["id"] for l in self._list(base)}
        wanted = list(labels) + [{"name": PR_LABEL, "color": "6f42c1", "description": "Pull request imported from GitHub"}]
        for label in wanted:
            if label["name"] in existing:
                continue
            created = self._post(base, {"name": label["name"], "color": f"#{label.get('color', 'ededed').lstrip('#')}",
                                        "description": label.get("description") or ""}, f"label {label['name']}")
            if created:
                existing[label["name"]] = created["id"]
        return existing

    def milestones(self, milestones: Sequence[Dict[str, Any]]) -> Dict[str, int]:
        """Create missing milestones; returns title -> id."""
        base = f"/repos/{self.owner}/{self.repo}/milestones"
        existing = {m["title"]: m["id"] for m in self._list(f"{base}?state=all")}
        for ms in milestones:
            if ms["title"] in existing:
                continue
            body = {"title": ms["title"], "description": ms.get("description") or "", "state": ms.get("state", "open")}
            if ms.get("due_on"):
                body["due_on"] = ms["due_on"]
            created = self._post(base, body, f"milestone {ms['title']}")
            if created:
                existing[ms["title"]] = created["id"]
        return existing

    def issues(self, issues: Sequence[Dict[str, Any]], prs: Sequence[Dict[str, Any]], comments: Sequence[Dict[str, Any]],
               label_ids: Dict[str, int], milestone_ids: Dict[str, int]) -> None:
        """Create issues (and PRs as issues) with comments, skipping already-imported ones."""
        base = f"/repos/{self.owner}/{self.repo}/issues"
        existing_bodies = [i.get("body") or "" for i in self._list(f"{base}?state=all&type=issues")]
        by_issue_url: Dict[str, List[Dict[str, Any]]] = {}
        for comment in comments:
            if comment.get("comment_type", "issue") == "issue":
                by_issue_url.setdefault(comment.get("issue_url", ""), []).append(comment)

        entries = [(i, "issue") for i in issues] + [(p, "pull request") for p in prs]
        for item, kind in sorted(entries, key=lambda e: e[0].get("number", 0)):
            marker = _marker(self.source, item.get("number"))
            if any(marker in body for body in existing_bodies):
                self._bump("skipped_existing_issues")
                continue
            names = [l["name"] for l in item.get("labels", []) if isinstance(l, dict)]
            if kind == "pull request":
                names.append(PR_LABEL)
            body: Dict[str, Any] = {
                "title": item.get("title") or "(untitled)",
                "body": _provenance(item, kind) + (item.get("body") or "") + "\n\n" + marker,
                "labels": [label_ids[n] for n in names if n in label_ids],
                "closed": item.get("state") == "closed",
            }
            ms = (item.get("milestone") or {}).get("title")
            if ms and ms in milestone_ids:
                body["milestone"] = milestone_ids[ms]
            created = self._post(base, body, f"{kind} #{item.get('number')}: {item.get('title')}")
            issue_api_url = item.get("url", "").replace("/pulls/", "/issues/")
            for comment in by_issue_url.get(issue_api_url, []):
                text = _provenance(comment, "comment") + (comment.get("body") or "")
                if created:
                    self._post(f"{base}/{created['number']}/comments", {"body": text}, "comment")
                else:
                    self._post(f"{base}/0/comments", {"body": text}, f"comment on #{item.get('number')}")
            if created and body["closed"]:
                self.client.request("PATCH", f"{base}/{created['number']}", json_body={"state": "closed"})

    def releases(self, releases: Sequence[Dict[str, Any]]) -> None:
        """Create releases for tags that exist in the mirrored git data (assets not restored)."""
        base = f"/repos/{self.owner}/{self.repo}/releases"
        existing = {r["tag_name"] for r in self._list(base)}
        for rel in releases:
            if rel.get("tag_name") in existing:
                continue
            assets = "\n".join(f"- {a.get('name')} ({a.get('size')} bytes): {a.get('browser_download_url')}" for a in rel.get("assets", []))
            note = f"\n\n---\nOriginal assets (not restored):\n{assets}" if assets else ""
            body = {"tag_name": rel["tag_name"], "name": rel.get("name") or rel["tag_name"],
                    "body": (rel.get("body") or "") + note, "draft": bool(rel.get("draft")),
                    "prerelease": bool(rel.get("prerelease"))}
            try:
                self._post(base, body, f"release {rel['tag_name']}")
            except ForgejoError as exc:
                log.warning("Release %s not created (tag missing?): %s", rel["tag_name"], exc)
                self._bump("failed_releases")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--backup", required=True, help="Backup directory, e.g. /backups/20261028")
    parser.add_argument("--source", required=True, help="GitHub OWNER/REPO as stored in the backup")
    parser.add_argument("--target", required=True, help="Forgejo OWNER/REPO to import into")
    parser.add_argument("--apply", action="store_true", help="Actually create objects (default: dry-run)")
    parser.add_argument("--skip", nargs="*", default=[], choices=["labels", "milestones", "issues", "releases"])
    args = parser.parse_args(argv)

    token = os.environ.get("FORGEJO_TOKEN", "")
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"), os.environ.get("LOG_FORMAT", "text"), [token])
    repo_dir = Path(args.backup) / args.source
    if not repo_dir.is_dir():
        log.error("Backup for %s not found at %s", args.source, repo_dir)
        return 1
    if not token:
        log.error("FORGEJO_TOKEN is required")
        return 4
    client = ForgejoClient(os.environ.get("FORGEJO_URL", "http://forgejo:3000"), token)
    if client.get_repo(*args.target.split("/", 1)) is None:
        log.error("Target repository %s does not exist in Forgejo", args.target)
        return 1
    restorer = Restorer(client, args.source, args.target, args.apply)
    label_ids = restorer.labels(_load(repo_dir, "labels")) if "labels" not in args.skip else {}
    milestone_ids = restorer.milestones(_load(repo_dir, "milestones")) if "milestones" not in args.skip else {}
    if "issues" not in args.skip:
        restorer.issues(_load(repo_dir, "issues"), _load(repo_dir, "pull_requests"), _load(repo_dir, "comments"),
                        label_ids, milestone_ids)
    if "releases" not in args.skip:
        restorer.releases(_load(repo_dir, "releases"))
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", "stats": restorer.stats}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
