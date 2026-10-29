# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 forgejo-github-nightly-cloner contributors
"""Forgejo REST API (``/api/v1``) client.

Uses ``Authorization: token <TOKEN>``. Token values are registered with the
log redactor and are never included in log lines or exception messages.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from .logging_utils import redact, register_secret

log = logging.getLogger(__name__)


class ForgejoError(Exception):
    """Base class for Forgejo API errors."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(redact(message))
        self.status_code = status_code


class ForgejoAuthError(ForgejoError):
    """401 - invalid or missing token."""


class ForgejoForbiddenError(ForgejoError):
    """403 - token lacks permission/scope."""


class ForgejoNotFoundError(ForgejoError):
    """404 - resource does not exist."""


class ForgejoConflictError(ForgejoError):
    """409 - resource already exists (e.g. repository name taken)."""


class ForgejoClientError(ForgejoError):
    """Other 4xx (e.g. 422 validation errors)."""


class ForgejoServerError(ForgejoError):
    """5xx or network failure after retries."""


class ForgejoClient:
    """Thin wrapper around the Forgejo API endpoints used by this project."""

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        session: Optional[requests.Session] = None,
        sleep=time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_url = f"{self.base_url}/api/v1"
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.retry_delay = retry_delay
        self._sleep = sleep
        self._session = session or requests.Session()
        self._session.headers.update({"Accept": "application/json", "User-Agent": "forgejo-github-nightly-cloner"})
        if token:
            register_secret(token)
            self._session.headers["Authorization"] = f"token {token}"

    # ------------------------------------------------------------------ core
    def request(
        self,
        method: str,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        auth: Optional[Tuple[str, str]] = None,
        expected: Tuple[int, ...] = (200, 201, 202, 204),
    ) -> Any:
        """Call an API path (relative to ``/api/v1``) and return decoded JSON (or ``None``).

        Retries 5xx/network errors for idempotent methods only, so that e.g. a
        migration POST is never submitted twice.
        """
        url = f"{self.api_url}/{path.lstrip('/')}"
        retryable = method.upper() in {"GET", "HEAD", "PUT", "DELETE", "PATCH"}
        attempt = 0
        while True:
            try:
                headers = None
                if auth is not None:
                    headers = {"Authorization": None}  # use basic auth instead of the token
                response = self._session.request(
                    method, url, json=json_body, params=params, auth=auth, headers=headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                if retryable and attempt < self.max_retries:
                    attempt += 1
                    self._sleep(self.retry_delay * 2 ** (attempt - 1))
                    continue
                raise ForgejoServerError(f"{method} {url} failed: {type(exc).__name__}") from None

            status = response.status_code
            if status in expected or status < 400:
                if status == 204 or not response.content:
                    return None
                try:
                    return response.json()
                except ValueError:
                    return response.text
            if status >= 500 and retryable and attempt < self.max_retries:
                attempt += 1
                log.warning("Forgejo %d on %s %s; retry %d/%d", status, method, url, attempt, self.max_retries)
                self._sleep(self.retry_delay * 2 ** (attempt - 1))
                continue
            self._raise_for(method, url, response)

    @staticmethod
    def _raise_for(method: str, url: str, response: requests.Response) -> None:
        try:
            body = response.json()
            detail = body.get("message") if isinstance(body, dict) else str(body)
        except ValueError:
            detail = response.text
        detail = (detail or response.reason or "")[:300]
        status = response.status_code
        message = f"{method} {url}: {status} ({detail})"
        if status == 401:
            raise ForgejoAuthError(message, status)
        if status == 403:
            raise ForgejoForbiddenError(message, status)
        if status == 404:
            raise ForgejoNotFoundError(message, status)
        if status == 409:
            raise ForgejoConflictError(message, status)
        if status >= 500:
            raise ForgejoServerError(message, status)
        raise ForgejoClientError(message, status)

    def _paginate(self, path: str, limit: int = 50) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page = 1
        while True:
            batch = self.request("GET", path, params={"page": page, "limit": limit}) or []
            items.extend(batch)
            if len(batch) < limit:
                return items
            page += 1

    # --------------------------------------------------------------- public
    def get_server_info(self) -> Dict[str, Any]:
        """Return ``{"version": ...}`` from ``GET /version`` (also a connectivity check)."""
        return self.request("GET", "/version") or {}

    def get_repo(self, owner: str, repo: str) -> Optional[Dict[str, Any]]:
        """Return the repository JSON or ``None`` if it does not exist."""
        try:
            return self.request("GET", f"/repos/{owner}/{repo}")
        except ForgejoNotFoundError:
            return None

    def list_repos(self, owner: str) -> List[Dict[str, Any]]:
        """List repositories of an organization or user (org endpoint tried first)."""
        try:
            return self._paginate(f"/orgs/{owner}/repos")
        except ForgejoNotFoundError:
            return self._paginate(f"/users/{owner}/repos")

    def create_mirror(
        self,
        owner: str,
        repo_name: str,
        clone_url: str,
        github_token: Optional[str],
        private: bool = True,
        lfs: bool = True,
        mirror_interval: str = "24h",
        description: str = "",
    ) -> Dict[str, Any]:
        """Create a pull mirror with ``POST /repos/migrate``.

        Metadata migration (issues, PRs, ...) is disabled because Forgejo does
        not allow it for mirrors; metadata is backed up separately as JSON.
        The GitHub token is sent in the request body only (never in the URL),
        so Forgejo can fetch private repositories on every sync.
        """
        if github_token:
            register_secret(github_token)
        body: Dict[str, Any] = {
            "clone_addr": clone_url,
            "repo_name": repo_name,
            "repo_owner": owner,
            "mirror": True,
            "mirror_interval": mirror_interval,
            "private": bool(private),
            "lfs": bool(lfs),
            "description": (description or "")[:2048],
            "service": "github",
            "wiki": False,
            "issues": False,
            "labels": False,
            "milestones": False,
            "pull_requests": False,
            "releases": False,
        }
        if github_token:
            body["auth_token"] = github_token
        return self.request("POST", "/repos/migrate", json_body=body) or {}

    def trigger_mirror_sync(self, owner: str, repo: str) -> None:
        """Queue an immediate mirror sync (``POST /repos/{o}/{r}/mirror-sync``)."""
        self.request("POST", f"/repos/{owner}/{repo}/mirror-sync")

    def set_mirror_enabled(self, owner: str, repo: str, enabled: bool, interval: str = "24h") -> None:
        """Enable or disable periodic syncing by setting ``mirror_interval`` (``0`` disables)."""
        value = interval if enabled else "0"
        self.request("PATCH", f"/repos/{owner}/{repo}", json_body={"mirror_interval": value})

    def convert_mirror_to_writable(self, owner: str, repo: str) -> None:
        """Turn a pull mirror into a normal writable repository.

        Sends ``PATCH {"mirror": false}`` and then re-reads the repository to
        confirm. Forgejo versions whose API ignores the field leave the repo as
        a mirror; in that case :class:`ForgejoError` is raised with the manual
        alternative (Settings → "Convert to regular repository").
        """
        self.request("PATCH", f"/repos/{owner}/{repo}", json_body={"mirror": False})
        current = self.get_repo(owner, repo)
        if current is None:
            raise ForgejoNotFoundError(f"Repository {owner}/{repo} disappeared during conversion", 404)
        if current.get("mirror"):
            raise ForgejoError(
                f"{owner}/{repo} is still a mirror: this Forgejo version does not support conversion via API. "
                "Periodic sync has been disabled; finish the conversion in the web UI: "
                "Repository Settings -> Danger Zone -> 'Convert to regular repository'."
            )

    def create_user_token(
        self, description: str, username: str, password: str, scopes: Optional[List[str]] = None
    ) -> str:
        """Create an access token for ``username`` using basic auth (initial bootstrap)."""
        register_secret(password)
        body = {"name": description, "scopes": scopes or ["write:repository", "write:organization", "read:user", "read:admin"]}
        data = self.request("POST", f"/users/{username}/tokens", json_body=body, auth=(username, password))
        token = (data or {}).get("sha1") or (data or {}).get("token")
        if not token:
            raise ForgejoError("Token creation response did not contain a token")
        register_secret(token)
        return str(token)

    def delete_repo(self, owner: str, repo: str) -> None:
        """Delete a repository (never called by the scheduled jobs)."""
        self.request("DELETE", f"/repos/{owner}/{repo}")

    def create_fork_from_url(self, *args: Any, **kwargs: Any) -> None:
        """Not used: pull mirrors are preferred over forks/one-off imports."""
        raise NotImplementedError("Use create_mirror(); forks/one-off imports are intentionally unsupported")
