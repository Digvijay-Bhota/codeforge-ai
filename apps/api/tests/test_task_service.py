"""Tests for TaskService (Phase 5 delegation).

TaskService now delegates to Orchestrator.  Tests mock at the Orchestrator
level to avoid real LLM calls, while preserving the original test semantics:

- no-changes task → TaskStatus.failure
- repository context is used → (covered by orchestration tests more specifically)
- invalid plan → TaskStatus.error
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.orchestration.models import FinalTaskResult, WorkflowStatus
from app.schemas.task import TaskRequest, TaskStatus, TestResult
from app.services.task_service import TaskService


def _make_failed_final(reason: str) -> FinalTaskResult:
    return FinalTaskResult(
        task_id="test-id",
        workflow_status=WorkflowStatus.FAILED,
        task_description="Do nothing",
        plan_summary="",
        changed_files=[],
        diff="",
        test_result=None,
        final_message=f"Task failed: {reason}",
        failure_reason=reason,
    )


def _make_success_final() -> FinalTaskResult:
    return FinalTaskResult(
        task_id="test-id",
        workflow_status=WorkflowStatus.COMPLETED,
        task_description="Fix it",
        plan_summary="Fix the bug",
        changed_files=["src/auth.py"],
        diff="--- a\n+++ b\n",
        test_result=TestResult(
            passed=True, exit_code=0, stdout="1 passed", stderr="", duration_seconds=0.5
        ),
        final_message="Done",
        failure_reason=None,
    )


@pytest.mark.asyncio
async def test_run_task_no_changes(tmp_path: Path) -> None:
    """No-change outcome from Orchestrator → TaskStatus.failure."""
    service = TaskService()
    request = TaskRequest(
        workspace_path=str(tmp_path),
        description="Do nothing here",
    )
    no_change_reason = "Coding agent completed without making any changes to the workspace."
    failed_result = _make_failed_final(no_change_reason)

    with patch.object(
        service._orchestrator, "run", new=AsyncMock(return_value=failed_result)
    ):
        result = await service.run_task(request)

    assert result.status == TaskStatus.failure
    assert "without making any changes" in (result.error_message or "").lower()
    assert not result.changed_files


@pytest.mark.asyncio
async def test_run_task_success(tmp_path: Path) -> None:
    """Completed workflow → TaskStatus.success."""
    service = TaskService()
    request = TaskRequest(
        workspace_path=str(tmp_path),
        description="Fix authentication logic here",
    )
    with patch.object(
        service._orchestrator, "run", new=AsyncMock(return_value=_make_success_final())
    ):
        result = await service.run_task(request)

    assert result.status == TaskStatus.success
    assert result.test_result is not None
    assert result.test_result.passed is True
    assert "src/auth.py" in [f.path for f in result.changed_files]


@pytest.mark.asyncio
async def test_run_task_invalid_plan(tmp_path: Path) -> None:
    """Plan validation failure → TaskStatus.error."""
    service = TaskService()
    request = TaskRequest(workspace_path=str(tmp_path), description="do bad things here")

    plan_fail_reason = "Planning failed: Plan validation failed: Plan must contain at least one step"
    failed_result = _make_failed_final(plan_fail_reason)

    with patch.object(
        service._orchestrator, "run", new=AsyncMock(return_value=failed_result)
    ):
        result = await service.run_task(request)

    assert result.status == TaskStatus.error
    assert "Plan" in (result.error_message or "")


@pytest.mark.asyncio
async def test_run_task_workspace_error() -> None:
    """Non-existent workspace path → TaskStatus.error before reaching Orchestrator."""
    service = TaskService()
    request = TaskRequest(
        workspace_path="/nonexistent/path/xyz",
        description="Fix the authentication module",
    )
    # Should fail at workspace init, not reaching orchestrator.
    result = await service.run_task(request)
    assert result.status == TaskStatus.error
    assert result.error_message is not None
    assert "exist" in result.error_message.lower() or "workspace" in result.error_message.lower()
