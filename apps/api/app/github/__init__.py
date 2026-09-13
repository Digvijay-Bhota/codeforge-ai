"""Phase 6A: GitHub Integration Foundation.

This package provides typed, authenticated access to GitHub operations.
It establishes the boundary between the internal CodeForge orchestration
and the external GitHub API, ensuring strict validation and separation
of concerns.
"""

from .client import CredentialMode, GitHubClient
from .exceptions import (
    GitHubAuthenticationError,
    GitHubAuthorizationError,
    GitHubConfigurationError,
    GitHubConflictError,
    GitHubConnectionError,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubTimeoutError,
    GitHubTransientError,
    GitHubUpstreamError,
    GitHubValidationError,
)
from .models import (
    CreateBranchRequest,
    CreatePullRequestRequest,
    GitHubBranch,
    GitHubCollaboratorPermission,
    GitHubCommit,
    GitHubPullRequest,
    GitHubRepository,
)
from .permissions import GitHubPermission, has_github_permission
from .token_service import (
    CachedInstallationToken,
    InstallationTokenService,
    get_installation_token_service,
)

__all__ = [
    "CredentialMode",
    "GitHubClient",
    "GitHubPermission",
    "has_github_permission",
    "GitHubRepository",
    "GitHubBranch",
    "GitHubCommit",
    "GitHubPullRequest",
    "GitHubCollaboratorPermission",
    "CreateBranchRequest",
    "CreatePullRequestRequest",
    "GitHubError",
    "GitHubAuthenticationError",
    "GitHubAuthorizationError",
    "GitHubNotFoundError",
    "GitHubConflictError",
    "GitHubValidationError",
    "GitHubRateLimitError",
    "GitHubTransientError",
    "GitHubUpstreamError",
    "GitHubTimeoutError",
    "GitHubConnectionError",
    "GitHubConfigurationError",
    "CachedInstallationToken",
    "InstallationTokenService",
    "get_installation_token_service",
]
