"""Phase 6A: GitHub Integration Foundation.

This package provides typed, authenticated access to GitHub operations.
It establishes the boundary between the internal CodeForge orchestration
and the external GitHub API, ensuring strict validation and separation
of concerns.
"""

from .client import CredentialMode, GitHubClient
from .command_consumer import GitHubCommandConsumer
from .command_parser import (
    CodeForgeCommand,
    CodeForgeCommandType,
    CommandParser,
    InvalidCodeForgeCommand,
)
from .exceptions import (
    CommandConsumerError,
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
    InactiveUserError,
    InvalidCommandActionError,
    MalformedCommandEventError,
    TransientConsumerError,
    UnresolvableIdentityError,
)
from .models import (
    CheckRunOutput,
    CreateBranchRequest,
    CreateCheckRunRequest,
    CreateCommentRequest,
    CreatePullRequestRequest,
    GitHubBranch,
    GitHubCheckRun,
    GitHubCollaboratorPermission,
    GitHubComment,
    GitHubCommit,
    GitHubPullRequest,
    GitHubRepository,
    UpdateCheckRunRequest,
    UpdateCommentRequest,
    build_managed_comment_body,
    extract_managed_comment_marker,
)
from .permissions import GitHubPermission, has_github_permission
from .status_consumer import GitHubStatusConsumer
from .token_service import (
    CachedInstallationToken,
    InstallationTokenService,
    get_installation_token_service,
)
from .webhook_models import (
    CodeForgeCommandEvent,
    NormalizedIssueCommentPayload,
)
from .webhooks import (
    parse_issue_comment_payload,
    verify_signature,
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
    "CheckRunOutput",
    "GitHubCheckRun",
    "CreateCheckRunRequest",
    "UpdateCheckRunRequest",
    "GitHubComment",
    "CreateCommentRequest",
    "UpdateCommentRequest",
    "build_managed_comment_body",
    "extract_managed_comment_marker",
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
    "CodeForgeCommand",
    "CodeForgeCommandType",
    "CodeForgeCommandEvent",
    "CommandParser",
    "InvalidCodeForgeCommand",
    "NormalizedIssueCommentPayload",
    "verify_signature",
    "parse_issue_comment_payload",
    "GitHubCommandConsumer",
    "GitHubStatusConsumer",
    "CommandConsumerError",
    "MalformedCommandEventError",
    "UnresolvableIdentityError",
    "InactiveUserError",
    "InvalidCommandActionError",
    "TransientConsumerError",
]

