# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 forgejo-github-nightly-cloner contributors
"""GitHub REST API client.

Features:

* Bearer token authentication; the token is never logged or put in URLs or
  exception messages.
* Proactive rate limiting: when ``X-RateLimit-Remaining`` drops below the
  configured buffer the client sleeps until ``X-RateLimit-Reset`` + 5 seconds.
* Automatic pagination via the ``Link: <...>; rel="next"`` header.
* Bounded exponential-backoff retries for 429, secondary rate limits, 5xx and
  network errors.
* Typed exceptions: :class:`AuthError`, :class:`RateLimitError`,
  :class:`NotFoundError`, :class:`ForbiddenError`, :class:`ClientError`,
  :class:`ServerError`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional

import requests

from .logging_utils import redact, register_secret

log = logging.getLogger(__name__)

RESET_BUFFER_SECONDS = 5
MAX_SINGLE_SLEEP_SECONDS = 3600


class GitHubError(Exception):
    """Base class for GitHub API errors. Messages never contain credentials."""

    def __init__(self, message: str, status_code: Optional[int] = None, url: Optional[str] = None) -> None:
        super().__init__(redact(message))
        self.status_code = status_code
        self.url = url


class AuthError(GitHubError):
    """401: the token is missing, invalid or expired."""


class RateLimitError(GitHubError):
    """403/429 caused by primary or secondary rate limiting, retries exhausted."""


class ClientError(GitHubError):
    """Any other 4xx response."""


class ForbiddenError(ClientError):
    """403 that is *not* rate limiting (insufficient scope / permissions)."""


class NotFoundError(ClientError):
    """404: resource missing or not visible to the token."""


class ServerError(GitHubError):
    """5xx responses or network failures after all retries."""


@dataclass
class RepoInfo:
    """Subset of GitHub repository fields used by the worker."""

    name: str
    owner: str
    full_name: str
    private: bool
    fork: bool
    archived: bool
    clone_url: str
    has_wiki: bool = False
    description: str = ""
    size: int = 0
    default_branch: str = "main"

    @classmethod
    def from_api(cls, data: Dict[str, Any]) -> "RepoInfo":
        """Build from a GitHub ``repository`` JSON object."""
        owner = (data.get("owner") or {}).get("login") or data["full_name"].split("/", 1)[0]
        return cls(
            name=data["name"],
            owner=owner,
            full_name=data.get("full_name") or f"{owner}/{data['name']}",
            private=bool(data.get("private", False)),
            fork=bool(data.get("fork", False)),
            archived=bool(data.get("archived", False)),
            clone_url=data.get("clone_url") or f"https://github.com/{owner}/{data['name']}.git",
            has_wiki=bool(data.get("has_wiki", False)),
            description=data.get("description") or "",
            size=int(data.get("size") or 0),
            default_branch=data.get("default_branch") or "main",
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dict."""
        return asdict(self)


class GitHubClient:
    """Minimal, robust GitHub REST v3 client."""

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.github.com",
        max_retries: int = 3,
        retry_delay: float = 2.0,
        rate_limit_buffer: int = 100,
        timeout: float = 30.0,
        per_page: int = 100,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not token:
            raise AuthError("GitHub token is empty")
        register_secret(token)
        self.base_url = base_url.rstrip("/")
        self.max_retries = max(0, int(max_retries))
        self.retry_delay = max(0.0, float(retry_delay))
        self.rate_limit_buffer = max(0, int(rate_limit_buffer))
        self.timeout = timeout
        self.per_page = per_page
        self._sleep = sleep
        self._clock = clock
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "forgejo-github-nightly-cloner",
            }
        )
        self._remaining: Optional[int] = None
        self._reset_at: Optional[float] = None
        self._login: Optional[str] = None

    # ------------------------------------------------------------------ core
    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        return f"{self.base_url}/{path_or_url.lstrip('/')}"

    def _update_rate_limit(self, response: requests.Response) -> None:
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset = response.headers.get("X-RateLimit-Reset")
        if remaining is not None:
            try:
                self._remaining = int(remaining)
            except ValueError:
                pass
        if reset is not None:
            try:
                self._reset_at = float(reset)
            except ValueError:
                pass

    def _sleep_until_reset(self, reason: str) -> None:
        now = self._clock()
        reset_at = self._reset_at or now
        delay = min(max(reset_at - now, 0) + RESET_BUFFER_SECONDS, MAX_SINGLE_SLEEP_SECONDS)
        log.warning("GitHub rate limit %s; sleeping %.0fs until reset", reason, delay)
        self._sleep(delay)
        self._remaining = None

    def _wait_for_rate_limit(self) -> None:
        """Sleep proactively if the remaining quota is below the buffer."""
        if self._remaining is not None and self._remaining < self.rate_limit_buffer:
            self._sleep_until_reset(f"low ({self._remaining} remaining < buffer {self.rate_limit_buffer})")

    @staticmethod
    def _error_message(response: requests.Response) -> str:
        try:
            body = response.json()
            if isinstance(body, dict) and body.get("message"):
                return str(body["message"])[:300]
        except ValueError:
            pass
        return (response.text or response.reason or "")[:300]

    @staticmethod
    def _is_rate_limited(response: requests.Response) -> bool:
        if response.status_code == 429:
            return True
        if response.status_code != 403:
            return False
        if response.headers.get("X-RateLimit-Remaining") == "0" or response.headers.get("Retry-After"):
            return True
        return "rate limit" in GitHubClient._error_message(response).lower()

    def _backoff(self, attempt: int, response: Optional[requests.Response] = None) -> float:
        if response is not None and response.headers.get("Retry-After"):
            try:
                return min(float(response.headers["Retry-After"]), MAX_SINGLE_SLEEP_SECONDS)
            except ValueError:
                pass
        return self.retry_delay * (2 ** attempt)

    def _request(self, method: str, path_or_url: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
        """Perform a request with rate limiting, retries and error mapping."""
        url = self._url(path_or_url)
        attempt = 0
        while True:
            self._wait_for_rate_limit()
            try:
                response = self._session.request(method, url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise ServerError(f"{method} {url} failed after {attempt + 1} attempts: {type(exc).__name__}", url=url) from None
                delay = self._backoff(attempt)
                log.warning("Network error on %s %s (%s); retry %d/%d in %.0fs", method, url, type(exc).__name__, attempt + 1, self.max_retries, delay)
                self._sleep(delay)
                attempt += 1
                continue

            self._update_rate_limit(response)
            status = response.status_code
            if status < 400:
                return response

            message = self._error_message(response)
            if status == 401:
                raise AuthError(f"{method} {url}: 401 Unauthorized ({message})", status, url)
            if self._is_rate_limited(response):
                if attempt >= self.max_retries:
                    raise RateLimitError(f"{method} {url}: rate limited ({status}) after {attempt + 1} attempts", status, url)
                if response.headers.get("X-RateLimit-Remaining") == "0" and not response.headers.get("Retry-After"):
                    self._sleep_until_reset(f"exhausted on {url}")
                else:
                    delay = self._backoff(attempt, response)
                    log.warning("Rate limited (%d) on %s; retry %d/%d in %.0fs", status, url, attempt + 1, self.max_retries, delay)
                    self._sleep(delay)
                attempt += 1
                continue
            if status >= 500:
                if attempt >= self.max_retries:
                    raise ServerError(f"{method} {url}: {status} after {attempt + 1} attempts ({message})", status, url)
                delay = self._backoff(attempt)
                log.warning("Server error %d on %s; retry %d/%d in %.0fs", status, url, attempt + 1, self.max_retries, delay)
                self._sleep(delay)
                attempt += 1
                continue
            if status == 403:
                raise ForbiddenError(f"{method} {url}: 403 Forbidden ({message})", status, url)
            if status == 404:
                raise NotFoundError(f"{method} {url}: 404 Not Found ({message})", status, url)
            raise ClientError(f"{method} {url}: {status} ({message})", status, url)

    def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self._request("GET", path, params=params).json()

    def _paginate(self, path: str, params: Optional[Dict[str, Any]] = None, item_key: Optional[str] = None) -> Iterator[Any]:
        """Yield every item across all pages, following ``rel="next"`` links."""
        query: Optional[Dict[str, Any]] = dict(params or {})
        query.setdefault("per_page", self.per_page)
        url: Optional[str] = self._url(path)
        while url:
            response = self._request("GET", url, params=query)
            data = response.json()
            items = data.get(item_key, []) if item_key and isinstance(data, dict) else data
            if not isinstance(items, list):
                raise ClientError(f"Unexpected non-list response from {url}", response.status_code, url)
            yield from items
            next_link = response.links.get("next", {}).get("url")
            url = next_link
            query = None  # the next link already embeds all query parameters

    # --------------------------------------------------------------- public
    def get_authenticated_login(self) -> str:
        """Return the login of the token owner (cached)."""
        if self._login is None:
            self._login = str(self._get_json("/user").get("login", ""))
        return self._login

    def list_user_repos(self, username: str) -> List[RepoInfo]:
        """List all repositories owned by ``username``.

        If ``username`` is the token owner, ``/user/repos`` is used so that
        private repositories are included; otherwise ``/users/{u}/repos``
        (public only, as enforced by GitHub).
        """
        try:
            login = self.get_authenticated_login()
        except (NotFoundError, ForbiddenError):
            login = ""
        if login and login.lower() == username.lower():
            items = self._paginate("/user/repos", {"affiliation": "owner", "visibility": "all"})
            return [RepoInfo.from_api(r) for r in items if (r.get("owner") or {}).get("login", "").lower() == username.lower()]
        return [RepoInfo.from_api(r) for r in self._paginate(f"/users/{username}/repos", {"type": "owner"})]

    def list_org_repos(self, org: str) -> List[RepoInfo]:
        """List all repositories of an organization (private ones need ``repo`` + ``read:org``)."""
        return [RepoInfo.from_api(r) for r in self._paginate(f"/orgs/{org}/repos", {"type": "all"})]

    def get_repo(self, owner: str, repo: str) -> RepoInfo:
        """Fetch a single repository."""
        return RepoInfo.from_api(self._get_json(f"/repos/{owner}/{repo}"))

    def list_issues(self, owner: str, repo: str, state: str = "all") -> List[Dict[str, Any]]:
        """List issues (pull requests, which GitHub also returns here, are filtered out)."""
        items = self._paginate(f"/repos/{owner}/{repo}/issues", {"state": state, "direction": "asc"})
        return [i for i in items if "pull_request" not in i]

    def list_pull_requests(self, owner: str, repo: str, state: str = "all") -> List[Dict[str, Any]]:
        """List pull requests."""
        return list(self._paginate(f"/repos/{owner}/{repo}/pulls", {"state": state, "direction": "asc"}))

    def list_labels(self, owner: str, repo: str) -> List[Dict[str, Any]]:
        """List labels."""
        return list(self._paginate(f"/repos/{owner}/{repo}/labels"))

    def list_milestones(self, owner: str, repo: str, state: str = "all") -> List[Dict[str, Any]]:
        """List milestones."""
        return list(self._paginate(f"/repos/{owner}/{repo}/milestones", {"state": state}))

    def list_releases(self, owner: str, repo: str) -> List[Dict[str, Any]]:
        """List releases (asset metadata only; binaries are not downloaded)."""
        return list(self._paginate(f"/repos/{owner}/{repo}/releases"))

    def list_issue_comments(self, owner: str, repo: str) -> List[Dict[str, Any]]:
        """List all issue and PR conversation comments of the repository."""
        return list(self._paginate(f"/repos/{owner}/{repo}/issues/comments", {"sort": "created", "direction": "asc"}))

    def list_pr_comments(self, owner: str, repo: str) -> List[Dict[str, Any]]:
        """List all pull request review (diff) comments of the repository."""
        return list(self._paginate(f"/repos/{owner}/{repo}/pulls/comments", {"sort": "created", "direction": "asc"}))

    def get_topics(self, owner: str, repo: str) -> List[str]:
        """Return repository topics."""
        return list(self._get_json(f"/repos/{owner}/{repo}/topics").get("names", []))

    def list_collaborators(self, owner: str, repo: str) -> List[Dict[str, Any]]:
        """List collaborators (requires push access; often 403 for forks/public repos)."""
        return list(self._paginate(f"/repos/{owner}/{repo}/collaborators"))

    def get_rate_limit_status(self) -> Dict[str, Any]:
        """Return the ``core`` rate-limit resource (does not count against the quota)."""
        data = self._get_json("/rate_limit")
        return dict((data.get("resources") or {}).get("core") or data.get("rate") or {})
