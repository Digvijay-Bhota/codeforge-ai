"""Phase 6A: GitHub Integration Foundation.

This package provides typed, authenticated access to GitHub operations.
It establishes the boundary between the internal CodeForge orchestration
and the external GitHub API, ensuring strict validation and separation
of concerns.
"""

from .client import GitHubClient
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
    GitHubCommit,
    GitHubPullRequest,
    GitHubRepository,
)
from .permissions import GitHubPermission, has_github_permission

__all__ = [
    "GitHubClient",
    "GitHubPermission",
    "has_github_permission",
    "GitHubRepository",
    "GitHubBranch",
    "GitHubCommit",
    "GitHubPullRequest",
    "CreateBranchRequest",
    "CreatePullRequestRequest",
    "GitHubError",
    "GitHubAuthenticationError",
    "GitHubAuthorizationError",
    "GitHubNotFoundError",
    "GitHubValidationError",
    "GitHubRateLimitError",
    "GitHubUpstreamError",
    "GitHubTimeoutError",
    "GitHubConfigurationError",
]
