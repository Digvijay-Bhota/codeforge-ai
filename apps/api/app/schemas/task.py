"""Task-related Pydantic schemas for the Phase 1 coding agent.

These models describe the full request/response lifecycle of a coding task:
request → plan → edits → test → result.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """High-level outcome of a coding task."""

    success = "success"
    failure = "failure"
    error = "error"


class TaskRequest(BaseModel):
    """Input required to start a coding task."""

    workspace_path: str = Field(
        ...,
        description="Absolute path to the repository/workspace root.",
    )
    description: str = Field(
        ...,
        min_length=10,
        description="Natural-language description of the coding task.",
    )
    test_command: list[str] | None = Field(
        default=None,
        description="Override the default test command (defaults to pytest).",
    )


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
    agent_output: str
