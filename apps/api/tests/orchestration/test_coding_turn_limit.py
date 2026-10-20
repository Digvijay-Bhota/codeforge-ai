"""Coding-agent turn limit tests.

No real LLM calls are made.  The exhaustion test drives the real Agents SDK
run loop with an in-process fake model that requests a tool call on every
turn, so it needs no API key or network access.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai.types.responses import ResponseFunctionToolCall
from pydantic import ValidationError

import app.mcp.tools.repository  # noqa: F401  (registers the workspace tools)
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.tracing import get_trace_provider
from agents.usage import Usage
from app.agents.coding_agent import make_coding_agent
from app.config import Settings, settings
from app.execution.test_executor import IsolationLevel, TestExecutor
from app.orchestration.models import CodingResult, WorkflowStatus
from app.orchestration.orchestrator import MAX_FAILURE_REASON_LEN, Orchestrator
from app.orchestration.stages import (
    CodingStageError,
    CodingTurnLimitError,
    run_coding_stage,
)
from app.repository.models import GitMetadata, RepositoryContext, RepositoryMap
from app.schemas.plan import ImplementationPlan, PlanAction, RiskLevel
from app.schemas.plan import PlanStep as PlanStepModel
from app.schemas.task import TaskStatus, TestResult
from app.services.task_service import _map_to_task_result
from app.workspace.manager import WorkspaceManager

SECRET_ARGUMENT = "sk-do-not-leak-this-value"


def _executor(isolation: IsolationLevel = IsolationLevel.NONE) -> MagicMock:
    executor = MagicMock(spec=TestExecutor)
    executor.isolation = isolation
    return executor


def _make_context() -> RepositoryContext:
    return RepositoryContext(
        repository_map=RepositoryMap(root="/tmp/test"),
        git_metadata=GitMetadata(is_available=False),
    )


def _make_plan() -> ImplementationPlan:
    return ImplementationPlan(
        goal="Fix the bug",
        validation_strategy="Run pytest",
        risk_level=RiskLevel.low,
        summary="summary",
        steps=[
            PlanStepModel(
                step_number=1,
                action=PlanAction.modify,
                description="Fix it",
                rationale="Because",
            )
        ],
    )


class ToolLoopModel(Model):
    """Fake model that asks for a tool call on every turn and never finishes."""

    def __init__(self, tool_name: str, arguments: str) -> None:
        self.tool_name = tool_name
        self.arguments = arguments
        self.calls = 0
        self._ids = itertools.count(1)

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        self.calls += 1
        n = next(self._ids)
        return ModelResponse(
            output=[
                ResponseFunctionToolCall(
                    type="function_call",
                    id=f"fc_{n}",
                    call_id=f"call_{n}",
                    name=self.tool_name,
                    arguments=self.arguments,
                )
            ],
            usage=Usage(),
            response_id=None,
        )

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


@pytest.fixture
def tracing_disabled():
    provider = get_trace_provider()
    previous = getattr(provider, "_manual_disabled", None)
    provider.set_disabled(True)
    yield
    provider._manual_disabled = previous  # type: ignore[attr-defined]
    provider._refresh_disabled_flag()  # type: ignore[attr-defined]


# ── Configuration ────────────────────────────────────────────────────────────


def test_turn_limit_default_is_25(monkeypatch):
    monkeypatch.delenv("CODEFORGE_CODING_MAX_TURNS", raising=False)
    assert Settings(_env_file=None).codeforge_coding_max_turns == 25


def test_turn_limit_env_override(monkeypatch):
    monkeypatch.setenv("CODEFORGE_CODING_MAX_TURNS", "40")
    assert Settings(_env_file=None).codeforge_coding_max_turns == 40


@pytest.mark.parametrize("value", [0, -1, 101])
def test_turn_limit_rejects_out_of_range(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, codeforge_coding_max_turns=value)


@pytest.mark.parametrize("value", [1, 100])
def test_turn_limit_accepts_bounds(value):
    assert Settings(_env_file=None, codeforge_coding_max_turns=value).codeforge_coding_max_turns == value


@pytest.mark.parametrize("value", [0, 101])
def test_orchestrator_rejects_out_of_range_turn_limit(value):
    with pytest.raises(ValueError):
        Orchestrator(executor=_executor(), max_turns=value)


# ── Limit is passed to the SDK ───────────────────────────────────────────────


class _FakeResult:
    final_output = "done"


@pytest.mark.asyncio
async def test_coding_stage_passes_configured_turn_limit(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "codeforge_coding_max_turns", 7)
    workspace = WorkspaceManager(tmp_path)
    with (
        patch("app.orchestration.stages.make_coding_agent"),
        patch("app.orchestration.stages.Runner.run", new_callable=AsyncMock, return_value=_FakeResult()) as run,
    ):
        await run_coding_stage("task", _make_context(), _make_plan(), workspace, _executor(), "model")
    assert run.await_args.kwargs["max_turns"] == 7


@pytest.mark.asyncio
async def test_coding_stage_passes_explicit_turn_limit(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "codeforge_coding_max_turns", 7)
    workspace = WorkspaceManager(tmp_path)
    with (
        patch("app.orchestration.stages.make_coding_agent"),
        patch("app.orchestration.stages.Runner.run", new_callable=AsyncMock, return_value=_FakeResult()) as run,
    ):
        await run_coding_stage(
            "task", _make_context(), _make_plan(), workspace, _executor(), "model", max_turns=3
        )
    assert run.await_args.kwargs["max_turns"] == 3


@pytest.mark.asyncio
async def test_orchestrator_passes_its_turn_limit_to_coding_stage(tmp_path: Path):
    orchestrator = Orchestrator(executor=_executor(), max_turns=12)
    coding = AsyncMock(return_value=CodingResult(changes_made=True, changed_paths=["x.py"], message="ok"))
    with (
        patch("app.orchestration.orchestrator.run_analysis_stage", return_value=_make_context()),
        patch("app.orchestration.orchestrator.run_planning_stage", new_callable=AsyncMock, return_value=_make_plan()),
        patch("app.orchestration.orchestrator.run_coding_stage", coding),
        patch(
            "app.orchestration.orchestrator.run_testing_stage",
            return_value=TestResult(passed=True, exit_code=0, stdout="", stderr="", duration_seconds=0.1),
        ),
    ):
        await orchestrator.run(WorkspaceManager(tmp_path), "Do something useful here")
    assert coding.await_args.kwargs["max_turns"] == 12


# ── Exhaustion through the real SDK run loop ─────────────────────────────────


@pytest.mark.asyncio
async def test_real_sdk_turn_exhaustion_raises_bounded_actionable_error(tmp_path: Path, tracing_disabled):
    (tmp_path / "a.py").write_text("print('a')\n")
    workspace = WorkspaceManager(tmp_path)
    fake_model = ToolLoopModel("search_files", f'{{"query": "{SECRET_ARGUMENT}", "path": "."}}')

    def build_agent(workspace: WorkspaceManager, executor: TestExecutor, model: str) -> Any:
        return make_coding_agent(workspace=workspace, executor=executor, model=fake_model)  # type: ignore[arg-type]

    with patch("app.orchestration.stages.make_coding_agent", side_effect=build_agent):
        with pytest.raises(CodingTurnLimitError) as exc_info:
            await run_coding_stage(
                "task", _make_context(), _make_plan(), workspace, _executor(), "model", max_turns=3
            )

    exc = exc_info.value
    message = str(exc)
    assert isinstance(exc, CodingStageError)
    assert fake_model.calls == 3
    assert exc.max_turns == 3
    assert exc.tool_call_counts == {"search_files": 3}
    assert "turn limit (3 turns, CODEFORGE_CODING_MAX_TURNS)" in message
    assert "during initial coding" in message
    assert "search_files×3" in message
    assert SECRET_ARGUMENT not in message
    assert len(message) <= MAX_FAILURE_REASON_LEN


def test_turn_limit_message_stays_bounded_with_many_long_tool_names():
    counts = {f"{'t' * 40}{i}": i + 1 for i in range(20)}
    error = CodingTurnLimitError(max_turns=100, phase="repair attempt 5/5", tool_call_counts=counts, changed_file_count=999)
    message = str(error)
    assert len(message) <= MAX_FAILURE_REASON_LEN
    assert message.endswith("Raise CODEFORGE_CODING_MAX_TURNS or narrow the task.")
    assert ", …" in message


@pytest.mark.asyncio
async def test_turn_limit_error_tolerates_missing_run_data(tmp_path: Path):
    from agents.exceptions import MaxTurnsExceeded

    workspace = WorkspaceManager(tmp_path)
    with (
        patch("app.orchestration.stages.make_coding_agent"),
        patch(
            "app.orchestration.stages.Runner.run",
            new_callable=AsyncMock,
            side_effect=MaxTurnsExceeded("Max turns (5) exceeded"),
        ),
    ):
        with pytest.raises(CodingTurnLimitError) as exc_info:
            await run_coding_stage(
                "task", _make_context(), _make_plan(), workspace, _executor(), "model", max_turns=5
            )
    assert exc_info.value.tool_call_counts == {}
    assert "turn limit (5 turns" in str(exc_info.value)


@pytest.mark.asyncio
async def test_turn_limit_message_counts_changed_files(tmp_path: Path):
    from agents.exceptions import MaxTurnsExceeded

    workspace = WorkspaceManager(tmp_path)

    async def edit_then_exhaust(*args: Any, **kwargs: Any) -> Any:
        workspace.write_file("new.py", "x = 1\n")
        raise MaxTurnsExceeded("Max turns (2) exceeded")

    with (
        patch("app.orchestration.stages.make_coding_agent"),
        patch("app.orchestration.stages.Runner.run", side_effect=edit_then_exhaust),
    ):
        with pytest.raises(CodingTurnLimitError) as exc_info:
            await run_coding_stage(
                "task", _make_context(), _make_plan(), workspace, _executor(), "model", max_turns=2
            )
    assert "1 file(s) were changed but not verified" in str(exc_info.value)


@pytest.mark.asyncio
async def test_orchestrator_reports_turn_exhaustion_without_running_tests(tmp_path: Path):
    executor = _executor()
    orchestrator = Orchestrator(executor=executor, max_turns=4)
    testing = MagicMock()
    error = CodingTurnLimitError(max_turns=4, phase="initial coding", tool_call_counts={"read_file": 4}, changed_file_count=0)
    with (
        patch("app.orchestration.orchestrator.run_analysis_stage", return_value=_make_context()),
        patch("app.orchestration.orchestrator.run_planning_stage", new_callable=AsyncMock, return_value=_make_plan()),
        patch("app.orchestration.orchestrator.run_coding_stage", new_callable=AsyncMock, side_effect=error),
        patch("app.orchestration.orchestrator.run_testing_stage", testing),
    ):
        result = await orchestrator.run(WorkspaceManager(tmp_path), "Do something useful here")

    assert result.workflow_status == WorkflowStatus.FAILED
    assert result.failure_reason == str(error)
    assert not result.failure_reason.startswith("Coding agent failed: Coding agent failed")
    assert "CODEFORGE_CODING_MAX_TURNS" in result.failure_reason
    assert len(result.failure_reason) <= MAX_FAILURE_REASON_LEN
    testing.assert_not_called()
    assert _map_to_task_result(result).status == TaskStatus.error


# ── Partial edits before the turn limit ──────────────────────────────────────


def _edit_then_exhaust(workspace: WorkspaceManager, max_turns: int) -> Any:
    async def run(*args: Any, **kwargs: Any) -> Any:
        from agents.exceptions import MaxTurnsExceeded

        workspace.write_file("app.py", "value = 1\n")
        workspace.write_file("new_module.py", "def helper():\n    return 1\n")
        raise MaxTurnsExceeded(f"Max turns ({max_turns}) exceeded")

    return run


@pytest.mark.asyncio
async def test_initial_turn_limit_reports_partial_workspace_changes(tmp_path: Path):
    (tmp_path / "app.py").write_text("value = 0\n")
    workspace = WorkspaceManager(tmp_path)
    testing = MagicMock()
    orchestrator = Orchestrator(executor=_executor(IsolationLevel.SANDBOX), max_turns=4, max_repair_attempts=2)
    with (
        patch("app.orchestration.orchestrator.run_analysis_stage", return_value=_make_context()),
        patch("app.orchestration.orchestrator.run_planning_stage", new_callable=AsyncMock, return_value=_make_plan()),
        patch("app.orchestration.stages.make_coding_agent"),
        patch("app.orchestration.stages.Runner.run", side_effect=_edit_then_exhaust(workspace, 4)),
        patch("app.orchestration.orchestrator.run_testing_stage", testing),
    ):
        result = await orchestrator.run(workspace, "Do something useful here")

    assert result.workflow_status == WorkflowStatus.FAILED
    assert "turn limit (4 turns, CODEFORGE_CODING_MAX_TURNS) during initial coding" in result.failure_reason
    assert "2 file(s) were changed but not verified" in result.failure_reason
    assert result.changed_files == ["app.py", "new_module.py"]
    assert "-value = 0" in result.diff and "+value = 1" in result.diff
    assert "+def helper():" in result.diff
    assert result.test_result is None
    assert result.repair_attempts_used == 0
    assert result.final_message == f"Task failed: {result.failure_reason}"
    testing.assert_not_called()
    task_result = _map_to_task_result(result)
    assert task_result.status == TaskStatus.error
    assert [f.path for f in task_result.changed_files] == ["app.py", "new_module.py"]


@pytest.mark.asyncio
async def test_initial_turn_limit_without_edits_reports_no_changes(tmp_path: Path):
    from agents.exceptions import MaxTurnsExceeded

    workspace = WorkspaceManager(tmp_path)
    orchestrator = Orchestrator(executor=_executor(), max_turns=2)
    with (
        patch("app.orchestration.orchestrator.run_analysis_stage", return_value=_make_context()),
        patch("app.orchestration.orchestrator.run_planning_stage", new_callable=AsyncMock, return_value=_make_plan()),
        patch("app.orchestration.stages.make_coding_agent"),
        patch("app.orchestration.stages.Runner.run", side_effect=MaxTurnsExceeded("Max turns (2) exceeded")),
    ):
        result = await orchestrator.run(workspace, "Do something useful here")

    assert result.workflow_status == WorkflowStatus.FAILED
    assert result.changed_files == []
    assert result.diff == ""


@pytest.mark.asyncio
async def test_initial_agent_error_reports_partial_workspace_changes(tmp_path: Path):
    workspace = WorkspaceManager(tmp_path)

    async def edit_then_fail(*args: Any, **kwargs: Any) -> Any:
        workspace.write_file("partial.py", "x = 1\n")
        raise RuntimeError("upstream 500")

    orchestrator = Orchestrator(executor=_executor())
    with (
        patch("app.orchestration.orchestrator.run_analysis_stage", return_value=_make_context()),
        patch("app.orchestration.orchestrator.run_planning_stage", new_callable=AsyncMock, return_value=_make_plan()),
        patch("app.orchestration.stages.make_coding_agent"),
        patch("app.orchestration.stages.Runner.run", side_effect=edit_then_fail),
    ):
        result = await orchestrator.run(workspace, "Do something useful here")

    assert result.workflow_status == WorkflowStatus.FAILED
    assert result.failure_reason == "Coding agent failed: Coding agent failed: upstream 500"
    assert result.changed_files == ["partial.py"]
    assert "+x = 1" in result.diff


@pytest.mark.asyncio
async def test_github_path_does_not_publish_partial_turn_limit_edits(tmp_path: Path, monkeypatch):
    from app.execution.github_execution import GitHubExecutionService
    from app.schemas.task import ExecutionTarget, GitHubFailureStage, TaskRequest

    client = MagicMock()
    client.token = "test-token"
    client.get_repository = AsyncMock(return_value=MagicMock(default_branch="main"))
    client.get_branch = AsyncMock(return_value=MagicMock(sha="abc123"))
    client.create_branch = AsyncMock()
    client.create_pull_request = AsyncMock()

    git_wrapper = MagicMock()
    monkeypatch.setattr("app.execution.github_execution.SafeGitWrapper", MagicMock(return_value=git_wrapper))
    monkeypatch.setattr(settings, "codeforge_github_clone_root", str(tmp_path))

    service = GitHubExecutionService.__new__(GitHubExecutionService)
    service.github_client = client
    service._orchestrator = Orchestrator(executor=_executor(IsolationLevel.SANDBOX), max_turns=3, max_repair_attempts=2)

    workspaces: list[WorkspaceManager] = []
    real_manager = WorkspaceManager

    def capture_workspace(*args: Any, **kwargs: Any) -> WorkspaceManager:
        ws = real_manager(*args, **kwargs)
        workspaces.append(ws)
        return ws

    async def edit_then_exhaust(*args: Any, **kwargs: Any) -> Any:
        from agents.exceptions import MaxTurnsExceeded

        workspaces[-1].write_file("app.py", "value = 1\n")
        raise MaxTurnsExceeded("Max turns (3) exceeded")

    testing = MagicMock()
    request = TaskRequest(execution_target=ExecutionTarget.github, github_repository="o/r", description="Fix the thing")
    with (
        patch("app.execution.github_execution.WorkspaceManager", side_effect=capture_workspace),
        patch("app.orchestration.orchestrator.run_analysis_stage", return_value=_make_context()),
        patch("app.orchestration.orchestrator.run_planning_stage", new_callable=AsyncMock, return_value=_make_plan()),
        patch("app.orchestration.stages.make_coding_agent"),
        patch("app.orchestration.stages.Runner.run", side_effect=edit_then_exhaust),
        patch("app.orchestration.orchestrator.run_testing_stage", testing),
    ):
        task_result = await service.execute(request, "task-1")

    assert task_result.status == TaskStatus.error
    assert task_result.failure_stage == GitHubFailureStage.ORCHESTRATION_FAILED
    assert "CODEFORGE_CODING_MAX_TURNS" in (task_result.error_message or "")
    assert [f.path for f in task_result.changed_files] == ["app.py"]
    assert task_result.github is None
    testing.assert_not_called()
    git_wrapper.commit_files.assert_not_called()
    git_wrapper.push.assert_not_called()
    client.create_pull_request.assert_not_awaited()
