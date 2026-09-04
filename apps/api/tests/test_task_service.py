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
