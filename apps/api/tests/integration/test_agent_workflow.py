"""Integration tests for the full coding-agent workflow.

These tests make real calls to the OpenAI API and therefore:
- Require OPENAI_API_KEY to be set in the environment.
- Are skipped automatically when the key is absent.
- Are marked ``integration`` so they can be excluded from the fast unit-test
  suite: ``pytest -m 'not integration'``

Run the integration suite explicitly:
    OPENAI_API_KEY=sk-... pytest apps/api/tests/integration -v -m integration
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.schemas.task import TaskRequest, TaskStatus
from app.services.task_service import TaskService

_HAS_KEY = bool(os.getenv("OPENAI_API_KEY"))


@pytest.mark.integration
@pytest.mark.skipif(not _HAS_KEY, reason="OPENAI_API_KEY not set")
@pytest.mark.asyncio
async def test_agent_fixes_calculator_bug(sample_repo: Path) -> None:
    """End-to-end: agent receives the task, fixes the bug, tests pass."""
    service = TaskService()
    request = TaskRequest(
        workspace_path=str(sample_repo),
        description=(
            "The add function in calculator.py has a bug: it subtracts instead of adds. "
            "Fix the function so that add(a, b) returns a + b."
        ),
    )
    result = await service.run_task(request)

    assert result.status == TaskStatus.success, (
        f"Expected success but got {result.status}.\n"
        f"Error: {result.error_message}\n"
        f"Agent output: {result.agent_output}\n"
        f"Test result: {result.test_result}"
    )
    assert result.test_result is not None
    assert result.test_result.passed is True
    # The diff should show the fix
    assert "+" in result.diff
    # calculator.py should be among the changed files
    changed_paths = [f.path for f in result.changed_files]
    assert any("calculator" in p for p in changed_paths)


@pytest.mark.integration
@pytest.mark.skipif(not _HAS_KEY, reason="OPENAI_API_KEY not set")
@pytest.mark.asyncio
async def test_agent_reports_workspace_error() -> None:
    """Agent returns TaskStatus.error for a non-existent workspace."""
    service = TaskService()
    request = TaskRequest(
        workspace_path="/definitely/does/not/exist/at/all/anywhere",
        description="Fix the bug in the calculator add function please",
    )
    result = await service.run_task(request)
    assert result.status == TaskStatus.error
    assert result.error_message is not None
    assert "does not exist" in result.error_message
