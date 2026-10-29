# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 forgejo-github-nightly-cloner contributors
"""Tests for mirror provisioning and the Forgejo client."""

from __future__ import annotations

import json

import pytest
import responses

from scripts.common.forgejo_client import (
    ForgejoAuthError,
    ForgejoClient,
    ForgejoConflictError,
    ForgejoError,
    ForgejoServerError,
)
from scripts.mirror import provision_mirrors
from tests.conftest import FORGEJO_TOKEN, GITHUB_TOKEN, make_repo

FJ = "http://forgejo.test:3000/api/v1"


def test_existing_mirror_is_skipped(mock_config, mock_forgejo_client, temp_state_db):
    repo = make_repo("example-org/app")
    mock_forgejo_client.get_repo.return_value = {"name": "app", "mirror": True, "original_url": repo.clone_url}

    result = provision_mirrors(mock_config, mock_forgejo_client, temp_state_db, [repo])

    assert result.skipped == ["example-org/app"] and not result.created and not result.failed
    mock_forgejo_client.create_mirror.assert_not_called()
    assert temp_state_db.get_mirror("example-org/app")["status"] == "active"


def test_running_twice_is_idempotent(mock_config, mock_forgejo_client, temp_state_db):
    repo = make_repo("example-org/app")
    mock_forgejo_client.get_repo.side_effect = [None, {"mirror": True, "original_url": repo.clone_url}]
    first = provision_mirrors(mock_config, mock_forgejo_client, temp_state_db, [repo])
    second = provision_mirrors(mock_config, mock_forgejo_client, temp_state_db, [repo])
    assert first.created == ["example-org/app"]
    assert second.skipped == ["example-org/app"]
    assert mock_forgejo_client.create_mirror.call_count == 1


def test_missing_mirror_is_created(mock_config, mock_forgejo_client, temp_state_db):
    repo = make_repo("example-org/secret", private=True)
    mock_forgejo_client.get_repo.return_value = None
    mock_forgejo_client.create_mirror.return_value = {"name": "secret", "mirror": True}

    result = provision_mirrors(mock_config, mock_forgejo_client, temp_state_db, [repo])

    assert result.created == ["example-org/secret"]
    mock_forgejo_client.create_mirror.assert_called_once_with(
        owner="backup-user",
        repo_name="secret",
        clone_url="https://github.com/example-org/secret.git",
        github_token=GITHUB_TOKEN,
        private=True,
        lfs=True,
        mirror_interval="24h",
        description="secret description",
    )
    row = temp_state_db.get_mirror("example-org/secret")
    assert row["status"] == "active" and row["forgejo_owner"] == "backup-user"


def test_dry_run_does_not_create(mock_config, mock_forgejo_client, temp_state_db):
    mock_forgejo_client.get_repo.return_value = None
    result = provision_mirrors(mock_config, mock_forgejo_client, temp_state_db, [make_repo("example-org/app")], dry_run=True)
    assert result.created == ["example-org/app"]  # reported as "would create"
    mock_forgejo_client.create_mirror.assert_not_called()
    assert temp_state_db.get_mirror("example-org/app") is None


def test_failure_isolated_per_repo(mock_config, mock_forgejo_client, temp_state_db):
    repos = [make_repo("example-org/a"), make_repo("example-org/b"), make_repo("example-org/c")]
    mock_forgejo_client.get_repo.return_value = None
    mock_forgejo_client.create_mirror.side_effect = [{}, ForgejoServerError("500 boom", 500), {}]

    result = provision_mirrors(mock_config, mock_forgejo_client, temp_state_db, repos)

    assert result.created == ["example-org/a", "example-org/c"]
    assert list(result.failed) == ["example-org/b"]
    assert "ForgejoServerError" in result.failed["example-org/b"]
    assert temp_state_db.get_mirror("example-org/b")["status"] == "error"
    assert mock_forgejo_client.create_mirror.call_count == 3


def test_existing_non_mirror_is_not_overwritten(mock_config, mock_forgejo_client, temp_state_db):
    mock_forgejo_client.get_repo.return_value = {"name": "app", "mirror": False}
    result = provision_mirrors(mock_config, mock_forgejo_client, temp_state_db, [make_repo("example-org/app")])
    assert result.skipped == ["example-org/app"]
    mock_forgejo_client.create_mirror.assert_not_called()
    mock_forgejo_client.delete_repo.assert_not_called()


def test_writable_repo_in_state_is_skipped(mock_config, mock_forgejo_client, temp_state_db):
    temp_state_db.upsert_mirror("example-org/app", "backup-user", "app", "writable")
    result = provision_mirrors(mock_config, mock_forgejo_client, temp_state_db, [make_repo("example-org/app")])
    assert result.skipped == ["example-org/app"]
    mock_forgejo_client.get_repo.assert_not_called()


def test_conflict_is_treated_as_existing(mock_config, mock_forgejo_client, temp_state_db):
    mock_forgejo_client.get_repo.return_value = None
    mock_forgejo_client.create_mirror.side_effect = ForgejoConflictError("409 exists", 409)
    result = provision_mirrors(mock_config, mock_forgejo_client, temp_state_db, [make_repo("example-org/app")])
    assert result.skipped == ["example-org/app"] and not result.failed


def test_name_collision_detected(mock_config, mock_forgejo_client, temp_state_db):
    mock_forgejo_client.get_repo.return_value = None
    repos = [make_repo("example-org/app"), make_repo("example-user/app")]
    result = provision_mirrors(mock_config, mock_forgejo_client, temp_state_db, repos)
    assert result.created == ["example-org/app"]
    assert "collision" in result.failed["example-user/app"]


def test_existing_mirror_of_other_source_is_collision(mock_config, mock_forgejo_client, temp_state_db):
    mock_forgejo_client.get_repo.return_value = {"mirror": True, "original_url": "https://github.com/someone/app.git"}
    result = provision_mirrors(mock_config, mock_forgejo_client, temp_state_db, [make_repo("example-org/app")])
    assert "collision" in result.failed["example-org/app"]


def test_name_template(mock_config, mock_forgejo_client, temp_state_db):
    mock_config.forgejo.repo_name_template = "{owner}-{repo}"
    mock_forgejo_client.get_repo.return_value = None
    provision_mirrors(mock_config, mock_forgejo_client, temp_state_db, [make_repo("example-org/app")])
    assert mock_forgejo_client.create_mirror.call_args.kwargs["repo_name"] == "example-org-app"


# ------------------------------------------------------------ ForgejoClient HTTP
@responses.activate
def test_forgejo_get_repo_404_returns_none():
    responses.get(f"{FJ}/repos/backup-user/nope", status=404, json={"message": "not found"})
    client = ForgejoClient("http://forgejo.test:3000", FORGEJO_TOKEN)
    assert client.get_repo("backup-user", "nope") is None
    assert responses.calls[0].request.headers["Authorization"] == f"token {FORGEJO_TOKEN}"


@responses.activate
def test_forgejo_create_mirror_payload_keeps_token_out_of_url():
    responses.post(f"{FJ}/repos/migrate", status=201, json={"name": "app", "mirror": True})
    client = ForgejoClient("http://forgejo.test:3000", FORGEJO_TOKEN)
    client.create_mirror("backup-user", "app", "https://github.com/o/app.git", GITHUB_TOKEN, private=True, lfs=True,
                         mirror_interval="8h", description="d")
    request = responses.calls[0].request
    body = json.loads(request.body)
    assert body["mirror"] is True and body["service"] == "github" and body["auth_token"] == GITHUB_TOKEN
    assert body["repo_owner"] == "backup-user" and body["mirror_interval"] == "8h" and body["issues"] is False
    assert GITHUB_TOKEN not in request.url


@responses.activate
def test_forgejo_error_mapping_and_redaction():
    responses.post(f"{FJ}/repos/migrate", status=409, json={"message": "The repository with the same name already exists."})
    responses.get(f"{FJ}/version", status=401, json={"message": f"bad token {FORGEJO_TOKEN}"})
    client = ForgejoClient("http://forgejo.test:3000", FORGEJO_TOKEN)
    with pytest.raises(ForgejoConflictError):
        client.create_mirror("o", "r", "https://github.com/o/r.git", None)
    with pytest.raises(ForgejoAuthError) as excinfo:
        client.get_server_info()
    assert FORGEJO_TOKEN not in str(excinfo.value)


@responses.activate
def test_forgejo_set_mirror_enabled_and_convert():
    responses.patch(f"{FJ}/repos/o/r", json={"mirror": True})
    responses.get(f"{FJ}/repos/o/r", json={"mirror": True})
    client = ForgejoClient("http://forgejo.test:3000", FORGEJO_TOKEN)
    client.set_mirror_enabled("o", "r", False)
    assert json.loads(responses.calls[0].request.body) == {"mirror_interval": "0"}
    with pytest.raises(ForgejoError, match="Convert to regular repository"):
        client.convert_mirror_to_writable("o", "r")
