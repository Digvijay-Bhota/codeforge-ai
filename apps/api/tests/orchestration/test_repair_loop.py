"""Bounded test-failure repair loop tests.

No real LLM calls are made.  The coding stage is replaced by fakes that edit
the workspace through WorkspaceManager, so diffs and changed files are real.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.execution.ownership import (
    OwnershipLostError,
    reset_async_ownership_verifier,
    set_async_ownership_verifier,
)
from app.execution.test_executor import IsolationLevel, TestExecutor
from app.orchestration.models import (
    CodingResult,
    RepairRequest,
    WorkflowStatus,
    is_valid_transition,
)
from app.orchestration.orchestrator import MAX_FAILURE_REASON_LEN, Orchestrator
from app.orchestration.stages import (
    REPAIR_FEEDBACK_MAX_BYTES,
    CodingStageError,
    CodingTurnLimitError,
    build_repair_prompt,
    run_coding_stage,
)
from app.repository.models import GitMetadata, RepositoryContext, RepositoryMap
from app.schemas.plan import ImplementationPlan, PlanAction, RiskLevel
from app.schemas.plan import PlanStep as PlanStepModel
from app.schemas.task import TaskStatus, TestResult
from app.services.task_service import _map_to_task_result
from app.workspace.manager import WorkspaceManager

TASK = "Do something useful here"


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


def _passed() -> TestResult:
    return TestResult(passed=True, exit_code=0, stdout="3 passed in 0.1s", stderr="", duration_seconds=0.1)


def _failed(summary: str = "1 failed, 2 passed in 0.1s") -> TestResult:
    return TestResult(
        passed=False,
        exit_code=1,
        stdout=f"FAILED tests/test_x.py::test_x - AssertionError\n{summary}\n",
        stderr="",
        duration_seconds=0.1,
    )


class FakeCodingStage:
    """Stands in for run_coding_stage; each call applies the next edit."""

    def __init__(self, workspace: WorkspaceManager, edits: list[Callable[[WorkspaceManager], None] | Exception]):
        self.workspace = workspace
        self.edits = edits
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> CodingResult:
        self.calls.append(kwargs)
        edit = self.edits[len(self.calls) - 1]
        if isinstance(edit, Exception):
            raise edit
        edit(self.workspace)
        paths = self.workspace.get_modified_paths()
        attempt = kwargs.get("repair").attempt if kwargs.get("repair") else 0
        return CodingResult(
            changes_made=bool(paths),
            changed_paths=paths,
            diff=self.workspace.generate_diff(),
            message=f"coding call {attempt}",
        )


def _write(path: str, content: str) -> Callable[[WorkspaceManager], None]:
    return lambda ws: ws.write_file(path, content)


def _no_edit(ws: WorkspaceManager) -> None:
    return None


def _sandbox_executor() -> MagicMock:
    """Repairs require an IsolationLevel.SANDBOX executor; stages are patched, so it never runs."""
    executor = MagicMock(spec=TestExecutor)
    executor.isolation = IsolationLevel.SANDBOX
    return executor


async def _run(
    tmp_path: Path,
    edits: list[Any],
    test_results: list[TestResult],
    max_repair_attempts: int | None,
    session: Any = None,
) -> tuple[Any, FakeCodingStage, MagicMock]:
    (tmp_path / "app.py").write_text("value = 0\n")
    workspace = WorkspaceManager(tmp_path)
    coding = FakeCodingStage(workspace, edits)
    testing = MagicMock(side_effect=test_results)
    orchestrator = Orchestrator(
        executor=_sandbox_executor(), session=session, max_repair_attempts=max_repair_attempts
    )
    with (
        patch("app.orchestration.orchestrator.run_analysis_stage", return_value=_make_context()),
        patch("app.orchestration.orchestrator.run_planning_stage", new_callable=AsyncMock, return_value=_make_plan()),
        patch("app.orchestration.orchestrator.run_coding_stage", side_effect=coding.__call__),
        patch("app.orchestration.orchestrator.run_testing_stage", testing),
    ):
        result = await orchestrator.run(workspace, TASK)
    return result, coding, testing


# ── Configuration ────────────────────────────────────────────────────────────


def test_repair_attempts_default_to_zero(monkeypatch):
    monkeypatch.delenv("CODEFORGE_MAX_REPAIR_ATTEMPTS", raising=False)
    assert Settings(_env_file=None).codeforge_max_repair_attempts == 0


def test_repair_attempts_env_override(monkeypatch):
    monkeypatch.setenv("CODEFORGE_TEST_EXECUTION_MODE", "sandbox")
    monkeypatch.setenv("CODEFORGE_MAX_REPAIR_ATTEMPTS", "3")
    assert Settings(_env_file=None).codeforge_max_repair_attempts == 3


@pytest.mark.parametrize("value", [-1, 6])
def test_repair_attempts_reject_out_of_range(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, codeforge_max_repair_attempts=value)


@pytest.mark.parametrize("value", [0, 5])
def test_repair_attempts_accept_bounds(value):
    loaded = Settings(
        _env_file=None, codeforge_test_execution_mode="sandbox", codeforge_max_repair_attempts=value
    )
    assert loaded.codeforge_max_repair_attempts == value


@pytest.mark.parametrize("value", [-1, 6])
def test_orchestrator_rejects_out_of_range_repair_attempts(value):
    with pytest.raises(ValueError):
        Orchestrator(executor=_sandbox_executor(), max_repair_attempts=value)


def test_testing_can_return_to_coding_and_terminal_states_stay_terminal():
    assert is_valid_transition(WorkflowStatus.TESTING, WorkflowStatus.CODING)
    assert not is_valid_transition(WorkflowStatus.COMPLETED, WorkflowStatus.CODING)
    assert not is_valid_transition(WorkflowStatus.FAILED, WorkflowStatus.CODING)


# ── Loop outcomes ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_test_run_passes_without_repair(tmp_path: Path):
    result, coding, testing = await _run(tmp_path, [_write("app.py", "value = 1\n")], [_passed()], 2)

    assert result.workflow_status == WorkflowStatus.COMPLETED
    assert len(coding.calls) == 1
    assert coding.calls[0].get("repair") is None
    assert testing.call_count == 1
    assert result.repair_attempts_used == 0
    assert result.final_message == "coding call 0"


@pytest.mark.asyncio
async def test_failure_then_successful_repair(tmp_path: Path):
    result, coding, testing = await _run(
        tmp_path,
        [_write("app.py", "value = 1\n"), _write("helper.py", "def helper():\n    return 1\n")],
        [_failed(), _passed()],
        2,
    )

    assert result.workflow_status == WorkflowStatus.COMPLETED
    assert len(coding.calls) == 2
    assert testing.call_count == 2
    repair = coding.calls[1]["repair"]
    assert isinstance(repair, RepairRequest)
    assert (repair.attempt, repair.max_attempts) == (1, 2)
    assert repair.failed_test_result.exit_code == 1
    assert "+value = 1" in repair.current_diff
    assert result.repair_attempts_used == 1
    assert result.test_result is not None and result.test_result.passed
    assert result.changed_files == ["app.py", "helper.py"]
    assert "+value = 1" in result.diff and "+def helper():" in result.diff
    assert result.final_message.startswith("Tests passed after 1 repair attempt(s).")
    assert _map_to_task_result(result).status == TaskStatus.success
    assert _map_to_task_result(result).repair_attempts_used == 1


@pytest.mark.asyncio
async def test_repairs_exhausted_with_tests_still_failing(tmp_path: Path):
    result, coding, testing = await _run(
        tmp_path,
        [_write("app.py", "value = 1\n"), _write("app.py", "value = 2\n"), _write("app.py", "value = 3\n")],
        [_failed(), _failed(), _failed("2 failed, 1 passed in 0.1s")],
        2,
    )

    assert result.workflow_status == WorkflowStatus.FAILED
    assert len(coding.calls) == 3
    assert testing.call_count == 3
    assert result.repair_attempts_used == 2
    assert result.failure_reason.startswith("Tests failed (exit_code=1) after 2 repair attempt(s).")
    assert "2 failed, 1 passed in 0.1s" in result.failure_reason
    assert result.test_result is not None and not result.test_result.passed
    assert "2 failed" in result.test_result.stdout
    assert "+value = 3" in result.diff
    assert _map_to_task_result(result).status == TaskStatus.failure


@pytest.mark.asyncio
async def test_zero_repair_budget_keeps_previous_behaviour(tmp_path: Path):
    result, coding, testing = await _run(tmp_path, [_write("app.py", "value = 1\n")], [_failed()], 0)

    assert result.workflow_status == WorkflowStatus.FAILED
    assert len(coding.calls) == 1
    assert testing.call_count == 1
    assert result.failure_reason == "Tests failed (exit_code=1)."
    assert result.repair_attempts_used == 0
    assert _map_to_task_result(result).status == TaskStatus.failure


@pytest.mark.asyncio
async def test_default_settings_make_no_repair_attempt(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "codeforge_max_repair_attempts", 0)
    result, coding, testing = await _run(tmp_path, [_write("app.py", "value = 1\n")], [_failed()], None)

    assert len(coding.calls) == 1
    assert testing.call_count == 1
    assert result.failure_reason == "Tests failed (exit_code=1)."


@pytest.mark.asyncio
async def test_configured_repair_attempts_are_read_at_run_time(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "codeforge_max_repair_attempts", 1)
    result, coding, testing = await _run(
        tmp_path, [_write("app.py", "value = 1\n"), _write("app.py", "value = 2\n")], [_failed(), _passed()], None
    )

    assert result.workflow_status == WorkflowStatus.COMPLETED
    assert len(coding.calls) == 2


@pytest.mark.asyncio
async def test_agent_error_during_repair_stops_without_retesting(tmp_path: Path):
    result, coding, testing = await _run(
        tmp_path,
        [_write("app.py", "value = 1\n"), CodingStageError("Coding agent failed: upstream 500")],
        [_failed()],
        3,
    )

    assert result.workflow_status == WorkflowStatus.FAILED
    assert len(coding.calls) == 2
    assert testing.call_count == 1
    assert result.failure_reason.startswith("Repair attempt 1/3 failed: Coding agent failed: upstream 500")
    assert "its edits were not re-tested" in result.failure_reason
    assert result.repair_attempts_used == 1
    assert result.test_result is not None and not result.test_result.passed
    assert result.changed_files == ["app.py"]
    assert "+value = 1" in result.diff
    assert _map_to_task_result(result).status == TaskStatus.error


@pytest.mark.asyncio
async def test_turn_limit_during_repair_reports_repair_phase(tmp_path: Path):
    error = CodingTurnLimitError(
        max_turns=25, phase="repair attempt 1/2", tool_call_counts={"read_file": 25}, changed_file_count=1
    )
    result, coding, testing = await _run(
        tmp_path, [_write("app.py", "value = 1\n"), error], [_failed()], 2
    )

    assert result.workflow_status == WorkflowStatus.FAILED
    assert testing.call_count == 1
    assert "during repair attempt 1/2" in result.failure_reason
    assert "CODEFORGE_CODING_MAX_TURNS" in result.failure_reason
    assert "its edits were not re-tested" in result.failure_reason
    assert len(result.failure_reason) <= MAX_FAILURE_REASON_LEN
    assert _map_to_task_result(result).status == TaskStatus.error


@pytest.mark.asyncio
async def test_repair_with_partial_edits_reports_current_workspace(tmp_path: Path):
    def edit_then_fail(ws: WorkspaceManager) -> None:
        ws.write_file("partial.py", "x = 1\n")
        raise CodingStageError("Coding agent failed: boom")

    result, coding, testing = await _run(
        tmp_path, [_write("app.py", "value = 1\n"), edit_then_fail], [_failed()], 1
    )

    assert result.workflow_status == WorkflowStatus.FAILED
    assert result.changed_files == ["app.py", "partial.py"]
    assert "+x = 1" in result.diff


@pytest.mark.asyncio
async def test_repair_without_changes_stops_early(tmp_path: Path):
    result, coding, testing = await _run(
        tmp_path, [_write("app.py", "value = 1\n"), _no_edit], [_failed()], 3
    )

    assert result.workflow_status == WorkflowStatus.FAILED
    assert len(coding.calls) == 2
    assert testing.call_count == 1
    assert result.failure_reason == "Tests failed (exit_code=1); repair attempt 1/3 made no changes."
    assert result.repair_attempts_used == 1
    assert _map_to_task_result(result).status == TaskStatus.failure


@pytest.mark.asyncio
async def test_loop_is_bounded_by_the_hard_limit(tmp_path: Path):
    edits = [_write("app.py", f"value = {i}\n") for i in range(1, 7)]
    result, coding, testing = await _run(tmp_path, edits, [_failed() for _ in range(6)], 5)

    assert result.workflow_status == WorkflowStatus.FAILED
    assert len(coding.calls) == 6
    assert testing.call_count == 6
    assert result.repair_attempts_used == 5


@pytest.mark.asyncio
async def test_out_of_range_setting_is_clamped_to_the_hard_limit(tmp_path: Path, monkeypatch):
    # Settings validation rejects this value; the clamp guards direct mutation.
    monkeypatch.setattr(settings, "codeforge_max_repair_attempts", 50)
    edits = [_write("app.py", f"value = {i}\n") for i in range(1, 7)]
    result, coding, testing = await _run(tmp_path, edits, [_failed() for _ in range(6)], None)

    assert testing.call_count == 6
    assert result.repair_attempts_used == 5


@pytest.mark.asyncio
async def test_test_runner_error_during_repair_cycle(tmp_path: Path):
    from app.orchestration.stages import TestingStageError

    result, coding, testing = await _run(
        tmp_path,
        [_write("app.py", "value = 1\n"), _write("app.py", "value = 2\n")],
        [_failed(), TestingStageError("runner exploded")],
        2,
    )

    assert result.workflow_status == WorkflowStatus.FAILED
    assert result.failure_reason.startswith("Test runner infrastructure error:")
    assert result.repair_attempts_used == 1


# ── Ownership ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ownership_loss_between_attempts_propagates(tmp_path: Path):
    checks = 0

    async def lose_ownership() -> None:
        nonlocal checks
        checks += 1
        raise OwnershipLostError("lease lost")

    token = set_async_ownership_verifier(lose_ownership)
    try:
        with pytest.raises(OwnershipLostError):
            await _run(tmp_path, [_write("app.py", "value = 1\n"), _write("app.py", "value = 2\n")], [_failed()], 2)
    finally:
        reset_async_ownership_verifier(token)
    assert checks == 1


@pytest.mark.asyncio
async def test_github_execution_does_not_wrap_ownership_loss(monkeypatch, tmp_path: Path):
    from app.execution.github_execution import GitHubExecutionService
    from app.schemas.task import ExecutionTarget, TaskRequest
    from tests.conftest import install_github_doubles, repository_access_for

    service = GitHubExecutionService.__new__(GitHubExecutionService)
    client = MagicMock()
    client.get_repository = AsyncMock(return_value=MagicMock(default_branch="main"))
    client.get_branch = AsyncMock(return_value=MagicMock(sha="abc123"))
    client.create_branch = AsyncMock()
    service.access = repository_access_for("o/r")
    install_github_doubles(monkeypatch, client)
    service._orchestrator = MagicMock()
    service._orchestrator.run = AsyncMock(side_effect=OwnershipLostError("lease lost"))

    git_wrapper = MagicMock()
    monkeypatch.setattr("app.execution.github_execution.SafeGitWrapper", MagicMock(return_value=git_wrapper))
    monkeypatch.setattr(settings, "codeforge_github_clone_root", str(tmp_path))

    request = TaskRequest(execution_target=ExecutionTarget.github, github_repository="o/r", description="Fix the thing")
    with pytest.raises(OwnershipLostError):
        await service.execute(request, "task-1")
    service._orchestrator.run.assert_awaited_once()


# ── Prompt and observability ─────────────────────────────────────────────────


def test_repair_prompt_bounds_output_and_keeps_the_tail():
    huge = "noise line\n" * 20000 + "=== 1 failed, 2 passed in 0.1s ===\n"
    repair = RepairRequest(
        attempt=1,
        max_attempts=2,
        failed_test_result=TestResult(passed=False, exit_code=1, stdout=huge, stderr="", duration_seconds=1.0),
        current_diff="+value = 1\n",
    )
    prompt = build_repair_prompt(repair)

    assert "REPAIR ATTEMPT 1 of 2" in prompt
    assert "exit_code=1" in prompt
    assert "=== 1 failed, 2 passed in 0.1s ===" in prompt
    assert "+value = 1" in prompt
    assert "Do not delete, skip or weaken tests" in prompt
    assert len(prompt.encode("utf-8")) < REPAIR_FEEDBACK_MAX_BYTES + 40 * 1024


@pytest.mark.asyncio
async def test_coding_stage_adds_repair_section_and_phase(tmp_path: Path):
    workspace = WorkspaceManager(tmp_path)
    repair = RepairRequest(attempt=2, max_attempts=3, failed_test_result=_failed(), current_diff="")

    class _Result:
        final_output = "fixed"

    with (
        patch("app.orchestration.stages.make_coding_agent"),
        patch("app.orchestration.stages.Runner.run", new_callable=AsyncMock, return_value=_Result()) as run,
    ):
        await run_coding_stage(
            "task", _make_context(), _make_plan(), workspace, _sandbox_executor(), "model", max_turns=5, repair=repair
        )
    prompt = run.await_args.args[1]
    assert "REPAIR ATTEMPT 2 of 3" in prompt
    assert "Original Task description:\ntask" in prompt

    from agents.exceptions import MaxTurnsExceeded

    with (
        patch("app.orchestration.stages.make_coding_agent"),
        patch("app.orchestration.stages.Runner.run", new_callable=AsyncMock, side_effect=MaxTurnsExceeded("x")),
    ):
        with pytest.raises(CodingTurnLimitError, match="during repair attempt 2/3"):
            await run_coding_stage(
                "task", _make_context(), _make_plan(), workspace, _sandbox_executor(), "model", max_turns=5, repair=repair
            )


@pytest.mark.asyncio
async def test_repair_attempts_are_recorded_as_repair_stages(tmp_path: Path):
    with patch("app.observability.tracing.record_event", new_callable=AsyncMock) as record:
        result, coding, testing = await _run(
            tmp_path,
            [_write("app.py", "value = 1\n"), _write("app.py", "value = 2\n")],
            [_failed(), _passed()],
            1,
            session=MagicMock(),
        )

    assert result.workflow_status == WorkflowStatus.COMPLETED
    stages = [(c.args[1].value, c.kwargs["stage"]) for c in record.await_args_list]
    assert stages == [
        ("STAGE_STARTED", "analyzing"),
        ("STAGE_COMPLETED", "analyzing"),
        ("STAGE_STARTED", "planning"),
        ("STAGE_COMPLETED", "planning"),
        ("STAGE_STARTED", "coding"),
        ("STAGE_COMPLETED", "coding"),
        ("STAGE_STARTED", "testing"),
        ("STAGE_COMPLETED", "testing"),
        ("STAGE_STARTED", "repair"),
        ("STAGE_COMPLETED", "repair"),
        ("STAGE_STARTED", "testing"),
        ("STAGE_COMPLETED", "testing"),
    ]


@pytest.mark.asyncio
async def test_plan_only_run_never_codes_or_repairs(tmp_path: Path):
    coding = AsyncMock()
    testing = MagicMock()
    orchestrator = Orchestrator(executor=_sandbox_executor(), max_repair_attempts=3)
    with (
        patch("app.orchestration.orchestrator.run_analysis_stage", return_value=_make_context()),
        patch("app.orchestration.orchestrator.run_planning_stage", new_callable=AsyncMock, return_value=_make_plan()),
        patch("app.orchestration.orchestrator.run_coding_stage", coding),
        patch("app.orchestration.orchestrator.run_testing_stage", testing),
    ):
        result = await orchestrator.run(WorkspaceManager(tmp_path), TASK, stop_after_plan=True)

    assert result.workflow_status == WorkflowStatus.COMPLETED
    coding.assert_not_called()
    testing.assert_not_called()


@pytest.mark.asyncio
async def test_approved_plan_run_can_repair(tmp_path: Path):
    (tmp_path / "app.py").write_text("value = 0\n")
    workspace = WorkspaceManager(tmp_path)
    coding = FakeCodingStage(workspace, [_write("app.py", "value = 1\n"), _write("app.py", "value = 2\n")])
    planning = AsyncMock()
    orchestrator = Orchestrator(executor=_sandbox_executor(), max_repair_attempts=1)
    with (
        patch("app.orchestration.orchestrator.run_analysis_stage", return_value=_make_context()),
        patch("app.orchestration.orchestrator.run_planning_stage", planning),
        patch("app.orchestration.orchestrator.run_coding_stage", side_effect=coding.__call__),
        patch("app.orchestration.orchestrator.run_testing_stage", MagicMock(side_effect=[_failed(), _passed()])),
    ):
        result = await orchestrator.run(workspace, TASK, initial_plan=_make_plan())

    assert result.workflow_status == WorkflowStatus.COMPLETED
    planning.assert_not_called()
    assert result.repair_attempts_used == 1
