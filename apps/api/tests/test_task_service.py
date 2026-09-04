from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.coding_agent import AgentFinalOutput
from app.schemas.plan import ImplementationPlan, PlanAction, RiskLevel
from app.schemas.plan import PlanStep as PlanStepModel
from app.schemas.task import TaskRequest, TaskStatus
from app.services.task_service import TaskService


def make_dummy_plan() -> ImplementationPlan:
    return ImplementationPlan(
        goal="Do it",
        validation_strategy="Run tests",
        risk_level=RiskLevel.low,
        summary="summary",
        steps=[
            PlanStepModel(
                step_number=1,
                action=PlanAction.modify,
                description="Change it",
                rationale="Because"
            )
        ]
    )

@pytest.mark.asyncio
async def test_run_task_no_changes(tmp_path: Path) -> None:
    service = TaskService()
    request = TaskRequest(
        workspace_path=str(tmp_path),
        description="Do nothing",
    )

    class MockPlannerResult:
        final_output = make_dummy_plan()

    class MockCodingResult:
        final_output = AgentFinalOutput(
            plan=[],
            message="I made no changes"
        )

    with patch("app.agents.planner.make_planner_agent"), \
         patch("app.services.task_service.make_coding_agent"), \
         patch("app.services.task_service.Runner.run", new_callable=AsyncMock) as mock_run:

        mock_run.side_effect = [MockPlannerResult(), MockCodingResult()]

        result = await service.run_task(request)

        assert result.status == TaskStatus.failure
        assert result.error_message == "The agent completed without making any changes to the workspace."
        assert result.agent_output == "I made no changes"
        assert not result.changed_files

@pytest.mark.asyncio
async def test_run_task_with_repository_context(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text("def auth(): pass")
    (tmp_path / "README.md").write_text("# Hello")

    service = TaskService()
    request = TaskRequest(
        workspace_path=str(tmp_path),
        description="fix authentication",
    )

    class MockPlannerResult:
        final_output = make_dummy_plan()

    class MockCodingResult:
        final_output = AgentFinalOutput(
            plan=[],
            message="I did it"
        )

    with patch("app.agents.planner.make_planner_agent"), \
         patch("app.services.task_service.make_coding_agent"), \
         patch("app.services.task_service.Runner.run", new_callable=AsyncMock) as mock_run, \
         patch.object(service._test_runner, "run") as mock_test_run:

        from app.schemas.task import TestResult
        mock_test_run.return_value = TestResult(passed=True, command="", exit_code=0, stdout="", stderr="", duration_seconds=1.0)

        mock_run.side_effect = [MockPlannerResult(), MockCodingResult()]

        with patch("app.services.task_service.WorkspaceManager.get_modified_paths", return_value=["src/auth.py"]):
            _ = await service.run_task(request)

        assert mock_run.call_count == 2

        # Check planner args
        planner_arg, planner_prompt = mock_run.call_args_list[0][0]
        assert "Repository Context" in planner_prompt
        assert "auth.py" in planner_prompt

        # Check coding agent args
        coder_arg, coder_prompt = mock_run.call_args_list[1][0]
        assert "Repository Context" in coder_prompt
        assert "Implementation Plan" in coder_prompt
        assert "Do it" in coder_prompt  # the goal from dummy plan
        assert "Original Task description" in coder_prompt

@pytest.mark.asyncio
async def test_run_task_invalid_plan(tmp_path: Path) -> None:
    service = TaskService()
    request = TaskRequest(workspace_path=str(tmp_path), description="do bad things")

    class MockPlannerResult:
        # We construct it using model_construct to deliberately bypass Pydantic's
        # min_length=1 validation so we can prove our explicit validator catches it
        final_output = ImplementationPlan.model_construct(
            goal="Do a thing",
            validation_strategy="Run tests",
            risk_level=RiskLevel.low,
            summary="summary",
            steps=[],
            affected_files=[]
        )

    with patch("app.agents.planner.make_planner_agent"), \
         patch("app.services.task_service.make_coding_agent"), \
         patch("app.services.task_service.Runner.run", new_callable=AsyncMock) as mock_run:

        mock_run.side_effect = [MockPlannerResult()]

        result = await service.run_task(request)

        assert mock_run.call_count == 1  # Only planner was called
        assert result.status == TaskStatus.error
        assert "Plan must contain at least one step" in result.error_message
