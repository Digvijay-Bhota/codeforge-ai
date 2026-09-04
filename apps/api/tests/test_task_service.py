from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.coding_agent import AgentFinalOutput
from app.schemas.task import TaskRequest, TaskStatus
from app.services.task_service import TaskService


@pytest.mark.asyncio
async def test_run_task_no_changes(tmp_path: Path) -> None:
    service = TaskService()
    request = TaskRequest(
        workspace_path=str(tmp_path),
        description="Do nothing",
    )

    # Mock make_coding_agent to return a mock agent
    # Mock Runner.run to return a RunResult with AgentFinalOutput
    class MockRunResult:
        final_output = AgentFinalOutput(
            plan=[],
            message="I made no changes"
        )

    with patch("app.services.task_service.make_coding_agent"), \
         patch("app.services.task_service.Runner.run", new_callable=AsyncMock) as mock_run:

        mock_run.return_value = MockRunResult()

        result = await service.run_task(request)

        assert result.status == TaskStatus.failure
        assert result.error_message == "The agent completed without making any changes to the workspace."
        assert result.agent_output == "I made no changes"
        assert not result.changed_files

@pytest.mark.asyncio
async def test_run_task_with_repository_context(tmp_path: Path) -> None:
    # Set up basic repo structure
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text("def auth(): pass")
    (tmp_path / "README.md").write_text("# Hello")

    service = TaskService()
    request = TaskRequest(
        workspace_path=str(tmp_path),
        description="fix authentication",
    )

    class MockRunResult:
        final_output = AgentFinalOutput(
            plan=[],
            message="I did it"
        )

    with patch("app.services.task_service.make_coding_agent"), \
         patch("app.services.task_service.Runner.run", new_callable=AsyncMock) as mock_run, \
         patch.object(service._test_runner, "run") as mock_test_run:

        from app.schemas.task import TestResult
        mock_test_run.return_value = TestResult(passed=True, command="", exit_code=0, stdout="", stderr="", duration_seconds=1.0)
        mock_run.return_value = MockRunResult()

        with patch("app.services.task_service.WorkspaceManager.get_modified_paths", return_value=["src/auth.py"]):
            _ = await service.run_task(request)

        assert mock_run.call_count == 1
        agent_arg, prompt_arg = mock_run.call_args[0]

        # Verify context is built and passed
        assert "Repository Context" in prompt_arg
        assert "auth.py" in prompt_arg  # Relevant file
        assert "README.md" in prompt_arg # Important file
        assert "fix authentication" in prompt_arg
        assert str(tmp_path) in prompt_arg
