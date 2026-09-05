"""Typed internal models for normalized GitHub webhook events."""

from pydantic import BaseModel


class GitHubInstallationContext(BaseModel):
    id: int


class GitHubRepositoryContext(BaseModel):
    id: int
    name: str
    owner_login: str
    full_name: str
    default_branch: str


class GitHubIssueContext(BaseModel):
    number: int
    title: str
    body: str | None = None


class GitHubIssueCommentContext(BaseModel):
    id: int
    body: str


class GitHubPullRequestContext(BaseModel):
    number: int
    title: str
    body: str | None = None
    head_ref: str
    base_ref: str


class GitHubWebhookEvent(BaseModel):
    """Normalized internal representation of a GitHub webhook event."""

    # Event metadata
    delivery_id: str
    event_type: str  # e.g., 'issues', 'issue_comment', 'pull_request'
    action: str      # e.g., 'opened', 'created', 'synchronize'

    # Common contexts
    installation: GitHubInstallationContext
    repository: GitHubRepositoryContext

    # Event-specific contexts
    issue: GitHubIssueContext | None = None
    comment: GitHubIssueCommentContext | None = None
    pull_request: GitHubPullRequestContext | None = None

