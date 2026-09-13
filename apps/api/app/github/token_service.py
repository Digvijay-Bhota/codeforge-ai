"""GitHub App Installation Token Service.

Phase 10B.2.1: Authoritative installation-token lifecycle with process-local,
memory-only caching, single-flight concurrency, and typed GitHub errors.
"""

from __future__ import annotations

import asyncio
import logging
import time
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


@dataclass(frozen=True)
class CachedInstallationToken:
    """In-memory representation of a cached GitHub App installation access token.

    Note:
        Process-local only. Redacts token on __repr__ and __str__ to prevent accidental leaks.
    """

    token: str
    installation_id: int
    expires_at: datetime  # Timezone-aware UTC datetime

    def __repr__(self) -> str:
        redacted = self.token[:4] + "..." + self.token[-4:] if len(self.token) >= 8 else "***"
        return (
            f"CachedInstallationToken(installation_id={self.installation_id}, "
            f"token={redacted!r}, expires_at={self.expires_at.isoformat()})"
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
        self._cache: dict[int, CachedInstallationToken] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._master_lock = asyncio.Lock()

    def _is_token_valid(self, cached: CachedInstallationToken) -> bool:
        """Check if a cached token exists and has lifetime exceeding the refresh buffer."""
        now = datetime.now(UTC)
        expires_at = cached.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        remaining = (expires_at - now).total_seconds()
        return remaining > self.refresh_buffer_seconds

    async def _get_installation_lock(self, installation_id: int) -> asyncio.Lock:
        """Safely retrieve or create an asyncio.Lock dedicated to a specific installation ID."""
        async with self._master_lock:
            if installation_id not in self._locks:
                self._locks[installation_id] = asyncio.Lock()
            return self._locks[installation_id]

    async def get_token(self, installation_id: int) -> str:
        """Retrieve a valid installation access token for the given installation ID.

        If a valid cached token exists with remaining lifetime exceeding the refresh buffer,
        it is returned immediately. Otherwise, a fresh token is minted using the GitHub App JWT.
        Single-flight concurrency guarantees that concurrent calls for the same installation
        trigger exactly one upstream mint.

        Args:
            installation_id: GitHub App installation ID (positive integer).

        Returns:
            The plain installation access token string.

        Raises:
            ValueError: If installation_id is not a positive integer.
            GitHubConfigurationError: If GitHub App is unconfigured or JWT fails.
            GitHubAuthenticationError: If App JWT is rejected or response is malformed.
            GitHubAuthorizationError: If permissions are insufficient.
            GitHubNotFoundError: If installation does not exist.
            GitHubTimeoutError: If GitHub times out.
            GitHubConnectionError: If network connection fails.
            GitHubUpstreamError: If GitHub returns a 5xx error.
            GitHubError: For other unexpected GitHub errors.
        """
        valid_id = validate_installation_id(installation_id)

        # Fast path: check in-memory cache without locking
        cached = self._cache.get(valid_id)
        if cached is not None and self._is_token_valid(cached):
            return cached.token

        # Acquire per-installation lock to avoid dogpiling / concurrent redundant mints
        lock = await self._get_installation_lock(valid_id)
        async with lock:
            # Double-check cache under lock
            cached = self._cache.get(valid_id)
            if cached is not None and self._is_token_valid(cached):
                return cached.token

            # Mint fresh installation token
            token = await self._mint_installation_token(valid_id)
            return token

    def invalidate(self, installation_id: int) -> None:
        """Invalidate and evict the cached token for the given installation ID.

        Args:
            installation_id: GitHub App installation ID (positive integer).

        Raises:
            ValueError: If installation_id is not a positive integer.
        """
        valid_id = validate_installation_id(installation_id)
        evicted = self._cache.pop(valid_id, None)
        if evicted is not None:
            logger.info(
                "Invalidated cached installation token for installation_id=%s",
                valid_id,
            )
        else:
            logger.debug(
                "Invalidate called for installation_id=%s but no cached token existed",
                valid_id,
            )

    async def _mint_installation_token(self, installation_id: int) -> str:
        """Internal helper to mint an installation token via GitHub App JWT."""
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
            if self._http_client is not None:
                response = await self._http_client.post(url, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.post(url, headers=headers)
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
        )

    async def _process_token_response(
        self,
        response: httpx.Response,
        installation_id: int,
        operation: str,
        duration_ms: int,
    ) -> str:
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

            cached = CachedInstallationToken(
                token=raw_token,
                installation_id=installation_id,
                expires_at=parsed_dt,
            )
            self._cache[installation_id] = cached

            logger.info(
                "Minted installation access token for installation_id=%s (duration_ms=%s, expires_at=%s)",
                installation_id,
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

            return str(raw_token)

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
