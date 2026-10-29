# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 forgejo-github-nightly-cloner contributors
"""Tests for the GitHub API client (HTTP mocked with ``responses``)."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
import responses

from scripts.common.github_client import (
    AuthError,
    ForbiddenError,
    GitHubClient,
    NotFoundError,
    RateLimitError,
    RepoInfo,
    ServerError,
)
from scripts.common.logging_utils import REDACTED, redact, setup_logging
from tests.conftest import GITHUB_TOKEN, load_fixture

API = "https://api.github.com"
NOW = 1_800_000_000.0


@pytest.fixture
def sleep() -> MagicMock:
    return MagicMock()


@pytest.fixture
def client(sleep: MagicMock) -> GitHubClient:
    return GitHubClient(GITHUB_TOKEN, max_retries=2, retry_delay=1, rate_limit_buffer=10, sleep=sleep, clock=lambda: NOW)


def _headers(remaining: int = 4999, reset: float = NOW + 3600) -> dict:
    return {"X-RateLimit-Remaining": str(remaining), "X-RateLimit-Reset": str(int(reset))}


def test_repo_info_from_fixture() -> None:
    info = RepoInfo.from_api(load_fixture("github_repo.json"))
    assert info.full_name == "example-org/api-server"
    assert info.owner == "example-org"
    assert info.private is True and info.fork is False
    assert info.clone_url == "https://github.com/example-org/api-server.git"


@responses.activate
def test_sends_bearer_token(client: GitHubClient) -> None:
    responses.get(f"{API}/repos/example-org/api-server", json=load_fixture("github_repo.json"), headers=_headers())
    client.get_repo("example-org", "api-server")
    assert responses.calls[0].request.headers["Authorization"] == f"Bearer {GITHUB_TOKEN}"
    assert GITHUB_TOKEN not in responses.calls[0].request.url


@responses.activate
def test_rate_limit_low_remaining_sleeps_until_reset(client: GitHubClient, sleep: MagicMock) -> None:
    repo = load_fixture("github_repo.json")
    responses.get(f"{API}/repos/example-org/api-server", json=repo, headers=_headers(remaining=3, reset=NOW + 60))
    client.get_repo("example-org", "api-server")
    sleep.assert_not_called()  # first request: quota unknown
    client.get_repo("example-org", "api-server")
    sleep.assert_called_once_with(65)  # reset (60s) + 5s buffer
    assert len(responses.calls) == 2


@responses.activate
def test_rate_limit_403_exhausted_waits_and_retries(client: GitHubClient, sleep: MagicMock) -> None:
    url = f"{API}/repos/o/r/labels"
    responses.get(url, status=403, json={"message": "API rate limit exceeded"}, headers=_headers(remaining=0, reset=NOW + 30))
    responses.get(url, json=[{"name": "bug"}], headers=_headers())
    assert client.list_labels("o", "r") == [{"name": "bug"}]
    sleep.assert_called_once_with(35)


@responses.activate
def test_rate_limit_error_after_retries(client: GitHubClient) -> None:
    url = f"{API}/repos/o/r/labels"
    for _ in range(3):
        responses.get(url, status=429, json={"message": "secondary rate limit"}, headers={"Retry-After": "1"})
    with pytest.raises(RateLimitError):
        client.list_labels("o", "r")
    assert len(responses.calls) == 3


@responses.activate
def test_pagination_follows_link_header(client: GitHubClient) -> None:
    repos = load_fixture("github_org_repos.json")
    responses.get(
        f"{API}/orgs/example-org/repos",
        json=repos[:2],
        headers={**_headers(), "Link": f'<{API}/organizations/42/repos?page=2>; rel="next", <{API}/organizations/42/repos?page=3>; rel="last"'},
    )
    responses.get(
        f"{API}/organizations/42/repos",
        json=repos[2:4],
        headers={**_headers(), "Link": f'<{API}/organizations/42/repos?page=3>; rel="next"'},
    )
    responses.get(f"{API}/organizations/42/repos", json=repos[4:], headers=_headers())
    result = client.list_org_repos("example-org")
    assert [r.full_name for r in result] == [r["full_name"] for r in repos]
    assert len(responses.calls) == 3
    assert "per_page=100" in responses.calls[0].request.url
    assert responses.calls[1].request.url.endswith("page=2")
    assert responses.calls[2].request.url.endswith("page=3")


@responses.activate
def test_retry_on_5xx_then_success(client: GitHubClient, sleep: MagicMock) -> None:
    url = f"{API}/repos/example-org/api-server"
    responses.get(url, status=503, json={"message": "Service Unavailable"})
    responses.get(url, json=load_fixture("github_repo.json"), headers=_headers())
    info = client.get_repo("example-org", "api-server")
    assert info.name == "api-server"
    assert len(responses.calls) == 2
    sleep.assert_called_once_with(1)  # retry_delay * 2**0


@responses.activate
def test_5xx_exhausts_retries_with_backoff(client: GitHubClient, sleep: MagicMock) -> None:
    url = f"{API}/repos/o/r"
    for _ in range(3):
        responses.get(url, status=502)
    with pytest.raises(ServerError):
        client.get_repo("o", "r")
    assert [c.args[0] for c in sleep.call_args_list] == [1, 2]


@responses.activate
def test_401_raises_auth_error(client: GitHubClient) -> None:
    responses.get(f"{API}/orgs/example-org/repos", status=401, json={"message": "Bad credentials"})
    with pytest.raises(AuthError) as excinfo:
        client.list_org_repos("example-org")
    assert excinfo.value.status_code == 401
    assert len(responses.calls) == 1  # never retried


@responses.activate
def test_404_and_403_map_to_specific_errors(client: GitHubClient) -> None:
    responses.get(f"{API}/repos/o/missing", status=404, json={"message": "Not Found"})
    responses.get(f"{API}/repos/o/r/collaborators", status=403, json={"message": "Must have push access"})
    with pytest.raises(NotFoundError):
        client.get_repo("o", "missing")
    with pytest.raises(ForbiddenError):
        client.list_collaborators("o", "r")


@responses.activate
def test_token_not_in_exception_or_logs(client: GitHubClient, caplog: pytest.LogCaptureFixture) -> None:
    # A hostile/misbehaving server echoing the credential back must not leak it.
    responses.get(f"{API}/orgs/x/repos", status=401, json={"message": f"Bad credentials: Bearer {GITHUB_TOKEN}"})
    caplog.set_level(logging.DEBUG)
    with pytest.raises(AuthError) as excinfo:
        client.list_org_repos("x")
    assert GITHUB_TOKEN not in str(excinfo.value)
    assert GITHUB_TOKEN not in repr(excinfo.value.args)
    assert GITHUB_TOKEN not in caplog.text


def test_logging_redacts_secrets(capsys: pytest.CaptureFixture) -> None:
    setup_logging("DEBUG", "text", [GITHUB_TOKEN])
    logger = logging.getLogger("test.redaction")
    logger.info("token is %s", GITHUB_TOKEN)
    logger.info("header Authorization: token abcdef0123456789abcdef0123456789")
    logger.info("url https://user:s3cretpass@github.com/o/r.git")
    out = capsys.readouterr().out
    assert GITHUB_TOKEN not in out
    assert "abcdef0123456789abcdef0123456789" not in out
    assert "s3cretpass" not in out
    assert out.count(REDACTED) >= 3


def test_redact_patterns() -> None:
    assert "github_pat_" not in redact("github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz")
    assert redact("nothing secret here") == "nothing secret here"


@responses.activate
def test_list_issues_filters_pull_requests(client: GitHubClient) -> None:
    responses.get(
        f"{API}/repos/o/r/issues",
        json=[{"number": 1, "title": "bug"}, {"number": 2, "title": "pr", "pull_request": {"url": "x"}}],
        headers=_headers(),
    )
    assert [i["number"] for i in client.list_issues("o", "r")] == [1]
    assert "state=all" in responses.calls[0].request.url


@responses.activate
def test_list_user_repos_for_token_owner_includes_private(client: GitHubClient) -> None:
    repo = load_fixture("github_repo.json")
    own = dict(repo, owner={"login": "me"}, full_name="me/secret", name="secret", private=True)
    other = dict(repo, owner={"login": "someone-else"}, full_name="someone-else/x", name="x")
    responses.get(f"{API}/user", json={"login": "me"}, headers=_headers())
    responses.get(f"{API}/user/repos", json=[own, other], headers=_headers())
    result = client.list_user_repos("me")
    assert [r.full_name for r in result] == ["me/secret"]
    assert "affiliation=owner" in responses.calls[1].request.url


@responses.activate
def test_list_user_repos_other_user_uses_public_endpoint(client: GitHubClient) -> None:
    responses.get(f"{API}/user", json={"login": "me"}, headers=_headers())
    responses.get(f"{API}/users/example-user/repos", json=[load_fixture("github_repo.json")], headers=_headers())
    assert len(client.list_user_repos("example-user")) == 1


@responses.activate
def test_topics_and_rate_limit_status(client: GitHubClient) -> None:
    responses.get(f"{API}/repos/o/r/topics", json={"names": ["a", "b"]}, headers=_headers())
    responses.get(f"{API}/rate_limit", json={"resources": {"core": {"limit": 5000, "remaining": 4990}}}, headers=_headers())
    assert client.get_topics("o", "r") == ["a", "b"]
    assert client.get_rate_limit_status()["remaining"] == 4990


def test_empty_token_rejected() -> None:
    with pytest.raises(AuthError):
        GitHubClient("")
