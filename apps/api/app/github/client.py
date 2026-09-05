"""GitHub client abstraction for Phase 6A."""

from __future__ import annotations

import logging

import httpx

from app.config import settings

from .exceptions import (
    GitHubAuthenticationError,
    GitHubAuthorizationError,
    GitHubConfigurationError,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubTimeoutError,
    GitHubUpstreamError,
    GitHubValidationError,
)
from .models import (
    CreateBranchRequest,
    CreatePullRequestRequest,
    GitHubBranch,
    GitHubPullRequest,
    GitHubRepository,
)

logger = logging.getLogger(__name__)


class GitHubClient:
    """Safe abstraction for GitHub API operations."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token or settings.github_token
        if not self.token:
            raise GitHubConfigurationError("GitHub token is not configured")

        self.base_url = settings.github_api_url.rstrip("/")

        self._headers = {
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _handle_error(self, response: httpx.Response) -> None:
        """Map HTTP status codes to typed exceptions safely without logging secrets."""
        if response.status_code == 401:
            raise GitHubAuthenticationError("GitHub authentication failed (invalid token).")
        if response.status_code == 403:
            # Check for rate limit
            if response.headers.get("x-ratelimit-remaining") == "0":
                raise GitHubRateLimitError("GitHub API rate limit exceeded.")
            raise GitHubAuthorizationError("GitHub authorization failed (insufficient permissions).")
        if response.status_code == 404:
            raise GitHubNotFoundError("Resource not found on GitHub.")
        if response.status_code == 422:
            raise GitHubValidationError("GitHub validation error (422).")
        if response.status_code >= 500:
            raise GitHubUpstreamError(f"GitHub upstream error: {response.status_code}")

        # Fallback
        raise GitHubError(f"GitHub API error: {response.status_code}")

    async def get_repository(self, owner: str, repo: str) -> GitHubRepository:
        """Fetch repository metadata."""
        url = f"{self.base_url}/repos/{owner}/{repo}"
        logger.info("GitHubClient: fetching repository metadata for %s/%s", owner, repo)

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get(url, headers=self._headers)
            except httpx.TimeoutException as exc:
                raise GitHubTimeoutError("Request to GitHub timed out") from exc
            except httpx.RequestError as exc:
                raise GitHubError(f"Request to GitHub failed: {exc}") from exc

        if response.status_code != 200:
            self._handle_error(response)

        data = response.json()
        return GitHubRepository(
            owner=data["owner"]["login"],
            name=data["name"],
            full_name=data["full_name"],
            default_branch=data["default_branch"],
            private=data["private"],
            clone_url=data["clone_url"],
            html_url=data["html_url"],
        )

    async def get_branch(self, owner: str, repo: str, branch: str) -> GitHubBranch:
        """Fetch branch information."""
        url = f"{self.base_url}/repos/{owner}/{repo}/branches/{branch}"
        logger.info("GitHubClient: fetching branch %s for %s/%s", branch, owner, repo)

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get(url, headers=self._headers)
            except httpx.TimeoutException as exc:
                raise GitHubTimeoutError("Request to GitHub timed out") from exc
            except httpx.RequestError as exc:
                raise GitHubError(f"Request to GitHub failed: {exc}") from exc

        if response.status_code != 200:
            self._handle_error(response)

        data = response.json()
        return GitHubBranch(
            name=data["name"],
            sha=data["commit"]["sha"],
        )

    async def create_branch(self, request: CreateBranchRequest) -> GitHubBranch:
        """Create a new branch from a base SHA."""
        url = f"{self.base_url}/repos/{request.owner}/{request.repo}/git/refs"
        logger.info("GitHubClient: creating branch %s for %s/%s", request.branch_name, request.owner, request.repo)

        payload = {
            "ref": f"refs/heads/{request.branch_name}",
            "sha": request.base_sha
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.post(url, headers=self._headers, json=payload)
            except httpx.TimeoutException as exc:
                raise GitHubTimeoutError("Request to GitHub timed out") from exc
            except httpx.RequestError as exc:
                raise GitHubError(f"Request to GitHub failed: {exc}") from exc

        if response.status_code != 201:
            self._handle_error(response)

        data = response.json()
        # The create ref API doesn't return exactly the same branch object,
        # but it gives us the ref and the object sha.
        return GitHubBranch(
            name=request.branch_name,
            sha=data["object"]["sha"],
        )

    async def create_pull_request(self, request: CreatePullRequestRequest) -> GitHubPullRequest:
        """Create a new pull request."""
        url = f"{self.base_url}/repos/{request.owner}/{request.repo}/pulls"
        logger.info("GitHubClient: creating PR from %s to %s for %s/%s", request.head_branch, request.base_branch, request.owner, request.repo)

        payload = {
            "title": request.title,
            "body": request.body,
            "head": request.head_branch,
            "base": request.base_branch,
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.post(url, headers=self._headers, json=payload)
            except httpx.TimeoutException as exc:
                raise GitHubTimeoutError("Request to GitHub timed out") from exc
            except httpx.RequestError as exc:
                raise GitHubError(f"Request to GitHub failed: {exc}") from exc

        if response.status_code != 201:
            self._handle_error(response)

        data = response.json()
        return GitHubPullRequest(
            number=data["number"],
            title=data["title"],
            body=data.get("body", ""),
            head_branch=data["head"]["ref"],
            base_branch=data["base"]["ref"],
            html_url=data["html_url"],
            state=data["state"],
        )
