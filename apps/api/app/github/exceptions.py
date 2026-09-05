"""GitHub integration specific exceptions."""

class GitHubError(Exception):
    """Base exception for all GitHub integration errors."""

class GitHubAuthenticationError(GitHubError):
    """Raised when GitHub authentication fails (e.g., invalid token)."""

class GitHubAuthorizationError(GitHubError):
    """Raised when the provided token lacks required permissions."""

class GitHubNotFoundError(GitHubError):
    """Raised when a GitHub resource (repo, branch, etc.) is not found."""

class GitHubValidationError(GitHubError):
    """Raised when GitHub returns a 422 Unprocessable Entity or similar bad request."""

class GitHubRateLimitError(GitHubError):
    """Raised when GitHub rate limit is exceeded."""

class GitHubUpstreamError(GitHubError):
    """Raised for 5xx errors from GitHub."""

class GitHubTimeoutError(GitHubError):
    """Raised when a request to GitHub times out."""

class GitHubConfigurationError(GitHubError):
    """Raised when GitHub integration is improperly configured (e.g., missing token)."""
