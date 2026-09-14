"""GitHub integration specific exceptions.

Phase 10B.2.1: Typed GitHub error hierarchy with safe metadata and no credential leakage.
"""

from __future__ import annotations


class GitHubError(Exception):
    """Base exception for all GitHub integration errors."""

    def __init__(
        self,
        message: str = "GitHub integration error",
        *,
        status_code: int | None = None,
        operation: str | None = None,
        github_request_id: str | None = None,
        retryable: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.operation = operation
        self.github_request_id = github_request_id
        self.retryable = retryable
        self.retry_after = retry_after

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"message={self.message!r}, "
            f"status_code={self.status_code!r}, "
            f"operation={self.operation!r}, "
            f"github_request_id={self.github_request_id!r}, "
            f"retryable={self.retryable!r}, "
            f"retry_after={self.retry_after!r})"
        )


class GitHubAuthenticationError(GitHubError):
    """Raised when GitHub authentication fails (401 Unauthorized, invalid token/credentials)."""

    def __init__(
        self,
        message: str = "GitHub authentication failed (invalid token).",
        *,
        status_code: int | None = 401,
        operation: str | None = None,
        github_request_id: str | None = None,
        retryable: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            operation=operation,
            github_request_id=github_request_id,
            retryable=retryable,
            retry_after=retry_after,
        )


class GitHubAuthorizationError(GitHubError):
    """Raised when the provided token lacks required permissions (403 Forbidden)."""

    def __init__(
        self,
        message: str = "GitHub authorization failed (insufficient permissions).",
        *,
        status_code: int | None = 403,
        operation: str | None = None,
        github_request_id: str | None = None,
        retryable: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            operation=operation,
            github_request_id=github_request_id,
            retryable=retryable,
            retry_after=retry_after,
        )


class GitHubNotFoundError(GitHubError):
    """Raised when a GitHub resource (repo, branch, etc.) is not found (404 Not Found)."""

    def __init__(
        self,
        message: str = "Resource not found on GitHub.",
        *,
        status_code: int | None = 404,
        operation: str | None = None,
        github_request_id: str | None = None,
        retryable: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            operation=operation,
            github_request_id=github_request_id,
            retryable=retryable,
            retry_after=retry_after,
        )


class GitHubConflictError(GitHubError):
    """Raised when GitHub returns a 409 Conflict (e.g. branch or reference collision)."""

    def __init__(
        self,
        message: str = "GitHub conflict error (409).",
        *,
        status_code: int | None = 409,
        operation: str | None = None,
        github_request_id: str | None = None,
        retryable: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            operation=operation,
            github_request_id=github_request_id,
            retryable=retryable,
            retry_after=retry_after,
        )


class GitHubValidationError(GitHubError):
    """Raised when GitHub returns a 422 Unprocessable Entity or similar validation failure."""

    def __init__(
        self,
        message: str = "GitHub validation error (422).",
        *,
        status_code: int | None = 422,
        operation: str | None = None,
        github_request_id: str | None = None,
        retryable: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            operation=operation,
            github_request_id=github_request_id,
            retryable=retryable,
            retry_after=retry_after,
        )


class GitHubRateLimitError(GitHubError):
    """Raised when GitHub API rate limit is exceeded."""

    def __init__(
        self,
        message: str = "GitHub API rate limit exceeded.",
        *,
        status_code: int | None = 403,
        operation: str | None = None,
        github_request_id: str | None = None,
        retryable: bool = True,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            operation=operation,
            github_request_id=github_request_id,
            retryable=retryable,
            retry_after=retry_after,
        )


class GitHubTransientError(GitHubError):
    """Base class for transient/recoverable errors from GitHub (network, timeouts, 5xx)."""

    def __init__(
        self,
        message: str = "GitHub transient error",
        *,
        status_code: int | None = None,
        operation: str | None = None,
        github_request_id: str | None = None,
        retryable: bool = True,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            operation=operation,
            github_request_id=github_request_id,
            retryable=retryable,
            retry_after=retry_after,
        )


class GitHubTimeoutError(GitHubTransientError):
    """Raised when a request to GitHub times out."""

    def __init__(
        self,
        message: str = "Request to GitHub timed out",
        *,
        status_code: int | None = None,
        operation: str | None = None,
        github_request_id: str | None = None,
        retryable: bool = True,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            operation=operation,
            github_request_id=github_request_id,
            retryable=retryable,
            retry_after=retry_after,
        )


class GitHubConnectionError(GitHubTransientError):
    """Raised when network connection to GitHub fails."""

    def __init__(
        self,
        message: str = "Failed to connect to GitHub",
        *,
        status_code: int | None = None,
        operation: str | None = None,
        github_request_id: str | None = None,
        retryable: bool = True,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            operation=operation,
            github_request_id=github_request_id,
            retryable=retryable,
            retry_after=retry_after,
        )


class GitHubUpstreamError(GitHubTransientError):
    """Raised for 5xx server errors from GitHub."""

    def __init__(
        self,
        message: str = "GitHub upstream error",
        *,
        status_code: int | None = 500,
        operation: str | None = None,
        github_request_id: str | None = None,
        retryable: bool = True,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            operation=operation,
            github_request_id=github_request_id,
            retryable=retryable,
            retry_after=retry_after,
        )


class GitHubConfigurationError(GitHubError):
    """Raised when GitHub integration is improperly configured (e.g., missing token or App credentials)."""

    def __init__(
        self,
        message: str = "GitHub integration is improperly configured",
        *,
        status_code: int | None = None,
        operation: str | None = None,
        github_request_id: str | None = None,
        retryable: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            operation=operation,
            github_request_id=github_request_id,
            retryable=retryable,
            retry_after=retry_after,
        )


class CommandConsumerError(GitHubError):
    """Base exception for outbox command consumer errors."""

    def __init__(
        self,
        message: str = "Command consumer error",
        *,
        retryable: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(message, retryable=retryable, **kwargs)


class MalformedCommandEventError(CommandConsumerError):
    """Raised when an outbox command event has an invalid/malformed payload (terminal failure)."""

    def __init__(self, message: str = "Malformed command event payload", **kwargs) -> None:
        super().__init__(message, retryable=False, **kwargs)


class UnresolvableIdentityError(CommandConsumerError):
    """Raised when the GitHub actor cannot be mapped to a known CodeForge user (terminal failure)."""

    def __init__(self, message: str = "GitHub actor could not be resolved to a CodeForge user", **kwargs) -> None:
        super().__init__(message, retryable=False, **kwargs)


class InactiveUserError(CommandConsumerError):
    """Raised when the mapped CodeForge user is inactive (terminal failure)."""

    def __init__(self, message: str = "CodeForge user is inactive", **kwargs) -> None:
        super().__init__(message, retryable=False, **kwargs)


class InvalidCommandActionError(CommandConsumerError):
    """Raised when the command action is invalid or unsupported (terminal failure)."""

    def __init__(self, message: str = "Invalid command action", **kwargs) -> None:
        super().__init__(message, retryable=False, **kwargs)


class TransientConsumerError(CommandConsumerError):
    """Raised when a transient downstream/database failure occurs (retryable)."""

    def __init__(self, message: str = "Transient consumer error", **kwargs) -> None:
        super().__init__(message, retryable=True, **kwargs)

