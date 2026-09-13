"""GitHub API client abstraction.

Phase 10B.2.2: Reusable pooled HTTP transport, explicit credential modes
(Installation vs App JWT vs Token), bounded retries, and typed APIs for
repository metadata and collaborator permissions.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any

import httpx

from app.config import settings
from app.github.app_auth import GitHubAppAuth
from app.github.exceptions import (
    GitHubAuthenticationError,
    GitHubAuthorizationError,
    GitHubConfigurationError,
    GitHubConflictError,
    GitHubConnectionError,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubTimeoutError,
    GitHubUpstreamError,
    GitHubValidationError,
)
from app.github.models import (
    CreateBranchRequest,
    CreatePullRequestRequest,
    GitHubBranch,
    GitHubCollaboratorPermission,
    GitHubPullRequest,
    GitHubRepository,
    _validate_branch_name,
)
from app.github.token_service import (
    InstallationTokenService,
    get_installation_token_service,
    validate_installation_id,
)

logger = logging.getLogger(__name__)

# Valid identifier pattern: alphanumeric, underscores, hyphens, dots
_PATH_SEGMENT_REGEX = re.compile(r"^[a-zA-Z0-9_.-]+$")


def _validate_path_segment(value: str, name: str = "identifier") -> str:
    """Validate path parameter to prevent path traversal and arbitrary path injection."""
    if not value or not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} cannot be empty")
    if not _PATH_SEGMENT_REGEX.match(value):
        raise ValueError(f"Invalid characters in {name}: {value!r}")
    if ".." in value:
        raise ValueError(f"Path traversal detected in {name}: {value!r}")
    return value


def _safe_int(val: Any, default: int) -> int:
    """Extract integer safely, falling back to default if val is mock or invalid."""
    if isinstance(val, int | float) and not isinstance(val, bool):
        return int(val)
    return default


def _safe_float(val: Any, default: float) -> float:
    """Extract float safely, falling back to default if val is mock or invalid."""
    if isinstance(val, int | float) and not isinstance(val, bool):
        return float(val)
    return default


def _safe_header_get(response: Any, header_name: str) -> str | None:
    """Safely extract a header value without triggering coroutines on mocks."""
    headers_obj = getattr(response, "headers", None)
    if headers_obj is None:
        return None
    get_fn = getattr(headers_obj, "get", None)
    if callable(get_fn):
        try:
            res = get_fn(header_name)
            if isinstance(res, str):
                return res
            if asyncio.iscoroutine(res):
                res.close()
                return None
        except Exception:
            return None
    return None


async def _safe_json(response: Any) -> Any:
    """Extract JSON payload whether json() is synchronous or a coroutine."""
    json_fn = getattr(response, "json", None)
    if callable(json_fn):
        data = json_fn()
        if asyncio.iscoroutine(data):
            return await data
        return data
    return {}


class CredentialMode(str, Enum):
    """Explicit credential mode for GitHubClient operations."""

    INSTALLATION = "installation"
    APP = "app"
    TOKEN = "token"  # nosec B105


class GitHubClient:
    """Safe, pooled abstraction for GitHub API operations.

    Supports:
    - Reusable connection-pooled httpx.AsyncClient.
    - Explicit credential separation (Installation Token vs App JWT vs Static Token).
    - Dynamic installation token acquisition via InstallationTokenService.
    - Bounded retries for transient errors and rate limits.
    - Typed repository and collaborator permission APIs.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        installation_id: int | None = None,
        mode: CredentialMode | None = None,
        app_jwt: str | None = None,
        token_service: InstallationTokenService | None = None,
        http_client: httpx.AsyncClient | None = None,
        base_url: str | None = None,
        max_retries: int | None = None,
        retry_backoff_factor: float | None = None,
        max_retry_after: int | None = None,
        sleep_func: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Initialize GitHubClient with connection pooling and credential mode."""
        base_url_val = base_url or getattr(settings, "github_api_url", "https://api.github.com")
        self.base_url = str(base_url_val).rstrip("/")
        self.max_retries = (
            max_retries
            if max_retries is not None
            else _safe_int(getattr(settings, "github_client_max_retries", 3), 3)
        )
        self.retry_backoff_factor = (
            retry_backoff_factor
            if retry_backoff_factor is not None
            else _safe_float(
                getattr(settings, "github_client_retry_backoff_factor", 0.5), 0.5
            )
        )
        self.max_retry_after = (
            max_retry_after
            if max_retry_after is not None
            else _safe_int(getattr(settings, "github_client_max_retry_after", 60), 60)
        )
        self._sleep = sleep_func or asyncio.sleep

        # Configure credential mode and tokens
        if mode is not None:
            self.mode = mode
        elif installation_id is not None:
            self.mode = CredentialMode.INSTALLATION
        elif app_jwt is not None:
            self.mode = CredentialMode.APP
        else:
            self.mode = CredentialMode.TOKEN

        self.installation_id: int | None = None
        self._app_jwt: str | None = None
        self.token: str = ""

        if self.mode == CredentialMode.INSTALLATION:
            if installation_id is None:
                raise GitHubConfigurationError(
                    "installation_id is required for INSTALLATION mode"
                )
            self.installation_id = validate_installation_id(installation_id)
            self._token_service = token_service or get_installation_token_service()
        elif self.mode == CredentialMode.APP:
            self._app_jwt = app_jwt
            self._token_service = token_service or get_installation_token_service()
        else:  # TOKEN mode
            token_val = token or getattr(settings, "github_token", "")
            self.token = str(token_val) if token_val else ""
            if not self.token:
                raise GitHubConfigurationError("GitHub token is not configured")
            self._token_service = token_service or get_installation_token_service()

        # Initialize pooled HTTP client
        if http_client is not None:
            self._client = http_client
            self._owns_client = False
        else:
            max_conn = _safe_int(
                getattr(settings, "github_client_max_connections", 100), 100
            )
            max_keep = _safe_int(
                getattr(settings, "github_client_max_keepalive", 20), 20
            )
            keep_exp = _safe_float(
                getattr(settings, "github_client_keepalive_expiry", 30.0), 30.0
            )
            t_conn = _safe_float(
                getattr(settings, "github_client_timeout_connect", 5.0), 5.0
            )
            t_read = _safe_float(
                getattr(settings, "github_client_timeout_read", 10.0), 10.0
            )
            t_write = _safe_float(
                getattr(settings, "github_client_timeout_write", 10.0), 10.0
            )
            t_pool = _safe_float(
                getattr(settings, "github_client_timeout_pool", 5.0), 5.0
            )

            limits = httpx.Limits(
                max_connections=max_conn,
                max_keepalive_connections=max_keep,
                keepalive_expiry=keep_exp,
            )
            timeout = httpx.Timeout(
                connect=t_conn,
                read=t_read,
                write=t_write,
                pool=t_pool,
            )
            self._client = httpx.AsyncClient(limits=limits, timeout=timeout)
            self._owns_client = True

    @classmethod
    def for_installation(
        cls,
        installation_id: int,
        *,
        token_service: InstallationTokenService | None = None,
        http_client: httpx.AsyncClient | None = None,
        **kwargs: Any,
    ) -> GitHubClient:
        """Create a client authenticated with short-lived GitHub Installation Access Tokens."""
        return cls(
            installation_id=installation_id,
            mode=CredentialMode.INSTALLATION,
            token_service=token_service,
            http_client=http_client,
            **kwargs,
        )

    @classmethod
    def for_app(
        cls,
        app_jwt: str | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
        **kwargs: Any,
    ) -> GitHubClient:
        """Create a client authenticated with GitHub App JWT for App-level operations."""
        return cls(
            app_jwt=app_jwt,
            mode=CredentialMode.APP,
            http_client=http_client,
            **kwargs,
        )

    @classmethod
    def for_token(
        cls,
        token: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        **kwargs: Any,
    ) -> GitHubClient:
        """Create a client authenticated with a static personal or workflow access token."""
        return cls(
            token=token,
            mode=CredentialMode.TOKEN,
            http_client=http_client,
            **kwargs,
        )

    @property
    def _headers(self) -> dict[str, str]:
        """Backward compatibility for existing callers inspecting client._headers."""
        token_val = self.token or (self._app_jwt if self.mode == CredentialMode.APP else "")
        return {
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"Bearer {token_val}" if token_val else "",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def aclose(self) -> None:
        """Close the underlying HTTP client if owned by this instance."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()

    async def _get_auth_header(self, operation: str) -> str:
        """Resolve Authorization header based on credential mode."""
        if self.mode == CredentialMode.INSTALLATION:
            if self.installation_id is None:
                raise GitHubConfigurationError(
                    "Missing installation_id in INSTALLATION mode", operation=operation
                )
            token = await self._token_service.get_token(self.installation_id)
            return f"Bearer {token}"
        elif self.mode == CredentialMode.APP:
            if self._app_jwt:
                token = self._app_jwt
            else:
                token = GitHubAppAuth.generate_jwt()
            return f"Bearer {token}"
        elif self.mode == CredentialMode.TOKEN:
            if not self.token:
                raise GitHubConfigurationError(
                    "Missing token in TOKEN mode", operation=operation
                )
            return f"Bearer {self.token}"
        else:
            raise GitHubConfigurationError(
                f"Unsupported credential mode: {self.mode}", operation=operation
            )

    def _handle_error(self, response: httpx.Response) -> None:
        """Backward compatibility: map HTTP status codes to typed exceptions safely."""
        req_id = _safe_header_get(response, "x-github-request-id")
        if response.status_code == 401:
            raise GitHubAuthenticationError(
                "GitHub authentication failed (invalid token).",
                status_code=401,
                github_request_id=req_id,
            )
        if response.status_code == 403:
            if _safe_header_get(response, "x-ratelimit-remaining") == "0":
                retry_after_str = _safe_header_get(response, "retry-after")
                retry_after = (
                    int(retry_after_str)
                    if retry_after_str and retry_after_str.isdigit()
                    else None
                )
                raise GitHubRateLimitError(
                    "GitHub API rate limit exceeded.",
                    status_code=403,
                    github_request_id=req_id,
                    retry_after=retry_after,
                )
            raise GitHubAuthorizationError(
                "GitHub authorization failed (insufficient permissions).",
                status_code=403,
                github_request_id=req_id,
            )
        if response.status_code == 404:
            raise GitHubNotFoundError(
                "Resource not found on GitHub.",
                status_code=404,
                github_request_id=req_id,
            )
        if response.status_code == 409:
            raise GitHubConflictError(
                "GitHub conflict error (409).",
                status_code=409,
                github_request_id=req_id,
            )
        if response.status_code == 422:
            raise GitHubValidationError(
                "GitHub validation error (422).",
                status_code=422,
                github_request_id=req_id,
            )
        if response.status_code >= 500:
            raise GitHubUpstreamError(
                f"GitHub upstream error: {response.status_code}",
                status_code=response.status_code,
                github_request_id=req_id,
            )
        raise GitHubError(
            f"GitHub API error: {response.status_code}",
            status_code=response.status_code,
            github_request_id=req_id,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        operation: str,
    ) -> httpx.Response:
        """Execute an HTTP request with credential resolution, error mapping, and bounded retries."""
        url = f"{self.base_url}/{path.lstrip('/')}"

        # Enforce credential separation: App JWT cannot be used for repository-scoped calls
        if self.mode == CredentialMode.APP and operation not in (
            "create_installation_access_token",
            "app_meta",
        ):
            raise GitHubConfigurationError(
                "Repository-scoped operations require an installation access token, not an App JWT",
                operation=operation,
            )

        max_attempts = 1 + max(0, self.max_retries)

        for attempt in range(max_attempts):
            start_time = time.monotonic()
            req_id: str | None = None
            status_code: int | None = None
            try:
                auth_header = await self._get_auth_header(operation)
                headers = {
                    "Accept": "application/vnd.github.v3+json",
                    "Authorization": auth_header,
                    "X-GitHub-Api-Version": "2022-11-28",
                }

                if method.upper() == "GET":
                    response = await self._client.get(url, headers=headers)
                elif method.upper() == "POST":
                    response = await self._client.post(url, headers=headers, json=json)
                else:
                    response = await self._client.request(
                        method, url, headers=headers, json=json
                    )

                duration_ms = int((time.monotonic() - start_time) * 1000)
                req_id = _safe_header_get(response, "x-github-request-id")
                status_code = getattr(response, "status_code", 200)

                if 200 <= status_code < 300:
                    logger.debug(
                        "GitHub request succeeded: %s %s (status=%s, attempt=%s, duration_ms=%s, req_id=%s)",
                        method,
                        operation,
                        status_code,
                        attempt + 1,
                        duration_ms,
                        req_id,
                    )
                    return response

                # Non-2xx response handling
                if status_code == 401:
                    if (
                        self.mode == CredentialMode.INSTALLATION
                        and self.installation_id is not None
                    ):
                        self._token_service.invalidate(self.installation_id)
                    raise GitHubAuthenticationError(
                        "GitHub authentication failed (invalid token).",
                        status_code=401,
                        operation=operation,
                        github_request_id=req_id,
                    )

                if status_code == 403:
                    if _safe_header_get(response, "x-ratelimit-remaining") == "0":
                        retry_after_str = _safe_header_get(response, "retry-after")
                        retry_after = (
                            int(retry_after_str)
                            if retry_after_str and retry_after_str.isdigit()
                            else None
                        )
                        if (
                            retry_after is not None
                            and retry_after <= self.max_retry_after
                            and attempt < max_attempts - 1
                        ):
                            logger.warning(
                                "GitHub rate limit reached for %s; retrying after %ss (attempt %s/%s)",
                                operation,
                                retry_after,
                                attempt + 1,
                                max_attempts,
                            )
                            await self._sleep(float(retry_after))
                            continue
                        raise GitHubRateLimitError(
                            "GitHub API rate limit exceeded.",
                            status_code=403,
                            operation=operation,
                            github_request_id=req_id,
                            retry_after=retry_after,
                            retryable=True,
                        )
                    raise GitHubAuthorizationError(
                        "GitHub authorization failed (insufficient permissions).",
                        status_code=403,
                        operation=operation,
                        github_request_id=req_id,
                    )

                if status_code == 404:
                    raise GitHubNotFoundError(
                        "Resource not found on GitHub.",
                        status_code=404,
                        operation=operation,
                        github_request_id=req_id,
                    )

                if status_code == 409:
                    raise GitHubConflictError(
                        "GitHub conflict error (409).",
                        status_code=409,
                        operation=operation,
                        github_request_id=req_id,
                    )

                if status_code == 422:
                    raise GitHubValidationError(
                        "GitHub validation error (422).",
                        status_code=422,
                        operation=operation,
                        github_request_id=req_id,
                    )

                if status_code == 429:
                    retry_after_str = _safe_header_get(response, "retry-after")
                    retry_after = (
                        int(retry_after_str)
                        if retry_after_str and retry_after_str.isdigit()
                        else None
                    )
                    if (
                        retry_after is not None
                        and retry_after <= self.max_retry_after
                        and attempt < max_attempts - 1
                    ):
                        logger.warning(
                            "GitHub 429 Too Many Requests for %s; retrying after %ss (attempt %s/%s)",
                            operation,
                            retry_after,
                            attempt + 1,
                            max_attempts,
                        )
                        await self._sleep(float(retry_after))
                        continue
                    raise GitHubRateLimitError(
                        "GitHub API rate limit exceeded (429).",
                        status_code=429,
                        operation=operation,
                        github_request_id=req_id,
                        retry_after=retry_after,
                        retryable=True,
                    )

                if status_code >= 500:
                    if attempt < max_attempts - 1:
                        backoff = self.retry_backoff_factor * (2**attempt)
                        logger.warning(
                            "GitHub upstream error %s on %s; retrying in %ss (attempt %s/%s)",
                            status_code,
                            operation,
                            backoff,
                            attempt + 1,
                            max_attempts,
                        )
                        await self._sleep(backoff)
                        continue
                    raise GitHubUpstreamError(
                        f"GitHub upstream error: {status_code}",
                        status_code=status_code,
                        operation=operation,
                        github_request_id=req_id,
                        retryable=True,
                    )

                raise GitHubError(
                    f"GitHub API error: {status_code}",
                    status_code=status_code,
                    operation=operation,
                    github_request_id=req_id,
                )

            except (
                GitHubAuthenticationError,
                GitHubAuthorizationError,
                GitHubNotFoundError,
                GitHubConflictError,
                GitHubValidationError,
            ):
                raise

            except (GitHubRateLimitError, GitHubUpstreamError, GitHubError):
                raise

            except httpx.TimeoutException as exc:
                duration_ms = int((time.monotonic() - start_time) * 1000)
                if attempt < max_attempts - 1:
                    backoff = self.retry_backoff_factor * (2**attempt)
                    logger.warning(
                        "Request to GitHub timed out on %s (duration_ms=%s); retrying in %ss (attempt %s/%s)",
                        operation,
                        duration_ms,
                        backoff,
                        attempt + 1,
                        max_attempts,
                    )
                    await self._sleep(backoff)
                    continue
                raise GitHubTimeoutError(
                    "Request to GitHub timed out",
                    operation=operation,
                    retryable=True,
                ) from exc

            except (
                httpx.NetworkError,
                httpx.TransportError,
                httpx.ConnectError,
                httpx.RequestError,
            ) as exc:
                duration_ms = int((time.monotonic() - start_time) * 1000)
                if attempt < max_attempts - 1:
                    backoff = self.retry_backoff_factor * (2**attempt)
                    logger.warning(
                        "Request to GitHub failed with %s on %s; retrying in %ss (attempt %s/%s)",
                        type(exc).__name__,
                        operation,
                        backoff,
                        attempt + 1,
                        max_attempts,
                    )
                    await self._sleep(backoff)
                    continue
                raise GitHubConnectionError(
                    f"Request to GitHub failed: {exc}",
                    operation=operation,
                    retryable=True,
                ) from exc

        # Unreachable fallback
        raise GitHubError("Exhausted retries without response", operation=operation)

    async def get_repository(self, owner: str, repo: str) -> GitHubRepository:
        """Fetch repository metadata."""
        _validate_path_segment(owner, "owner")
        _validate_path_segment(repo, "repo")

        logger.info("GitHubClient: fetching repository metadata for %s/%s", owner, repo)
        path = f"/repos/{owner}/{repo}"
        response = await self._request("GET", path, operation="get_repository")

        data = await _safe_json(response)
        return GitHubRepository(
            id=data.get("id"),
            owner=data["owner"]["login"],
            name=data["name"],
            full_name=data["full_name"],
            default_branch=data.get("default_branch", "main"),
            private=data.get("private", False),
            clone_url=data.get("clone_url", ""),
            html_url=data.get("html_url", ""),
        )

    async def get_branch(self, owner: str, repo: str, branch: str) -> GitHubBranch:
        """Fetch branch information."""
        _validate_path_segment(owner, "owner")
        _validate_path_segment(repo, "repo")
        _validate_branch_name(branch)

        logger.info("GitHubClient: fetching branch %s for %s/%s", branch, owner, repo)
        path = f"/repos/{owner}/{repo}/branches/{branch}"
        response = await self._request("GET", path, operation="get_branch")

        data = await _safe_json(response)
        return GitHubBranch(
            name=data["name"],
            sha=data["commit"]["sha"],
        )

    async def create_branch(self, request: CreateBranchRequest) -> GitHubBranch:
        """Create a new branch from a base SHA."""
        _validate_path_segment(request.owner, "owner")
        _validate_path_segment(request.repo, "repo")
        _validate_branch_name(request.branch_name)

        logger.info(
            "GitHubClient: creating branch %s for %s/%s",
            request.branch_name,
            request.owner,
            request.repo,
        )
        path = f"/repos/{request.owner}/{request.repo}/git/refs"
        payload = {
            "ref": f"refs/heads/{request.branch_name}",
            "sha": request.base_sha,
        }
        response = await self._request(
            "POST", path, json=payload, operation="create_branch"
        )

        data = await _safe_json(response)
        return GitHubBranch(
            name=request.branch_name,
            sha=data["object"]["sha"],
        )

    async def create_pull_request(
        self, request: CreatePullRequestRequest
    ) -> GitHubPullRequest:
        """Create a new pull request."""
        _validate_path_segment(request.owner, "owner")
        _validate_path_segment(request.repo, "repo")

        logger.info(
            "GitHubClient: creating PR from %s to %s for %s/%s",
            request.head_branch,
            request.base_branch,
            request.owner,
            request.repo,
        )
        path = f"/repos/{request.owner}/{request.repo}/pulls"
        payload = {
            "title": request.title,
            "body": request.body,
            "head": request.head_branch,
            "base": request.base_branch,
        }
        response = await self._request(
            "POST", path, json=payload, operation="create_pull_request"
        )

        data = await _safe_json(response)
        return GitHubPullRequest(
            number=data["number"],
            title=data["title"],
            body=data.get("body", ""),
            head_branch=data["head"]["ref"],
            base_branch=data["base"]["ref"],
            html_url=data["html_url"],
            state=data["state"],
        )

    async def get_collaborator_permission(
        self, owner: str, repo: str, username: str
    ) -> GitHubCollaboratorPermission:
        """Fetch collaborator permission level for a repository."""
        _validate_path_segment(owner, "owner")
        _validate_path_segment(repo, "repo")
        _validate_path_segment(username, "username")

        logger.info(
            "GitHubClient: fetching collaborator permission for %s in %s/%s",
            username,
            owner,
            repo,
        )
        path = f"/repos/{owner}/{repo}/collaborators/{username}/permission"
        try:
            response = await self._request(
                "GET", path, operation="get_collaborator_permission"
            )
        except (GitHubNotFoundError, GitHubAuthorizationError):
            # GitHub returns 404 if the user is not a collaborator
            return GitHubCollaboratorPermission(
                username=username,
                permission="none",
            )

        data = await _safe_json(response)
        permission = data.get("permission", "none")
        role_name = data.get("role_name")
        user_obj = data.get("user") or {}
        user_id = user_obj.get("id")
        perms = user_obj.get("permissions") or {}

        return GitHubCollaboratorPermission(
            username=username,
            permission=permission,
            role_name=role_name,
            user_id=user_id,
            can_admin=bool(perms.get("admin")),
            can_maintain=bool(perms.get("maintain")),
            can_push=bool(perms.get("push")),
            can_triage=bool(perms.get("triage")),
            can_pull=bool(perms.get("pull")),
        )

    async def create_installation_access_token(self, installation_id: int) -> str:
        """Create an installation access token using GitHub App JWT authentication."""
        if self.mode == CredentialMode.INSTALLATION:
            raise GitHubConfigurationError(
                "Cannot create installation access token using an installation token; App JWT required",
                operation="create_installation_access_token",
            )

        valid_id = validate_installation_id(installation_id)
        logger.info(
            "GitHubClient: fetching access token for installation %s", valid_id
        )
        path = f"/app/installations/{valid_id}/access_tokens"
        response = await self._request(
            "POST", path, operation="create_installation_access_token"
        )

        data = await _safe_json(response)
        return str(data["token"])
