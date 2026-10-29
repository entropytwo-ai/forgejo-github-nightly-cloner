# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 forgejo-github-nightly-cloner contributors
"""Tests for the staged metadata backup, retention and verification."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from scripts.backup import apply_retention, run_backup
from scripts.common.github_client import AuthError, ForbiddenError, NotFoundError, ServerError
from scripts.verify import verify_backup

DATE = "20261028"
TODAY = date(2026, 10, 28)


def _stub_github(mock):
    mock.list_issues.return_value = [{"number": 1, "title": "Bug"}, {"number": 3, "title": "Feature"}]
    mock.list_pull_requests.return_value = [{"number": 2, "title": "Fix bug"}]
    mock.list_labels.return_value = [{"name": "bug", "color": "d73a4a"}]
    mock.list_milestones.return_value = []
    mock.list_releases.return_value = [{"tag_name": "v1.0.0", "assets": []}]
    mock.list_issue_comments.return_value = [{"id": 10, "body": "+1"}]
    mock.list_pr_comments.return_value = [{"id": 11, "body": "nit"}]
    mock.get_topics.return_value = ["backup"]
    mock.list_collaborators.return_value = [{"login": "alice"}]
    return mock


def test_staging_promoted_to_final_on_success(mock_config, mock_github_client, temp_state_db, temp_backup_dir):
    _stub_github(mock_github_client)
    result = run_backup(mock_config, mock_github_client, temp_state_db, ["o/a", "o/b"], DATE, today=TODAY)

    final = temp_backup_dir / DATE
    assert result.success, result.errors
    assert result.final_path == final and final.is_dir()
    assert not (temp_backup_dir / f"{DATE}-staging").exists()
    assert json.loads((final / "o" / "a" / "issues.json").read_text()) == mock_github_client.list_issues.return_value
    comments = json.loads((final / "o" / "a" / "comments.json").read_text())
    assert [c["comment_type"] for c in comments] == ["issue", "review"]
    assert (final / "checksums.sha256").is_file()
    assert verify_backup(final).ok
    assert temp_state_db.get_backup("o/a", DATE)["status"] == "complete"


def test_manifest_structure(mock_config, mock_github_client, temp_state_db, temp_backup_dir):
    _stub_github(mock_github_client)
    run_backup(mock_config, mock_github_client, temp_state_db, ["o/a"], DATE, today=TODAY)
    manifest = json.loads((temp_backup_dir / DATE / "manifest.json").read_text())

    assert manifest["backup_date"] == DATE
    assert manifest["completed_at"]
    assert manifest["summary"] == {"total": 1, "succeeded": 1, "failed": 0, "inaccessible": 0, "skipped": 0}
    assert manifest["coverage"]["captured"] == mock_config.backup.metadata_types
    assert "discussions" in manifest["coverage"]["unsupported"]
    assert "Forgejo runner" in manifest["coverage"]["notes"]
    entry = manifest["repositories"]["o/a"]
    assert entry["status"] == "succeeded" and entry["error"] is None
    assert entry["items"] == {"issues": 2, "pull_requests": 1, "labels": 1, "milestones": 0, "releases": 1,
                              "comments": 2, "topics": 1, "collaborators": 1}
    assert "o/a/issues.json" in manifest["checksums"]


def test_failed_backup_does_not_overwrite_previous(mock_config, mock_github_client, temp_state_db, temp_backup_dir):
    _stub_github(mock_github_client)
    assert run_backup(mock_config, mock_github_client, temp_state_db, ["o/a"], DATE, today=TODAY).success
    final_manifest = (temp_backup_dir / DATE / "manifest.json").read_text()

    # Second run on the same date: GitHub is down for every request.
    for name in ("list_issues", "list_pull_requests", "list_labels", "list_milestones", "list_releases",
                 "list_issue_comments", "list_pr_comments", "get_topics", "list_collaborators"):
        getattr(mock_github_client, name).side_effect = ServerError("503 after retries", 503)
    result = run_backup(mock_config, mock_github_client, temp_state_db, ["o/a"], DATE, today=TODAY)

    assert not result.success
    assert result.errors
    assert (temp_backup_dir / DATE / "manifest.json").read_text() == final_manifest  # untouched
    staging = temp_backup_dir / f"{DATE}-staging"
    assert staging.is_dir()  # left for inspection
    assert json.loads((staging / "manifest.json").read_text())["repositories"]["o/a"]["status"] == "failed"


def test_failed_run_does_not_apply_retention(mock_config, mock_github_client, temp_state_db, temp_backup_dir):
    for name in ("20260101", "20260201", "20260301"):
        (temp_backup_dir / name).mkdir()
    mock_github_client.list_issues.side_effect = ServerError("boom", 500)
    result = run_backup(mock_config, mock_github_client, temp_state_db, ["o/a"], DATE, today=TODAY)
    assert not result.success
    assert sorted(p.name for p in temp_backup_dir.iterdir() if p.name.isdigit()) == ["20260101", "20260201", "20260301"]


def test_all_repos_unauthorized_is_not_promoted(mock_config, mock_github_client, temp_state_db, temp_backup_dir):
    for name in ("list_issues", "list_pull_requests", "list_labels", "list_milestones", "list_releases",
                 "list_issue_comments", "list_pr_comments", "get_topics", "list_collaborators"):
        getattr(mock_github_client, name).side_effect = AuthError("401", 401)
    result = run_backup(mock_config, mock_github_client, temp_state_db, ["o/a"], DATE, today=TODAY)
    assert not result.success
    assert not (temp_backup_dir / DATE).exists()


def test_retention_deletes_old_but_keeps_minimum(temp_backup_dir):
    for name in ("20260801", "20260815", "20260901", "20261020", "20261028", "20261028-staging", "20260701-staging"):
        (temp_backup_dir / name).mkdir()
    deleted = apply_retention(temp_backup_dir, retention_days=30, min_keep=2, today=TODAY)
    remaining = sorted(p.name for p in temp_backup_dir.iterdir())
    assert sorted(deleted) == ["20260701-staging", "20260801", "20260815", "20260901"]
    assert remaining == ["20261020", "20261028", "20261028-staging"]


def test_retention_always_keeps_two(temp_backup_dir):
    for name in ("20250101", "20250201", "20250301"):
        (temp_backup_dir / name).mkdir()
    deleted = apply_retention(temp_backup_dir, retention_days=1, min_keep=1, today=TODAY)  # min_keep floored to 2
    assert deleted == ["20250101"]
    assert sorted(p.name for p in temp_backup_dir.iterdir()) == ["20250201", "20250301"]


def test_successful_run_applies_retention(mock_config, mock_github_client, temp_state_db, temp_backup_dir):
    _stub_github(mock_github_client)
    for name in ("20250101", "20250201", "20261027"):
        (temp_backup_dir / name).mkdir()
    result = run_backup(mock_config, mock_github_client, temp_state_db, ["o/a"], DATE, today=TODAY)
    assert result.success
    assert sorted(result.deleted) == ["20250101", "20250201"]


def test_inaccessible_repo_recorded(mock_config, mock_github_client, temp_state_db, temp_backup_dir):
    _stub_github(mock_github_client)

    def gone(owner, repo, *a, **k):
        if repo == "gone":
            raise NotFoundError("404 Not Found", 404)
        return [{"number": 1}]

    for name in ("list_issues", "list_pull_requests", "list_labels", "list_milestones", "list_releases",
                 "list_issue_comments", "list_pr_comments", "get_topics", "list_collaborators"):
        getattr(mock_github_client, name).side_effect = gone

    result = run_backup(mock_config, mock_github_client, temp_state_db, ["o/gone", "o/ok"], DATE, today=TODAY)

    assert result.success, result.errors
    manifest = json.loads((temp_backup_dir / DATE / "manifest.json").read_text())
    assert manifest["repositories"]["o/gone"]["status"] == "inaccessible"
    assert "NotFoundError" in manifest["repositories"]["o/gone"]["error"]
    assert manifest["repositories"]["o/ok"]["status"] == "succeeded"
    assert manifest["summary"]["inaccessible"] == 1
    assert temp_state_db.get_backup("o/gone", DATE)["status"] == "inaccessible"


def test_forbidden_collaborators_does_not_fail_repo(mock_config, mock_github_client, temp_state_db, temp_backup_dir):
    _stub_github(mock_github_client)
    mock_github_client.list_collaborators.side_effect = ForbiddenError("403 Must have push access", 403)
    result = run_backup(mock_config, mock_github_client, temp_state_db, ["o/a"], DATE, today=TODAY)
    entry = result.manifest["repositories"]["o/a"]
    assert result.success
    assert entry["status"] == "succeeded"
    assert entry["items"]["collaborators"] is None
    assert "collaborators" in entry["inaccessible_types"]
    assert not (temp_backup_dir / DATE / "o" / "a" / "collaborators.json").exists()
    assert verify_backup(temp_backup_dir / DATE).ok


def test_verify_detects_corrupt_json(mock_config, mock_github_client, temp_state_db, temp_backup_dir):
    _stub_github(mock_github_client)
    run_backup(mock_config, mock_github_client, temp_state_db, ["o/a"], DATE, today=TODAY)
    target = temp_backup_dir / DATE / "o" / "a" / "labels.json"
    target.write_text('[{"name": "bug", ')  # truncated

    result = verify_backup(temp_backup_dir / DATE)

    assert not result.ok
    assert any("labels.json" in e and "invalid JSON" in e for e in result.errors)
    assert any("checksum mismatch" in e for e in result.errors)


def test_verify_detects_count_mismatch_and_missing_file(mock_config, mock_github_client, temp_state_db, temp_backup_dir):
    _stub_github(mock_github_client)
    run_backup(mock_config, mock_github_client, temp_state_db, ["o/a"], DATE, today=TODAY)
    repo_dir = temp_backup_dir / DATE / "o" / "a"
    (repo_dir / "issues.json").write_text("[]")
    (repo_dir / "topics.json").unlink()
    errors = verify_backup(temp_backup_dir / DATE).errors
    assert any("issues.json: has 0 items, manifest says 2" in e for e in errors)
    assert any("missing topics.json" in e for e in errors)


def test_verify_missing_manifest(tmp_path: Path):
    (tmp_path / "x").mkdir()
    result = verify_backup(tmp_path / "x")
    assert not result.ok and "manifest.json missing" in result.errors[0]


def test_dry_run_writes_nothing(mock_config, mock_github_client, temp_state_db, temp_backup_dir):
    result = run_backup(mock_config, mock_github_client, temp_state_db, ["o/a"], DATE, dry_run=True)
    assert result.success
    assert list(temp_backup_dir.iterdir()) == []
    mock_github_client.list_issues.assert_not_called()


def test_rerun_same_day_replaces_atomically(mock_config, mock_github_client, temp_state_db, temp_backup_dir):
    _stub_github(mock_github_client)
    run_backup(mock_config, mock_github_client, temp_state_db, ["o/a"], DATE, today=TODAY)
    mock_github_client.list_labels.return_value = [{"name": "bug"}, {"name": "docs"}]
    assert run_backup(mock_config, mock_github_client, temp_state_db, ["o/a"], DATE, today=TODAY).success
    labels = json.loads((temp_backup_dir / DATE / "o" / "a" / "labels.json").read_text())
    assert len(labels) == 2
    assert sorted(p.name for p in temp_backup_dir.iterdir()) == [DATE]
