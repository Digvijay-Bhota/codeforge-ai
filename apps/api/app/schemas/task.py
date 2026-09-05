"""Task-related Pydantic schemas for the Phase 1 coding agent.

These models describe the full request/response lifecycle of a coding task:
request → plan → edits → test → result.
"""

from __future__ import annotations

from enum import Enum

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
