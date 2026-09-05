"""GitHub specific permission model."""

from enum import Enum


class GitHubPermission(str, Enum):
    """Permissions for GitHub operations."""
    READ = "read"
    WRITE = "write"

def has_github_permission(required: GitHubPermission, granted: GitHubPermission) -> bool:
    """Check if the granted permission satisfies the required permission."""
    if required == GitHubPermission.READ:
        # Both READ and WRITE satisfy a READ requirement.
        return granted in (GitHubPermission.READ, GitHubPermission.WRITE)
    if required == GitHubPermission.WRITE:
        # Only WRITE satisfies a WRITE requirement.
        return granted == GitHubPermission.WRITE
    return False
