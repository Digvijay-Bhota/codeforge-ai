"""Unit tests for task Pydantic schemas.

No LLM calls, no I/O.  Pure model validation.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.task import (
    ChangedFile,
    PlanStep,
    TaskRequest,
    TaskResult,
    TaskStatus,
    TestResult,
)

# ── TaskRequest ───────────────────────────────────────────────────────────────

def test_task_request_valid() -> None:
    req = TaskRequest(
        workspace_path="/tmp/repo",
        description="Fix the bug in calculator.py",
    )
    assert req.workspace_path == "/tmp/repo"

def test_task_request_description_too_short() -> None:
    with pytest.raises(ValidationError):
        TaskRequest(workspace_path="/tmp/repo", description="short")

def test_task_request_missing_workspace() -> None:
    with pytest.raises(ValidationError):
        TaskRequest(description="Fix the bug in calculator.py")  # type: ignore[call-arg]

# ── TaskStatus ────────────────────────────────────────────────────────────────

def test_task_status_values() -> None:
    assert TaskStatus.success == "success"
    assert TaskStatus.failure == "failure"
    assert TaskStatus.error == "error"

# ── PlanStep ──────────────────────────────────────────────────────────────────

def test_plan_step_valid() -> None:
    step = PlanStep(step=1, description="Read the source file")
    assert step.step == 1

def test_plan_step_zero_invalid() -> None:
    with pytest.raises(ValidationError):
        PlanStep(step=0, description="invalid")

# ── TestResult ────────────────────────────────────────────────────────────────

def test_test_result_passed() -> None:
    r = TestResult(passed=True, exit_code=0, stdout="1 passed", stderr="", duration_seconds=0.5)
    assert r.passed is True

def test_test_result_failed() -> None:
    r = TestResult(passed=False, exit_code=1, stdout="", stderr="", duration_seconds=1.0)
    assert r.passed is False

# ── ChangedFile ───────────────────────────────────────────────────────────────

def test_changed_file_modified() -> None:
    f = ChangedFile(path="calculator.py", action="modified")
    assert f.action == "modified"

def test_changed_file_created() -> None:
    f = ChangedFile(path="new_file.py", action="created")
    assert f.action == "created"

def test_changed_file_deleted() -> None:
    f = ChangedFile(path="old.py", action="deleted")
    assert f.action == "deleted"

# ── TaskResult ────────────────────────────────────────────────────────────────

def test_task_result_success() -> None:
    result = TaskResult(
        task_id="abc-123",
        status=TaskStatus.success,
        description="Fix the bug",
        plan=[PlanStep(step=1, description="Read and edit")],
        changed_files=[ChangedFile(path="calc.py", action="modified")],
        diff="--- a/calc.py\n+++ b/calc.py\n",
        test_result=TestResult(
            passed=True, exit_code=0, stdout="ok", stderr="", duration_seconds=0.3
        ),
        agent_output="Done",
    )
    assert result.status == TaskStatus.success
    assert result.test_result is not None
    assert result.error_message is None

def test_task_result_error_no_test_result() -> None:
    result = TaskResult(
        task_id="xyz-456",
        status=TaskStatus.error,
        description="Fix something important in the code base",
        plan=[],
        changed_files=[],
        diff="",
        error_message="Workspace not found",
        agent_output="",
    )
    assert result.status == TaskStatus.error
    assert result.test_result is None
    assert result.error_message == "Workspace not found"

def test_task_result_failure() -> None:
    result = TaskResult(
        task_id="fail-789",
        status=TaskStatus.failure,
        description="Fix the failing tests in the project",
        plan=[PlanStep(step=1, description="Attempted fix")],
        changed_files=[ChangedFile(path="main.py", action="modified")],
        diff="--- a\n+++ b\n",
        test_result=TestResult(
            passed=False, exit_code=1, stdout="1 failed", stderr="", duration_seconds=0.8
        ),
        agent_output="Tests still failing after my changes.",
    )
    assert result.status == TaskStatus.failure
    assert result.test_result is not None
    assert result.test_result.passed is False
