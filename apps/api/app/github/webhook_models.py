from datetime import datetime

from pydantic import BaseModel

from app.github.command_parser import CodeForgeCommand


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
    action: str  # e.g., 'opened', 'created', 'synchronize'

    # Common contexts
    installation: GitHubInstallationContext
    repository: GitHubRepositoryContext

    # Event-specific contexts
    issue: GitHubIssueContext | None = None
    comment: GitHubIssueCommentContext | None = None
    pull_request: GitHubPullRequestContext | None = None


class NormalizedIssueCommentPayload(BaseModel):
    """Normalized payload specifically for issue_comment webhook events."""

    delivery_id: str
    event_name: str = "issue_comment"
    action: str = "created"
    installation_id: int
    repository_id: int
    repository_full_name: str
    repository_owner: str
    repository_name: str
    issue_number: int
    issue_title: str | None = None
    issue_html_url: str | None = None
    is_pull_request: bool = False
    comment_id: int
    comment_body: str
    actor_github_id: int
    actor_login: str
    sender_github_id: int | None = None
    sender_login: str | None = None
    created_at: str | None = None
    head_sha: str | None = None


class CodeForgeCommandEvent(BaseModel):
    """Durable domain event representation for an ingested CodeForge command."""

    delivery_id: str
    repository: str  # e.g. 'owner/repo'
    repository_id: int
    repository_owner: str
    repository_name: str
    installation_id: int
    actor_github_id: int
    actor_login: str
    issue_number: int
    issue_title: str | None = None
    issue_html_url: str | None = None
    is_pull_request: bool = False
    head_sha: str | None = None
    comment_id: int
    command: CodeForgeCommand
    received_at: datetime
    ingestion_status: str = "accepted"
