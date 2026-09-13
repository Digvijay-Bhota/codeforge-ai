"""Strongly typed models for GitHub integration."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

# GitHub limits: owner/repo names are max 39 and 100 chars typically, alphanumeric + hyphens + dots + underscores.
# Ref: https://docs.github.com/en/get-started/getting-started-with-git/about-remote-repositories
_NAME_REGEX = re.compile(r"^[a-zA-Z0-9_.-]+$")
_BRANCH_REGEX = re.compile(r"^[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)*$")
_SHA_REGEX = re.compile(r"^[0-9a-fA-F]{40}$")


def _validate_github_name(name: str) -> str:
    """Validate owner or repo name."""
    if not name or not name.strip():
        raise ValueError("Name cannot be empty")
    if not _NAME_REGEX.match(name):
        raise ValueError(f"Invalid characters in name: {name}")
    if ".." in name:
        raise ValueError(f"Path traversal detected in name: {name}")
    return name


def _validate_branch_name(name: str) -> str:
    """Validate branch name."""
    if not name or not name.strip():
        raise ValueError("Branch name cannot be empty")
    if not _BRANCH_REGEX.match(name):
        raise ValueError(f"Invalid characters in branch name: {name}")
    if ".." in name:
        raise ValueError(f"Path traversal detected in branch name: {name}")
    return name


def _validate_sha(sha: str, name: str = "sha") -> str:
    """Validate 40-character hex commit SHA."""
    if not sha or not isinstance(sha, str) or not sha.strip():
        raise ValueError(f"{name} cannot be empty")
    stripped = sha.strip()
    if not _SHA_REGEX.match(stripped):
        raise ValueError(f"Invalid {name} (must be 40-character hex SHA): {sha!r}")
    return stripped


def _validate_positive_int(val: Any, name: str = "identifier") -> int:
    """Validate positive integer."""
    if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
        raise ValueError(f"{name} must be a positive integer, got {val!r}")
    return int(val)


class GitHubRepository(BaseModel):
    id: int | None = None
    owner: str = Field(..., max_length=100)
    name: str = Field(..., max_length=100)
    full_name: str
    default_branch: str
    private: bool
    clone_url: str
    html_url: str

    @field_validator("owner", "name")
    @classmethod
    def validate_names(cls, v: str) -> str:
        return _validate_github_name(v)


class GitHubBranch(BaseModel):
    name: str = Field(..., max_length=255)
    sha: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return _validate_branch_name(v)


class GitHubCommit(BaseModel):
    sha: str
    message: str
    branch: str


class GitHubPullRequest(BaseModel):
    number: int
    title: str
    body: str
    head_branch: str
    base_branch: str
    html_url: str
    state: str


class CreateBranchRequest(BaseModel):
    owner: str
    repo: str
    branch_name: str = Field(..., max_length=255)
    base_sha: str

    @field_validator("owner", "repo")
    @classmethod
    def validate_repo_names(cls, v: str) -> str:
        return _validate_github_name(v)

    @field_validator("branch_name")
    @classmethod
    def validate_branch(cls, v: str) -> str:
        return _validate_branch_name(v)


class CreatePullRequestRequest(BaseModel):
    owner: str
    repo: str
    title: str = Field(..., min_length=1, max_length=255)
    body: str = ""
    head_branch: str
    base_branch: str

    @field_validator("owner", "repo")
    @classmethod
    def validate_repo_names(cls, v: str) -> str:
        return _validate_github_name(v)

    @field_validator("head_branch", "base_branch")
    @classmethod
    def validate_branches(cls, v: str) -> str:
        return _validate_branch_name(v)

    @field_validator("base_branch")
    @classmethod
    def validate_head_not_base(cls, v: str, info: Any) -> str:
        if "head_branch" in info.data and v == info.data["head_branch"]:
            raise ValueError("head_branch and base_branch cannot be the same")
        return v


class GitHubCollaboratorPermission(BaseModel):
    """Normalized collaborator permission for a GitHub repository."""

    username: str
    permission: str = "none"  # "admin", "maintain", "push", "triage", "pull", "none"
    role_name: str | None = None
    user_id: int | None = None
    can_admin: bool = False
    can_maintain: bool = False
    can_push: bool = False
    can_triage: bool = False
    can_pull: bool = False

    @field_validator("username")
    @classmethod
    def validate_user_name(cls, v: str) -> str:
        return _validate_github_name(v)

    @property
    def is_collaborator(self) -> bool:
        """Check if user has any collaborator status on repository."""
        return self.permission != "none"

    @property
    def can_write(self) -> bool:
        """Check if user has write or above permissions."""
        return (
            self.permission in ("admin", "maintain", "push", "write")
            or self.can_push
            or self.can_admin
        )

    @property
    def can_read(self) -> bool:
        """Check if user has read or above permissions."""
        return (
            self.permission
            in ("admin", "maintain", "push", "triage", "pull", "write", "read")
            or self.can_pull
            or self.can_write
        )


class CheckRunOutput(BaseModel):
    """Structured text output for a GitHub Check Run."""

    title: str = Field(..., max_length=255)
    summary: str = Field(..., max_length=65535)
    text: str | None = Field(None, max_length=65535)


class GitHubCheckRun(BaseModel):
    """Typed GitHub Check Run entity."""

    id: int
    name: str
    head_sha: str
    status: str  # "queued", "in_progress", "completed"
    conclusion: str | None = None
    html_url: str | None = None
    details_url: str | None = None
    external_id: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    output: CheckRunOutput | None = None


_VALID_CHECK_STATUSES = {"queued", "in_progress", "completed"}
_VALID_CHECK_CONCLUSIONS = {
    "action_required",
    "cancelled",
    "failure",
    "neutral",
    "success",
    "skipped",
    "stale",
    "timed_out",
}


class CreateCheckRunRequest(BaseModel):
    """Payload for creating a GitHub Check Run."""

    owner: str
    repo: str
    name: str = Field(..., min_length=1, max_length=255)
    head_sha: str
    status: str = Field("queued")
    conclusion: str | None = None
    details_url: str | None = None
    external_id: str | None = Field(None, max_length=100)
    started_at: str | None = None
    completed_at: str | None = None
    output: CheckRunOutput | None = None

    @field_validator("owner", "repo")
    @classmethod
    def validate_repo_names(cls, v: str) -> str:
        return _validate_github_name(v)

    @field_validator("head_sha")
    @classmethod
    def validate_sha(cls, v: str) -> str:
        return _validate_sha(v, "head_sha")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in _VALID_CHECK_STATUSES:
            raise ValueError(f"Invalid status: {v!r}. Must be one of {_VALID_CHECK_STATUSES}")
        return v

    @field_validator("conclusion")
    @classmethod
    def validate_conclusion(cls, v: str | None, info: Any) -> str | None:
        if v is None:
            return None
        if v not in _VALID_CHECK_CONCLUSIONS:
            raise ValueError(
                f"Invalid conclusion: {v!r}. Must be one of {_VALID_CHECK_CONCLUSIONS}"
            )
        status = info.data.get("status")
        if status is not None and status != "completed":
            raise ValueError("conclusion can only be set when status is 'completed'")
        return v


class UpdateCheckRunRequest(BaseModel):
    """Payload for updating an existing GitHub Check Run."""

    owner: str
    repo: str
    check_run_id: int
    name: str | None = Field(None, min_length=1, max_length=255)
    status: str | None = None
    conclusion: str | None = None
    details_url: str | None = None
    external_id: str | None = Field(None, max_length=100)
    started_at: str | None = None
    completed_at: str | None = None
    output: CheckRunOutput | None = None

    @field_validator("owner", "repo")
    @classmethod
    def validate_repo_names(cls, v: str) -> str:
        return _validate_github_name(v)

    @field_validator("check_run_id")
    @classmethod
    def validate_id(cls, v: int) -> int:
        return _validate_positive_int(v, "check_run_id")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in _VALID_CHECK_STATUSES:
            raise ValueError(f"Invalid status: {v!r}. Must be one of {_VALID_CHECK_STATUSES}")
        return v

    @field_validator("conclusion")
    @classmethod
    def validate_conclusion(cls, v: str | None, info: Any) -> str | None:
        if v is None:
            return None
        if v not in _VALID_CHECK_CONCLUSIONS:
            raise ValueError(
                f"Invalid conclusion: {v!r}. Must be one of {_VALID_CHECK_CONCLUSIONS}"
            )
        status = info.data.get("status")
        if status is not None and status != "completed":
            raise ValueError("conclusion can only be set when status is 'completed'")
        return v


class GitHubComment(BaseModel):
    """Typed GitHub Issue or Pull Request comment entity."""

    id: int
    body: str
    html_url: str
    user_login: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class CreateCommentRequest(BaseModel):
    """Payload for creating a comment on an issue or pull request."""

    owner: str
    repo: str
    issue_number: int
    body: str = Field(..., min_length=1)

    @field_validator("owner", "repo")
    @classmethod
    def validate_repo_names(cls, v: str) -> str:
        return _validate_github_name(v)

    @field_validator("issue_number")
    @classmethod
    def validate_issue_num(cls, v: int) -> int:
        return _validate_positive_int(v, "issue_number")


class UpdateCommentRequest(BaseModel):
    """Payload for updating an existing issue or pull request comment."""

    owner: str
    repo: str
    comment_id: int
    body: str = Field(..., min_length=1)

    @field_validator("owner", "repo")
    @classmethod
    def validate_repo_names(cls, v: str) -> str:
        return _validate_github_name(v)

    @field_validator("comment_id")
    @classmethod
    def validate_comment_num(cls, v: int) -> int:
        return _validate_positive_int(v, "comment_id")


_MANAGED_COMMENT_MARKER_REGEX = re.compile(
    r"<!--\s*codeforge:managed-comment:([a-zA-Z0-9_.-]+)\s*-->"
)


def build_managed_comment_body(body: str, marker_id: str) -> str:
    """Embed a deterministic machine-detectable CodeForge marker into a comment body."""
    if not marker_id or not _NAME_REGEX.match(marker_id):
        raise ValueError(f"Invalid marker_id: {marker_id!r}")
    marker = f"<!-- codeforge:managed-comment:{marker_id} -->"
    return f"{marker}\n\n{body}"


def extract_managed_comment_marker(body: str) -> str | None:
    """Extract marker_id from a comment body if present, else None."""
    if not body:
        return None
    match = _MANAGED_COMMENT_MARKER_REGEX.search(body)
    return match.group(1) if match else None
