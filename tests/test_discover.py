# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 forgejo-github-nightly-cloner contributors
"""Tests for repository discovery and its safety checks."""

from __future__ import annotations

import pytest

from scripts.common.github_client import AuthError, ServerError
from scripts.discover import DiscoveryError, discover, is_excluded_by_pattern
from tests.conftest import make_repo


def _names(repos):
    return sorted(r.full_name for r in repos)


def test_normal_discovery(mock_config, mock_github_client, temp_state_db, org_repos):
    mock_github_client.list_user_repos.return_value = [make_repo("example-user/dotfiles")]
    mock_github_client.list_org_repos.return_value = org_repos

    result = discover(mock_config, mock_github_client, temp_state_db, run_id="run-1")

    assert result.error is None
    assert _names(result.discovered) == ["example-org/api-server", "example-org/web-frontend", "example-user/dotfiles"]
    assert _names(result.excluded) == ["example-org/archived-docs", "example-org/forked-lib", "example-org/legacy-tool"]
    mock_github_client.list_user_repos.assert_called_once_with("example-user")
    mock_github_client.list_org_repos.assert_called_once_with("example-org")
    known = {r["repo_full_name"] for r in temp_state_db.get_known_repos()}
    assert known == set(_names(result.discovered))
    assert temp_state_db.last_discovery_run()["discovered_count"] == 3


def test_exclusion_filter(mock_config, mock_github_client, temp_state_db):
    mock_github_client.list_user_repos.return_value = [
        make_repo("example-user/personal-notes"),
        make_repo("example-user/Personal-Diary"),
        make_repo("example-user/project"),
    ]
    mock_github_client.list_org_repos.return_value = [make_repo("example-org/archived-site")]

    result = discover(mock_config, mock_github_client, temp_state_db)

    assert _names(result.discovered) == ["example-user/project"]
    assert "matches exclude pattern" in result.exclusion_reasons["example-user/personal-notes"]
    assert "example-user/Personal-Diary" in result.exclusion_reasons  # case-insensitive glob


def test_pattern_helper():
    assert is_excluded_by_pattern("a/archived-x", ["*/archived-*"]) == "*/archived-*"
    assert is_excluded_by_pattern("a/live", ["*/archived-*"]) is None


def test_empty_response_with_known_repos_raises(mock_config, mock_github_client, temp_state_db):
    temp_state_db.upsert_known_repo("example-org/api-server")
    temp_state_db.upsert_known_repo("example-user/dotfiles")
    mock_github_client.list_user_repos.return_value = []
    mock_github_client.list_org_repos.return_value = []

    with pytest.raises(DiscoveryError, match="0 repositories"):
        discover(mock_config, mock_github_client, temp_state_db)
    # Nothing was recorded or removed.
    assert len(temp_state_db.get_known_repos()) == 2
    assert temp_state_db.last_discovery_run() is None


def test_auth_failure_with_known_repos_raises(mock_config, mock_github_client, temp_state_db):
    temp_state_db.upsert_known_repo("example-org/api-server")
    mock_github_client.list_user_repos.side_effect = AuthError("401 Bad credentials", 401)
    mock_github_client.list_org_repos.side_effect = AuthError("401 Bad credentials", 401)

    with pytest.raises(DiscoveryError, match="AuthError"):
        discover(mock_config, mock_github_client, temp_state_db)


def test_single_source_suddenly_empty_raises(mock_config, mock_github_client, temp_state_db):
    temp_state_db.upsert_known_repo("example-org/api-server")
    mock_github_client.list_user_repos.return_value = [make_repo("example-user/dotfiles")]
    mock_github_client.list_org_repos.return_value = []  # e.g. org access revoked

    with pytest.raises(DiscoveryError, match="example-org"):
        discover(mock_config, mock_github_client, temp_state_db)


def test_partial_failure_with_known_repos_raises(mock_config, mock_github_client, temp_state_db, org_repos):
    temp_state_db.upsert_known_repo("example-org/api-server")
    mock_github_client.list_user_repos.side_effect = ServerError("503", 503)
    mock_github_client.list_org_repos.return_value = org_repos
    with pytest.raises(DiscoveryError):
        discover(mock_config, mock_github_client, temp_state_db)


def test_first_run_failure_returns_error_without_recording(mock_config, mock_github_client, temp_state_db):
    mock_github_client.list_user_repos.side_effect = ServerError("503", 503)
    mock_github_client.list_org_repos.return_value = [make_repo("example-org/app")]

    result = discover(mock_config, mock_github_client, temp_state_db)

    assert result.error and "ServerError" in result.error
    assert _names(result.discovered) == ["example-org/app"]
    assert temp_state_db.get_known_repos() == []


def test_first_run_empty_is_not_an_error(mock_config, mock_github_client, temp_state_db):
    mock_github_client.list_user_repos.return_value = []
    mock_github_client.list_org_repos.return_value = []
    result = discover(mock_config, mock_github_client, temp_state_db)
    assert result.discovered == [] and result.error is None


def test_fork_and_archived_filters(mock_config, mock_github_client, temp_state_db, org_repos):
    mock_github_client.list_user_repos.return_value = []
    mock_github_client.list_org_repos.return_value = org_repos

    mock_config.github.include_forks = True
    mock_config.github.include_archived = True
    result = discover(mock_config, mock_github_client, temp_state_db)
    assert "example-org/forked-lib" in _names(result.discovered)
    assert "example-org/legacy-tool" in _names(result.discovered)
    assert "example-org/archived-docs" in _names(result.excluded)  # glob still applies


def test_private_filter(mock_config, mock_github_client, temp_state_db, org_repos):
    mock_github_client.list_user_repos.return_value = []
    mock_github_client.list_org_repos.return_value = org_repos
    mock_config.github.include_private = False
    result = discover(mock_config, mock_github_client, temp_state_db)
    assert "example-org/api-server" not in _names(result.discovered)
    assert "private" in result.exclusion_reasons["example-org/api-server"]


def test_duplicate_repos_deduplicated(mock_config, mock_github_client, temp_state_db):
    mock_config.github.accounts = ["example-org"]  # same owner configured twice
    mock_github_client.list_user_repos.return_value = [make_repo("example-org/app")]
    mock_github_client.list_org_repos.return_value = [make_repo("example-org/app")]
    result = discover(mock_config, mock_github_client, temp_state_db)
    assert _names(result.discovered) == ["example-org/app"]


def test_missing_repo_is_kept_not_deleted(mock_config, mock_github_client, temp_state_db):
    temp_state_db.upsert_known_repo("example-org/old-repo")
    mock_github_client.list_user_repos.return_value = [make_repo("example-user/a")]
    mock_github_client.list_org_repos.return_value = [make_repo("example-org/new-repo")]
    discover(mock_config, mock_github_client, temp_state_db)
    known = {r["repo_full_name"] for r in temp_state_db.get_known_repos()}
    assert "example-org/old-repo" in known
