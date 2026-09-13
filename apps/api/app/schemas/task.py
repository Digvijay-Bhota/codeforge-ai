"""Task-related Pydantic schemas for the Phase 1 coding agent.

These models describe the full request/response lifecycle of a coding task:
request → plan → edits → test → result.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class TaskStatus(str, Enum):
    """High-level outcome of a coding task."""

    success = "success"
    failure = "failure"
    error = "error"


class ExecutionTarget(str, Enum):
    """The execution target for the task."""
    local = "local"
    github = "github"

class GitHubFailureStage(str, Enum):
    """Specific failure stages for the GitHub execution workflow."""
    REPOSITORY_RESOLUTION_FAILED = "REPOSITORY_RESOLUTION_FAILED"
    BRANCH_CREATION_FAILED = "BRANCH_CREATION_FAILED"
    REPOSITORY_ACQUISITION_FAILED = "REPOSITORY_ACQUISITION_FAILED"
    ORCHESTRATION_FAILED = "ORCHESTRATION_FAILED"
    NO_CHANGES = "NO_CHANGES"
    COMMIT_FAILED = "COMMIT_FAILED"
    PUSH_FAILED = "PUSH_FAILED"
    PULL_REQUEST_FAILED = "PULL_REQUEST_FAILED"

class TaskRequest(BaseModel):
    """Input required to start a coding task."""

    execution_target: ExecutionTarget = Field(
        default=ExecutionTarget.local,
        description="Target execution environment (local or github).",
    )

    workspace_path: str = Field(
        default="",
        description="Absolute path to the repository/workspace root. Required for local tasks.",
    )

    github_repository: str | None = Field(
        default=None,
        description="GitHub repository in 'owner/repo' format. Required for github tasks.",
    )

    github_base_branch: str | None = Field(
        default=None,
        description="Base branch to branch from. If omitted, uses the repository's default branch.",
    )
    description: str = Field(
        ...,
        min_length=10,
        max_length=131072,
        description="Natural-language description of the coding task.",
    )

    require_plan_approval: bool = Field(
        default=False,
        description="Whether human plan approval is required before coding commences.",
    )

    approval_config: dict[str, Any] | None = Field(
        default=None,
        description="Optional approval configuration such as timeouts or review policies.",
    )

    @model_validator(mode="after")
    def validate_execution_target(self) -> TaskRequest:
        if self.execution_target == ExecutionTarget.local:
            if not self.workspace_path:
                raise ValueError("workspace_path is required for local execution target")
        elif self.execution_target == ExecutionTarget.github:
            if not self.github_repository:
                raise ValueError("github_repository is required for github execution target")
        return self


class PlanStep(BaseModel):
    """A single step in the agent's implementation plan."""

    step: int = Field(..., ge=1)
    description: str


class ChangedFile(BaseModel):
    """A file that was created, modified, or deleted during the task."""

    path: str
    action: str = Field(
        ...,
        description="One of: 'created', 'modified', 'deleted'.",
    )


class TestResult(BaseModel):
    """Structured output from a test-suite run."""

    passed: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float


class GitHubPublicationMetadata(BaseModel):
    """Metadata regarding a published GitHub PR."""
    repository: str
    base_branch: str
    working_branch: str
    commit_sha: str | None = None
    pull_request_number: int | None = None
    pull_request_url: str | None = None

class TaskResult(BaseModel):
    """Full result of a completed (or failed) coding task."""

    task_id: str
    status: TaskStatus
    description: str
    plan: list[PlanStep]
    changed_files: list[ChangedFile]
    diff: str
    test_result: TestResult | None = None
    error_message: str | None = None
    failure_stage: GitHubFailureStage | None = None
    agent_output: str
    github: GitHubPublicationMetadata | None = None
    full_plan: dict[str, Any] | None = None


# ── Phase 10A: Product API & Approval Core Schemas ───────────────────────────


class ApproveTaskRequest(BaseModel):
    """Payload to approve a task currently in WAITING_APPROVAL."""

    comment: str | None = Field(default=None, max_length=5000, description="Optional reviewer feedback or guidance.")


class RejectTaskRequest(BaseModel):
    """Payload to reject/cancel a task currently in WAITING_APPROVAL."""

    reason: str | None = Field(default=None, max_length=5000, description="Reason for rejection or cancellation.")


class RetryTaskRequest(BaseModel):
    """Payload to retry a completed, failed, or cancelled task."""

    additional_instructions: str | None = Field(
        default=None, max_length=10000, description="Additional context or corrections to append to the original prompt."
    )
    workspace_path: str | None = Field(
        default=None, description="Optional isolated workspace path override for local execution target."
    )


class TaskSummary(BaseModel):
    """Compact summary of a task for listing views."""

    task_id: str
    status: str
    execution_target: str
    repository: str | None = None
    requested_task: str
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    parent_task_id: str | None = None
    has_plan: bool = False
    has_diff: bool = False


class TaskListResponse(BaseModel):
    """Paginated list of tasks."""

    total: int
    limit: int
    offset: int
    tasks: list[TaskSummary]


class ApprovalDetail(BaseModel):
    """Detailed record of a task approval checkpoint."""

    id: int
    approval_type: str
    status: str
    requested_by: str | None = None
    approved_by: str | None = None
    comment: str | None = None
    requested_at: str
    responded_at: str | None = None
    expires_at: str | None = None


class TaskDetailResponse(BaseModel):
    """Comprehensive product-level details for a task."""

    task_id: str
    status: str
    execution_target: str
    repository: str | None = None
    workspace_path: str | None = None
    requested_task: str
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    parent_task_id: str | None = None
    approval_config: dict[str, Any] | None = None
    pr_metadata: dict[str, Any] | None = None
    failure_code: str | None = None
    failure_reason: str | None = None
    final_message: str | None = None
    implementation_plan: dict[str, Any] | None = None
    task_result: dict[str, Any] | None = None
    latest_approval: ApprovalDetail | None = None


class FileDiff(BaseModel):
    """Structured diff representation for a single modified file."""

    path: str
    status: str = Field(description="'modified', 'added', or 'deleted'")
    additions: int = 0
    deletions: int = 0
    patch: str = ""


class DiffResponse(BaseModel):
    """Bounded, structured diff output for a task."""

    task_id: str
    files_changed_count: int
    additions: int
    deletions: int
    files: list[FileDiff]
