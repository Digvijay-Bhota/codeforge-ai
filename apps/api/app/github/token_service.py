"""GitHub App Installation Token Service.

Phase 10B.2.1: Authoritative installation-token lifecycle with process-local,
memory-only caching, single-flight concurrency, and typed GitHub errors.

CF-06: every token is scoped.  A token request names exactly one repository
and an explicit, minimal permission set (TokenScope); there is no way to ask
for an installation-wide token.  Tokens are cached per (installation,
repository, permissions), so a token minted for one repository or permission
set is never returned for another.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import settings
from app.github.app_auth import GitHubAppAuth, GitHubAppAuthError
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
from app.observability.events import EventType
from app.observability.tracing import record_event

logger = logging.getLogger(__name__)


def validate_installation_id(installation_id: Any) -> int:
    """Validate that installation_id is a positive integer.

    Rejects booleans, non-integers, zero, and negative values.

    Args:
        installation_id: Value to validate.

    Returns:
        The validated integer installation ID.

    Raises:
        ValueError: If installation_id is not a positive integer.
    """
    if (
        isinstance(installation_id, bool)
        or not isinstance(installation_id, int)
        or installation_id <= 0
    ):
        raise ValueError(
            f"Invalid installation_id: {installation_id!r}. Must be a positive integer."
        )
    return int(installation_id)


# Repository permissions a CodeForge token may request, and their allowed
# access levels.  Anything else (administration, workflows, secrets, ...) is
# refused before a request is made.
ALLOWED_TOKEN_PERMISSIONS: dict[str, frozenset[str]] = {
    "contents": frozenset({"read", "write"}),
    "metadata": frozenset({"read"}),
    "pull_requests": frozenset({"read", "write"}),
    "checks": frozenset({"read", "write"}),
    "issues": frozenset({"read", "write"}),
}

_REPOSITORY_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}")


@dataclass(frozen=True)
class TokenScope:
    """What one installation token may access: one repository, explicit permissions.

    Build it with for_repository_id (the normal case) or for_repository_name
    (only to look up a repository's ID).  The permissions are validated
    against ALLOWED_TOKEN_PERMISSIONS and stored sorted, so equal scopes
    compare and hash equal.
    """

    permissions: tuple[tuple[str, str], ...]
    repository_id: int | None = None
    repository_name: str | None = None

    def __post_init__(self) -> None:
        if (self.repository_id is None) == (self.repository_name is None):
            raise ValueError("A token scope names exactly one repository, by ID or by name.")
        if self.repository_id is not None and (
            isinstance(self.repository_id, bool)
            or not isinstance(self.repository_id, int)
            or self.repository_id <= 0
        ):
            raise ValueError(f"Invalid repository_id: {self.repository_id!r}")
        if self.repository_name is not None and (
            not isinstance(self.repository_name, str)
            or not _REPOSITORY_NAME.fullmatch(self.repository_name)
            or self.repository_name in (".", "..")
        ):
            raise ValueError(f"Invalid repository name: {self.repository_name!r}")
        if not self.permissions:
            raise ValueError("A token scope needs at least one permission.")
        names = [name for name, _ in self.permissions]
        if list(self.permissions) != sorted(self.permissions) or len(set(names)) != len(names):
            raise ValueError("Token permissions must be unique and sorted.")
        for name, level in self.permissions:
            if level not in ALLOWED_TOKEN_PERMISSIONS.get(name, frozenset()):
                raise ValueError(f"Token permission not allowed: {name}={level}")

    @staticmethod
    def _normalize(permissions: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(permissions.items()))

    @classmethod
    def for_repository_id(cls, repository_id: int, permissions: Mapping[str, str]) -> TokenScope:
        return cls(permissions=cls._normalize(permissions), repository_id=repository_id)

    @classmethod
    def for_repository_name(cls, repository_name: str, permissions: Mapping[str, str]) -> TokenScope:
        return cls(permissions=cls._normalize(permissions), repository_name=repository_name)

    @property
    def repository_key(self) -> tuple[str, Any]:
        if self.repository_id is not None:
            return ("id", self.repository_id)
        assert self.repository_name is not None  # nosec B101 - enforced in __post_init__
        return ("name", self.repository_name.lower())

    def request_body(self) -> dict[str, Any]:
        """The access_tokens request body: one repository and these permissions only."""
        body: dict[str, Any] = {"permissions": dict(self.permissions)}
        if self.repository_id is not None:
            body["repository_ids"] = [self.repository_id]
        else:
            body["repositories"] = [self.repository_name]
        return body

    def describe(self) -> str:
        """Non-secret description for logs."""
        repository = f"{self.repository_key[0]}={self.repository_key[1]}"
        permissions = ",".join(f"{name}:{level}" for name, level in self.permissions)
        return f"{repository} permissions={permissions}"


CacheKey = tuple[int, tuple[str, Any], tuple[tuple[str, str], ...]]


def _cache_key(installation_id: int, scope: TokenScope) -> CacheKey:
    return (installation_id, scope.repository_key, scope.permissions)


@dataclass(frozen=True)
class CachedInstallationToken:
    """In-memory representation of a cached GitHub App installation access token.

    Note:
        Process-local only. Redacts token on __repr__ and __str__ to prevent accidental leaks.
    """

    token: str
    installation_id: int
    expires_at: datetime  # Timezone-aware UTC datetime
    scope: TokenScope | None = None
    # The one repository GitHub reported granting (CF-06).
    repository_id: int | None = None
    repository_full_name: str | None = None

    def __repr__(self) -> str:
        scope = self.scope.describe() if self.scope else "unscoped"
        return (
            f"CachedInstallationToken(installation_id={self.installation_id}, "
            f"scope={scope!r}, repository_id={self.repository_id}, "
            f"token='[REDACTED]', expires_at={self.expires_at.isoformat()})"
        )

    def __str__(self) -> str:
        return self.__repr__()


class InstallationTokenService:
    """Service managing the lifecycle and process-local cache of GitHub App installation tokens.

    Provides:
    - In-memory process-local caching (zero persistence, never written to disk/DB/Redis).
    - Single-flight concurrency per installation ID to avoid redundant token generation.
    - Automatic refresh when remaining token lifetime is within the refresh safety buffer.
    - Explicit invalidation for 401 handling.
    - Strict redaction: tokens are never included in logs or exceptions.
    """

    def __init__(
        self,
        refresh_buffer_seconds: int | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialize the InstallationTokenService.

        Args:
            refresh_buffer_seconds: Seconds before expiration to trigger a fresh mint.
                Defaults to settings.github_token_refresh_buffer_seconds (300 seconds).
            http_client: Optional httpx.AsyncClient for HTTP requests (useful for mocking/testing).
        """
        if refresh_buffer_seconds is None:
            self.refresh_buffer_seconds = settings.github_token_refresh_buffer_seconds
        else:
            self.refresh_buffer_seconds = refresh_buffer_seconds

        self._http_client = http_client
        self._cache: dict[CacheKey, CachedInstallationToken] = {}
        self._locks: dict[CacheKey, asyncio.Lock] = {}
        self._master_lock = asyncio.Lock()

    def _is_token_valid(self, cached: CachedInstallationToken) -> bool:
        """Check if a cached token exists and has lifetime exceeding the refresh buffer."""
        now = datetime.now(UTC)
        expires_at = cached.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        remaining = (expires_at - now).total_seconds()
        return remaining > self.refresh_buffer_seconds

    async def _get_scope_lock(self, key: CacheKey) -> asyncio.Lock:
        """Safely retrieve or create an asyncio.Lock dedicated to one cache key."""
        async with self._master_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    async def get_token(self, installation_id: int, scope: TokenScope) -> str:
        """Retrieve a valid installation access token for one repository and permission set.

        The token of get_grant(); see there.
        """
        return (await self.get_grant(installation_id, scope)).token

    async def get_grant(self, installation_id: int, scope: TokenScope) -> CachedInstallationToken:
        """Retrieve a valid installation token for one repository and permission set, with what GitHub granted.

        The result names the one repository GitHub reported the token covers
        (repository_id, repository_full_name), verified against *scope*.

        If a valid cached token exists for exactly this installation, repository
        and permission set, with remaining lifetime exceeding the refresh buffer,
        it is returned immediately. Otherwise, a fresh token is minted using the
        GitHub App JWT. Single-flight concurrency guarantees that concurrent calls
        for the same scope trigger exactly one upstream mint.

        Args:
            installation_id: GitHub App installation ID (positive integer).
            scope: The repository and permissions the token is limited to.

        Returns:
            The cached token entry (its token is the plain access token string).

        Raises:
            ValueError: If installation_id is not a positive integer.
            GitHubConfigurationError: If GitHub App is unconfigured or JWT fails.
            GitHubAuthenticationError: If App JWT is rejected or response is malformed.
            GitHubAuthorizationError: If permissions are insufficient, or the grant
                is not exactly the requested scope.
            GitHubNotFoundError: If installation does not exist.
            GitHubTimeoutError: If GitHub times out.
            GitHubConnectionError: If network connection fails.
            GitHubUpstreamError: If GitHub returns a 5xx error.
            GitHubError: For other unexpected GitHub errors.
        """
        valid_id = validate_installation_id(installation_id)
        if not isinstance(scope, TokenScope):
            raise ValueError("An installation token requires an explicit TokenScope.")
        key = _cache_key(valid_id, scope)

        # Fast path: check in-memory cache without locking
        cached = self._cache.get(key)
        if cached is not None and self._is_token_valid(cached):
            return cached

        # Acquire per-scope lock to avoid dogpiling / concurrent redundant mints
        lock = await self._get_scope_lock(key)
        async with lock:
            # Double-check cache under lock
            cached = self._cache.get(key)
            if cached is not None and self._is_token_valid(cached):
                return cached
            # An expired or near-expiry entry is never reused.
            self._cache.pop(key, None)

            # Mint fresh installation token
            return await self._mint_installation_token(valid_id, scope)

    def invalidate(self, installation_id: int, scope: TokenScope | None = None) -> None:
        """Evict cached tokens: for one scope, or every scope of the installation.

        Args:
            installation_id: GitHub App installation ID (positive integer).
            scope: Evict only this scope's token; None evicts all of the installation's.

        Raises:
            ValueError: If installation_id is not a positive integer.
        """
        valid_id = validate_installation_id(installation_id)
        keys = (
            [_cache_key(valid_id, scope)]
            if scope is not None
            else [key for key in self._cache if key[0] == valid_id]
        )
        evicted = [self._cache.pop(key, None) for key in keys]
        if any(entry is not None for entry in evicted):
            logger.info(
                "Invalidated cached installation token for installation_id=%s",
                valid_id,
            )
        else:
            logger.debug(
                "Invalidate called for installation_id=%s but no cached token existed",
                valid_id,
            )

    async def _mint_installation_token(self, installation_id: int, scope: TokenScope) -> CachedInstallationToken:
        """Internal helper to mint a scoped installation token via GitHub App JWT."""
        operation = "mint_installation_token"

        if not GitHubAppAuth.is_configured():
            raise GitHubConfigurationError(
                "GitHub App is not configured. Missing github_app_id or github_app_private_key.",
                operation=operation,
            )

        try:
            app_jwt = GitHubAppAuth.generate_jwt()
        except GitHubAppAuthError as exc:
            raise GitHubConfigurationError(
                f"Failed to generate GitHub App JWT: {exc}",
                operation=operation,
            ) from exc

        url = f"{settings.github_api_url.rstrip('/')}/app/installations/{installation_id}/access_tokens"
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"Bearer {app_jwt}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        start_time = time.monotonic()
        try:
            body = scope.request_body()
            if self._http_client is not None:
                response = await self._http_client.post(url, headers=headers, json=body)
            else:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.error(
                "Timeout connecting to GitHub for installation_id=%s (duration_ms=%s)",
                installation_id,
                duration_ms,
            )
            try:
                await record_event(
                    event_type=EventType.GITHUB_OPERATION_FAILED,
                    component="installation_token_service",
                    duration_ms=duration_ms,
                    metadata={
                        "installation_id": installation_id,
                        "operation": operation,
                        "error": "timeout",
                    },
                )
            except Exception as e:
                logger.debug("Telemetry recording skipped: %s", e)
            raise GitHubTimeoutError(
                "Request to GitHub timed out while minting installation token",
                operation=operation,
                retryable=True,
            ) from exc
        except (httpx.NetworkError, httpx.TransportError, httpx.RequestError) as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.error(
                "Network error connecting to GitHub for installation_id=%s: %s (duration_ms=%s)",
                installation_id,
                type(exc).__name__,
                duration_ms,
            )
            try:
                await record_event(
                    event_type=EventType.GITHUB_OPERATION_FAILED,
                    component="installation_token_service",
                    duration_ms=duration_ms,
                    metadata={
                        "installation_id": installation_id,
                        "operation": operation,
                        "error": type(exc).__name__,
                    },
                )
            except Exception as e:
                logger.debug("Telemetry recording skipped: %s", e)
            raise GitHubConnectionError(
                f"Failed to connect to GitHub while minting installation token: {type(exc).__name__}",
                operation=operation,
                retryable=True,
            ) from exc

        duration_ms = int((time.monotonic() - start_time) * 1000)
        return await self._process_token_response(
            response=response,
            installation_id=installation_id,
            operation=operation,
            duration_ms=duration_ms,
            scope=scope,
        )

    @staticmethod
    def _verify_granted_scope(data: dict[str, Any], scope: TokenScope) -> str | None:
        """Why the minted token is not limited to *scope*, or None.

        GitHub reports what it granted, and the report is required: a response
        that does not say it covers exactly one selected repository, which
        one (ID and full name), and which permissions, is refused.  A token
        broader than requested (other repositories, all repositories, or
        extra permissions) is refused.  An extra read-only "metadata" grant is
        tolerated: it only exposes repository metadata, and GitHub may include
        it for any app with repository permissions.  A requested permission
        that was not granted is refused too, so a misconfigured app fails
        here, clearly.
        """
        selection = data.get("repository_selection")
        if selection != "selected":
            return f"repository_selection={selection!r}"
        repositories = data.get("repositories")
        if not isinstance(repositories, list):
            return "granted repositories not reported"
        if len(repositories) != 1:
            return "token covers more than one repository" if repositories else "token covers no repository"
        entry = repositories[0] if isinstance(repositories[0], dict) else {}
        repository_id = entry.get("id")
        full_name = entry.get("full_name")
        if (
            isinstance(repository_id, bool)
            or not isinstance(repository_id, int)
            or repository_id <= 0
            or not isinstance(full_name, str)
            or full_name.count("/") != 1
            or not all(full_name.split("/"))
        ):
            return "granted repository not identified"
        if scope.repository_id is not None and repository_id != scope.repository_id:
            return "token covers a different repository"
        if scope.repository_name is not None and (
            str(entry.get("name", "")).lower() != scope.repository_name.lower()
            or full_name.split("/")[1].lower() != scope.repository_name.lower()
        ):
            return "token covers a different repository"
        granted = data.get("permissions")
        if not isinstance(granted, dict):
            return "granted permissions not reported"
        requested = dict(scope.permissions)
        for name, level in granted.items():
            if requested.get(name) != level and not (name == "metadata" and level == "read"):
                return f"unrequested permission {name}={level}"
        for name, level in requested.items():
            if granted.get(name) != level:
                return f"requested permission {name}={level} was not granted"
        return None

    async def _process_token_response(
        self,
        response: httpx.Response,
        installation_id: int,
        operation: str,
        duration_ms: int,
        scope: TokenScope,
    ) -> CachedInstallationToken:
        """Validate response from GitHub and update process-local cache."""
        req_id = response.headers.get("x-github-request-id")
        status = response.status_code

        if status in (200, 201):
            try:
                data = response.json()
                if asyncio.iscoroutine(data):
                    data = await data
            except Exception as exc:
                raise GitHubAuthenticationError(
                    "Invalid JSON response from GitHub installation access token endpoint",
                    status_code=status,
                    operation=operation,
                    github_request_id=req_id,
                ) from exc

            raw_token = data.get("token")
            if not raw_token or not isinstance(raw_token, str):
                raise GitHubAuthenticationError(
                    "Malformed response from GitHub: missing or invalid 'token' field",
                    status_code=status,
                    operation=operation,
                    github_request_id=req_id,
                )

            raw_expires_at = data.get("expires_at")
            if not raw_expires_at or not isinstance(raw_expires_at, str):
                raise GitHubAuthenticationError(
                    "Malformed response from GitHub: missing or invalid 'expires_at' field",
                    status_code=status,
                    operation=operation,
                    github_request_id=req_id,
                )

            try:
                expires_str = raw_expires_at.replace("Z", "+00:00")
                parsed_dt = datetime.fromisoformat(expires_str)
                if parsed_dt.tzinfo is None:
                    parsed_dt = parsed_dt.replace(tzinfo=UTC)
                else:
                    parsed_dt = parsed_dt.astimezone(UTC)
            except Exception as exc:
                raise GitHubAuthenticationError(
                    "Malformed response from GitHub: unparseable 'expires_at' timestamp",
                    status_code=status,
                    operation=operation,
                    github_request_id=req_id,
                ) from exc

            problem = self._verify_granted_scope(data, scope)
            if problem is not None:
                logger.error(
                    "Refusing installation token for installation_id=%s (%s): %s",
                    installation_id,
                    scope.describe(),
                    problem,
                )
                raise GitHubAuthorizationError(
                    f"GitHub granted an installation token outside the requested scope: {problem}",
                    status_code=status,
                    operation=operation,
                    github_request_id=req_id,
                )

            [granted_repository] = data["repositories"]  # exactly one, verified above
            cached = CachedInstallationToken(
                token=raw_token,
                installation_id=installation_id,
                expires_at=parsed_dt,
                scope=scope,
                repository_id=granted_repository["id"],
                repository_full_name=granted_repository["full_name"],
            )
            self._cache[_cache_key(installation_id, scope)] = cached

            logger.info(
                "Minted installation access token for installation_id=%s %s (duration_ms=%s, expires_at=%s)",
                installation_id,
                scope.describe(),
                duration_ms,
                parsed_dt.isoformat(),
            )
            try:
                await record_event(
                    event_type=EventType.GITHUB_OPERATION_COMPLETED,
                    component="installation_token_service",
                    duration_ms=duration_ms,
                    metadata={
                        "installation_id": installation_id,
                        "operation": operation,
                        "status": "success",
                        "github_request_id": req_id,
                    },
                )
            except Exception as e:
                logger.debug("Telemetry recording skipped: %s", e)

            return cached

        # Non-success handling
        logger.error(
            "GitHub error minting installation token for installation_id=%s (status=%s, duration_ms=%s, request_id=%s)",
            installation_id,
            status,
            duration_ms,
            req_id,
        )
        try:
            await record_event(
                event_type=EventType.GITHUB_OPERATION_FAILED,
                component="installation_token_service",
                duration_ms=duration_ms,
                metadata={
                    "installation_id": installation_id,
                    "operation": operation,
                    "status_code": status,
                    "github_request_id": req_id,
                },
            )
        except Exception as e:
            logger.debug("Telemetry recording skipped: %s", e)

        if status == 401:
            raise GitHubAuthenticationError(
                "GitHub App authentication failed: invalid App JWT or credentials rejected",
                status_code=status,
                operation=operation,
                github_request_id=req_id,
            )
        if status == 403:
            if response.headers.get("x-ratelimit-remaining") == "0":
                retry_after_str = response.headers.get("retry-after")
                retry_after = (
                    int(retry_after_str) if retry_after_str and retry_after_str.isdigit() else None
                )
                raise GitHubRateLimitError(
                    "GitHub API rate limit exceeded during installation token generation",
                    status_code=status,
                    operation=operation,
                    github_request_id=req_id,
                    retryable=True,
                    retry_after=retry_after,
                )
            raise GitHubAuthorizationError(
                "GitHub authorization failed: insufficient permissions for installation",
                status_code=status,
                operation=operation,
                github_request_id=req_id,
            )
        if status == 404:
            raise GitHubNotFoundError(
                f"GitHub installation {installation_id} not found",
                status_code=status,
                operation=operation,
                github_request_id=req_id,
            )
        if status == 409:
            raise GitHubConflictError(
                "GitHub conflict during installation token generation",
                status_code=status,
                operation=operation,
                github_request_id=req_id,
            )
        if status == 422:
            raise GitHubValidationError(
                "GitHub validation error during installation token generation",
                status_code=status,
                operation=operation,
                github_request_id=req_id,
            )
        if status == 429:
            # Secondary rate limit; transient, as GitHubClient treats it.
            retry_after_str = response.headers.get("retry-after")
            raise GitHubRateLimitError(
                "GitHub API rate limit exceeded (429) during installation token generation",
                status_code=status,
                operation=operation,
                github_request_id=req_id,
                retryable=True,
                retry_after=int(retry_after_str) if retry_after_str and retry_after_str.isdigit() else None,
            )
        if status >= 500:
            raise GitHubUpstreamError(
                f"GitHub upstream server error: {status}",
                status_code=status,
                operation=operation,
                github_request_id=req_id,
                retryable=True,
            )

        raise GitHubError(
            f"Unexpected response from GitHub: {status}",
            status_code=status,
            operation=operation,
            github_request_id=req_id,
        )


_default_token_service: InstallationTokenService | None = None


def get_installation_token_service() -> InstallationTokenService:
    """Get or create the process-wide InstallationTokenService instance."""
    global _default_token_service
    if _default_token_service is None:
        _default_token_service = InstallationTokenService()
    return _default_token_service
