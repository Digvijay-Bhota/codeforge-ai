"""Phase 5 orchestration tests.

No real LLM calls are made.  All external agents are mocked/faked.
Each test covers one explicit scenario from the specification.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.orchestration.analyst import RepositoryAnalyst, RepositoryAnalystError
from app.orchestration.models import (
    CodingResult,
    FinalTaskResult,
    WorkflowState,
    WorkflowStatus,
    is_valid_transition,
)
from app.orchestration.orchestrator import Orchestrator
from app.orchestration.stages import (
    AnalysisStageError,
    CodingStageError,
    PlanningStageError,
    TestingStageError,
    run_analysis_stage,
)
from app.repository.models import GitMetadata, RepositoryContext, RepositoryMap
from app.schemas.plan import ImplementationPlan, PlanAction, RiskLevel
from app.schemas.plan import PlanStep as PlanStepModel
from app.schemas.task import TestResult
from app.workspace.manager import WorkspaceManager
from app.workspace.runner import TestRunner

# ── Fixtures / helpers ───────────────────────────────────────────────────────


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


def _make_passing_test_result() -> TestResult:
    return TestResult(
        passed=True, exit_code=0, stdout="1 passed", stderr="", duration_seconds=0.5
    )


def _make_failing_test_result() -> TestResult:
    return TestResult(
        passed=False, exit_code=1, stdout="1 failed", stderr="", duration_seconds=0.5
    )


def _make_orchestrator(runner: TestRunner | None = None) -> Orchestrator:
    return Orchestrator(runner=runner or MagicMock(spec=TestRunner))


def _make_workspace(tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(tmp_path)


# ── 1. Initial workflow state ─────────────────────────────────────────────────


def test_initial_workflow_state():
    state = WorkflowState(task_id="abc", task_description="Do something")
    assert state.status == WorkflowStatus.PENDING
    assert state.repository_context is None
    assert state.implementation_plan is None
    assert state.coding_result is None
    assert state.test_result is None
    assert state.failure_reason is None


# ── 2. State machine: valid transitions ──────────────────────────────────────


def test_valid_state_transitions():
    expected = [
        (WorkflowStatus.PENDING, WorkflowStatus.ANALYZING),
        (WorkflowStatus.ANALYZING, WorkflowStatus.PLANNING),
        (WorkflowStatus.PLANNING, WorkflowStatus.CODING),
        (WorkflowStatus.CODING, WorkflowStatus.TESTING),
        (WorkflowStatus.TESTING, WorkflowStatus.COMPLETED),
        # FAILED is valid from all non-terminal states
        (WorkflowStatus.PENDING, WorkflowStatus.FAILED),
        (WorkflowStatus.ANALYZING, WorkflowStatus.FAILED),
        (WorkflowStatus.PLANNING, WorkflowStatus.FAILED),
        (WorkflowStatus.CODING, WorkflowStatus.FAILED),
        (WorkflowStatus.TESTING, WorkflowStatus.FAILED),
    ]
    for from_s, to_s in expected:
        assert is_valid_transition(from_s, to_s), f"{from_s!r} → {to_s!r} should be valid"


# ── 3. Invalid state transition is rejected ───────────────────────────────────


def test_invalid_state_transition_raises():
    state = WorkflowState(task_id="x", task_description="t")
    # Cannot skip from PENDING straight to PLANNING
    with pytest.raises(ValueError, match="Invalid workflow transition"):
        state.transition(WorkflowStatus.PLANNING)


def test_terminal_state_cannot_transition():
    state = WorkflowState(task_id="x", task_description="t")
    state.status = WorkflowStatus.COMPLETED
    with pytest.raises(ValueError, match="Invalid workflow transition"):
        state.transition(WorkflowStatus.ANALYZING)


# ── 4. Repository Analyst: success ────────────────────────────────────────────


def test_repository_analyst_success(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    analyst = RepositoryAnalyst()
    # build_repository_context is deterministic; no mocking needed for an empty repo.
    context = analyst.analyse(workspace, "Fix authentication")
    assert isinstance(context, RepositoryContext)
    assert context.repository_map.root == str(tmp_path)


# ── 5. Repository Analyst failure propagates as AnalysisStageError ──────────


def test_analysis_stage_error_on_analyst_failure(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    with patch(
        "app.orchestration.stages.RepositoryAnalyst.analyse",
        side_effect=RepositoryAnalystError("scan crashed"),
    ):
        with pytest.raises(AnalysisStageError, match="scan crashed"):
            run_analysis_stage(workspace, "task")


# ── 6. Successful complete workflow ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_successful_complete_workflow(tmp_path: Path):
    mock_runner = MagicMock(spec=TestRunner)
    mock_runner.run.return_value = _make_passing_test_result()
    orchestrator = Orchestrator(runner=mock_runner)
    workspace = _make_workspace(tmp_path)

    with (
        patch(
            "app.orchestration.orchestrator.run_analysis_stage",
            return_value=_make_context(),
        ),
        patch(
            "app.orchestration.orchestrator.run_planning_stage",
            new_callable=AsyncMock,
            return_value=_make_plan(),
        ),
        patch(
            "app.orchestration.orchestrator.run_coding_stage",
            new_callable=AsyncMock,
            return_value=CodingResult(
                changes_made=True,
                changed_paths=["foo.py"],
                diff="--- a\n+++ b\n",
                message="Done",
            ),
        ),
        patch(
            "app.orchestration.orchestrator.run_testing_stage",
            return_value=_make_passing_test_result(),
        ),
    ):
        result = await orchestrator.run(workspace, "Fix the bug")

    assert result.workflow_status == WorkflowStatus.COMPLETED
    assert result.failure_reason is None
    assert "foo.py" in result.changed_files
    assert result.test_result is not None
    assert result.test_result.passed is True


# ── 7. Repository Analyst failure → workflow FAILED ───────────────────────────


@pytest.mark.asyncio
async def test_workflow_fails_on_analyst_failure(tmp_path: Path):
    orchestrator = _make_orchestrator()
    workspace = _make_workspace(tmp_path)

    with patch(
        "app.orchestration.orchestrator.run_analysis_stage",
        side_effect=AnalysisStageError("disk unavailable"),
    ):
        result = await orchestrator.run(workspace, "Do something useful here")

    assert result.workflow_status == WorkflowStatus.FAILED
    assert "disk unavailable" in result.failure_reason


# ── 8. Planner failure → workflow FAILED ─────────────────────────────────────


@pytest.mark.asyncio
async def test_workflow_fails_on_planner_failure(tmp_path: Path):
    orchestrator = _make_orchestrator()
    workspace = _make_workspace(tmp_path)

    with (
        patch(
            "app.orchestration.orchestrator.run_analysis_stage",
            return_value=_make_context(),
        ),
        patch(
            "app.orchestration.orchestrator.run_planning_stage",
            new_callable=AsyncMock,
            side_effect=PlanningStageError("LLM timeout"),
        ),
    ):
        result = await orchestrator.run(workspace, "Do something useful here")

    assert result.workflow_status == WorkflowStatus.FAILED
    assert "LLM timeout" in result.failure_reason


# ── 9. Invalid ImplementationPlan (validation failure) → workflow FAILED ──────


@pytest.mark.asyncio
async def test_workflow_fails_on_invalid_plan(tmp_path: Path):
    orchestrator = _make_orchestrator()
    workspace = _make_workspace(tmp_path)

    with (
        patch(
            "app.orchestration.orchestrator.run_analysis_stage",
            return_value=_make_context(),
        ),
        patch(
            "app.orchestration.orchestrator.run_planning_stage",
            new_callable=AsyncMock,
            side_effect=PlanningStageError("Plan validation failed: Plan must contain at least one step"),
        ),
    ):
        result = await orchestrator.run(workspace, "Do something useful here")

    assert result.workflow_status == WorkflowStatus.FAILED
    assert "Plan" in result.failure_reason


# ── 10. Coding Agent failure → workflow FAILED ────────────────────────────────


@pytest.mark.asyncio
async def test_workflow_fails_on_coding_failure(tmp_path: Path):
    orchestrator = _make_orchestrator()
    workspace = _make_workspace(tmp_path)

    with (
        patch(
            "app.orchestration.orchestrator.run_analysis_stage",
            return_value=_make_context(),
        ),
        patch(
            "app.orchestration.orchestrator.run_planning_stage",
            new_callable=AsyncMock,
            return_value=_make_plan(),
        ),
        patch(
            "app.orchestration.orchestrator.run_coding_stage",
            new_callable=AsyncMock,
            side_effect=CodingStageError("agent crashed"),
        ),
    ):
        result = await orchestrator.run(workspace, "Do something useful here")

    assert result.workflow_status == WorkflowStatus.FAILED
    assert "agent crashed" in result.failure_reason


# ── 11. No-change coding result → workflow FAILED ─────────────────────────────


@pytest.mark.asyncio
async def test_workflow_fails_on_no_changes(tmp_path: Path):
    orchestrator = _make_orchestrator()
    workspace = _make_workspace(tmp_path)

    with (
        patch(
            "app.orchestration.orchestrator.run_analysis_stage",
            return_value=_make_context(),
        ),
        patch(
            "app.orchestration.orchestrator.run_planning_stage",
            new_callable=AsyncMock,
            return_value=_make_plan(),
        ),
        patch(
            "app.orchestration.orchestrator.run_coding_stage",
            new_callable=AsyncMock,
            return_value=CodingResult(
                changes_made=False,
                changed_paths=[],
                diff="",
                message="I did nothing",
            ),
        ),
    ):
        result = await orchestrator.run(workspace, "Do something useful here")

    assert result.workflow_status == WorkflowStatus.FAILED
    assert "without making any changes" in result.failure_reason.lower()


# ── 12. TestRunner failure → workflow FAILED ──────────────────────────────────


@pytest.mark.asyncio
async def test_workflow_fails_on_test_runner_error(tmp_path: Path):
    orchestrator = _make_orchestrator()
    workspace = _make_workspace(tmp_path)

    with (
        patch(
            "app.orchestration.orchestrator.run_analysis_stage",
            return_value=_make_context(),
        ),
        patch(
            "app.orchestration.orchestrator.run_planning_stage",
            new_callable=AsyncMock,
            return_value=_make_plan(),
        ),
        patch(
            "app.orchestration.orchestrator.run_coding_stage",
            new_callable=AsyncMock,
            return_value=CodingResult(
                changes_made=True,
                changed_paths=["x.py"],
                diff="",
                message="done",
            ),
        ),
        patch(
            "app.orchestration.orchestrator.run_testing_stage",
            side_effect=TestingStageError("pytest not found"),
        ),
    ):
        result = await orchestrator.run(workspace, "Do something useful here")

    assert result.workflow_status == WorkflowStatus.FAILED
    assert "pytest not found" in result.failure_reason


# ── 13. Tests failing → workflow FAILED (not COMPLETED) ───────────────────────


@pytest.mark.asyncio
async def test_workflow_fails_when_tests_fail(tmp_path: Path):
    orchestrator = _make_orchestrator()
    workspace = _make_workspace(tmp_path)

    with (
        patch(
            "app.orchestration.orchestrator.run_analysis_stage",
            return_value=_make_context(),
        ),
        patch(
            "app.orchestration.orchestrator.run_planning_stage",
            new_callable=AsyncMock,
            return_value=_make_plan(),
        ),
        patch(
            "app.orchestration.orchestrator.run_coding_stage",
            new_callable=AsyncMock,
            return_value=CodingResult(
                changes_made=True,
                changed_paths=["x.py"],
                diff="",
                message="done",
            ),
        ),
        patch(
            "app.orchestration.orchestrator.run_testing_stage",
            return_value=_make_failing_test_result(),
        ),
    ):
        result = await orchestrator.run(workspace, "Do something useful here")

    assert result.workflow_status == WorkflowStatus.FAILED
    assert result.test_result is not None
    assert result.test_result.passed is False


# ── 14. Workflow stops after failed stage (no subsequent stage runs) ───────────


@pytest.mark.asyncio
async def test_workflow_stops_after_failed_stage(tmp_path: Path):
    orchestrator = _make_orchestrator()
    workspace = _make_workspace(tmp_path)
    coding_called = False

    async def fake_coding(*args, **kwargs):
        nonlocal coding_called
        coding_called = True
        return CodingResult(changes_made=True, changed_paths=[], diff="", message="x")

    with (
        patch(
            "app.orchestration.orchestrator.run_analysis_stage",
            return_value=_make_context(),
        ),
        patch(
            "app.orchestration.orchestrator.run_planning_stage",
            new_callable=AsyncMock,
            side_effect=PlanningStageError("planner broke"),
        ),
        patch(
            "app.orchestration.orchestrator.run_coding_stage",
            side_effect=fake_coding,
        ),
    ):
        result = await orchestrator.run(workspace, "Do something useful here")

    assert result.workflow_status == WorkflowStatus.FAILED
    assert not coding_called, "Coding stage must not run after planner failure"


# ── 15. FinalTaskResult is correctly constructed ──────────────────────────────


@pytest.mark.asyncio
async def test_final_task_result_fields(tmp_path: Path):
    mock_runner = MagicMock(spec=TestRunner)
    mock_runner.run.return_value = _make_passing_test_result()
    orchestrator = Orchestrator(runner=mock_runner)
    workspace = _make_workspace(tmp_path)

    with (
        patch(
            "app.orchestration.orchestrator.run_analysis_stage",
            return_value=_make_context(),
        ),
        patch(
            "app.orchestration.orchestrator.run_planning_stage",
            new_callable=AsyncMock,
            return_value=_make_plan(),
        ),
        patch(
            "app.orchestration.orchestrator.run_coding_stage",
            new_callable=AsyncMock,
            return_value=CodingResult(
                changes_made=True,
                changed_paths=["a.py", "b.py"],
                diff="--- a\n+++ b\n",
                message="All done",
            ),
        ),
        patch(
            "app.orchestration.orchestrator.run_testing_stage",
            return_value=_make_passing_test_result(),
        ),
    ):
        result = await orchestrator.run(workspace, "Fix authentication logic here")

    assert isinstance(result, FinalTaskResult)
    assert result.task_id != ""
    assert result.workflow_status == WorkflowStatus.COMPLETED
    assert result.task_description == "Fix authentication logic here"
    assert result.plan_summary == "Fix the bug"
    assert result.failure_reason is None
    assert result.test_result is not None


# ── 16. Stack traces are not exposed ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_stack_traces_not_exposed(tmp_path: Path):
    orchestrator = _make_orchestrator()
    workspace = _make_workspace(tmp_path)

    with patch(
        "app.orchestration.orchestrator.run_analysis_stage",
        side_effect=AnalysisStageError("Traceback (most recent call last):\n  File x\nValueError: bad"),
    ):
        result = await orchestrator.run(workspace, "Do something useful here")

    assert result.workflow_status == WorkflowStatus.FAILED
    # The failure_reason must not start with a Python traceback header
    assert result.failure_reason is not None
    assert result.failure_reason.startswith("Traceback") is False
    # The actual content is still bounded
    assert len(result.failure_reason) <= 800


# ── 17. Output remains bounded ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_failure_reason_is_bounded(tmp_path: Path):
    orchestrator = _make_orchestrator()
    workspace = _make_workspace(tmp_path)
    huge_msg = "X" * 10_000

    with patch(
        "app.orchestration.orchestrator.run_analysis_stage",
        side_effect=AnalysisStageError(huge_msg),
    ):
        result = await orchestrator.run(workspace, "Do something useful here")

    assert result.workflow_status == WorkflowStatus.FAILED
    assert result.failure_reason is not None
    assert len(result.failure_reason) <= 800


# ── 18. Analyst: READ-only (no write calls occur) ────────────────────────────


def test_analyst_does_not_write_files(tmp_path: Path):
    """Analyst must not call any write methods on WorkspaceManager."""
    workspace = _make_workspace(tmp_path)
    original_write = workspace.write_file
    calls: list[str] = []

    def spy_write(path: str, content: str) -> None:
        calls.append(path)
        return original_write(path, content)

    workspace.write_file = spy_write  # type: ignore[method-assign]

    analyst = RepositoryAnalyst()
    analyst.analyse(workspace, "Fix something")
    assert calls == [], "RepositoryAnalyst must not write any files"


# ── 19. CodingResult: no changes → changes_made is False ─────────────────────


def test_coding_result_no_changes():
    result = CodingResult(
        changes_made=False, changed_paths=[], diff="", message="nothing to do"
    )
    assert result.changes_made is False
    assert result.changed_paths == []


# ── Plan Regression Test ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_implementation_plan_is_preserved_end_to_end(tmp_path: Path):
    """Proves the planner's multi-step plan survives end-to-end to TaskResult."""
    from app.services.task_service import _map_to_task_result

    orchestrator = _make_orchestrator()
    workspace = _make_workspace(tmp_path)

    # Use our fake planner that returns a multi-step plan
    plan = _make_plan()
    from app.schemas.plan import PlanAction
    from app.schemas.plan import PlanStep as PlanStepModel
    plan.steps.append(
        PlanStepModel(
            step_number=2,
            action=PlanAction.test,
            description="Run tests",
            rationale="To verify",
        )
    )
    assert len(plan.steps) > 1, "Precondition: plan must have multiple steps"

    with (
        patch(
            "app.orchestration.orchestrator.run_analysis_stage",
            return_value=_make_context(),
        ),
        patch(
            "app.orchestration.orchestrator.run_planning_stage",
            new_callable=AsyncMock,
            return_value=plan,
        ),
        patch(
            "app.orchestration.orchestrator.run_coding_stage",
            new_callable=AsyncMock,
            return_value=CodingResult(
                changes_made=True, changed_paths=["f.py"], diff="diff", message="done", deviations=""
            ),
        ),
        patch(
            "app.orchestration.orchestrator.run_testing_stage",
            return_value=TestResult(
                passed=True, exit_code=0, stdout="", stderr="", duration_seconds=1.0
            ),
        ),
    ):
        final = await orchestrator.run(workspace, "Do something")

    # 1. FinalTaskResult must contain the full plan
    assert final.workflow_status == WorkflowStatus.COMPLETED
    assert final.implementation_plan is not None
    assert final.implementation_plan.goal == plan.goal
    assert len(final.implementation_plan.steps) == len(plan.steps)

    # 2. The mapping to legacy TaskResult must preserve the multiple steps
    task_result = _map_to_task_result(final)
    assert len(task_result.plan) == len(plan.steps)
    assert task_result.plan[0].step == plan.steps[0].step_number
    assert task_result.plan[0].description == plan.steps[0].description


# ── 20. Output-bound at the API serialization boundary ───────────────────────
#
# These tests prove that the bound enforced by the Orchestrator is reflected
# in the actual serialized representation that reaches API consumers.
# The centralized bound is MAX_FAILURE_REASON_LEN (800 chars) in orchestrator.py.


@pytest.mark.asyncio
async def test_output_bound_oversized_failure_in_serialized_result(tmp_path: Path):
    """Oversized failure message must be bounded in the JSON-serialized FinalTaskResult.

    This tests the API boundary: model_dump_json() / model_dump() reflects the
    bounded values, not the raw unbounded input.
    """
    from app.orchestration.orchestrator import MAX_FAILURE_REASON_LEN

    orchestrator = _make_orchestrator()
    workspace = _make_workspace(tmp_path)
    huge_msg = "OVERFLOW" * 200  # 1600 chars — well above the 800-char bound

    with patch(
        "app.orchestration.orchestrator.run_analysis_stage",
        side_effect=AnalysisStageError(huge_msg),
    ):
        result = await orchestrator.run(workspace, "Do something useful here")

    assert result.workflow_status == WorkflowStatus.FAILED

    # Verify the in-memory model is bounded.
    assert result.failure_reason is not None
    assert len(result.failure_reason) <= MAX_FAILURE_REASON_LEN

    # Verify the serialized JSON representation is bounded — this is what the
    # API layer serializes and returns to callers.
    import json

    serialized = json.loads(result.model_dump_json())
    assert serialized["failure_reason"] is not None
    assert len(serialized["failure_reason"]) <= MAX_FAILURE_REASON_LEN

    # final_message also must not exceed the bound (it embeds failure_reason).
    assert len(serialized["final_message"]) <= MAX_FAILURE_REASON_LEN + len("Task failed: ")


@pytest.mark.asyncio
async def test_output_bound_normal_message_is_unchanged(tmp_path: Path):
    """Normal-sized output must pass through without modification."""
    from app.orchestration.orchestrator import MAX_FAILURE_REASON_LEN

    short_reason = "Analysis failed: disk read error"
    assert len(short_reason) < MAX_FAILURE_REASON_LEN  # precondition

    orchestrator = _make_orchestrator()
    workspace = _make_workspace(tmp_path)

    with patch(
        "app.orchestration.orchestrator.run_analysis_stage",
        side_effect=AnalysisStageError(short_reason),
    ):
        result = await orchestrator.run(workspace, "Do something useful here")

    assert result.workflow_status == WorkflowStatus.FAILED
    # The bounded reason embeds the stage prefix added by _fail():
    # "Repository analysis failed: Analysis failed: disk read error"
    assert short_reason in (result.failure_reason or "")


@pytest.mark.asyncio
async def test_output_bound_stack_trace_not_in_serialized_result(tmp_path: Path):
    """A raw Python traceback must not appear as the top-level failure_reason value.

    Simulates a stage raising an exception whose str() begins with 'Traceback'.
    The orchestrator wraps it in a stage-prefixed message, so the raw
    traceback string is not the value of failure_reason.
    """
    tb_msg = "Traceback (most recent call last):\n  File foo.py, line 1\nValueError: boom"
    orchestrator = _make_orchestrator()
    workspace = _make_workspace(tmp_path)

    with patch(
        "app.orchestration.orchestrator.run_analysis_stage",
        side_effect=AnalysisStageError(tb_msg),
    ):
        result = await orchestrator.run(workspace, "Do something useful here")

    assert result.workflow_status == WorkflowStatus.FAILED
    assert result.failure_reason is not None

    # The failure_reason must not be a raw traceback starting line.
    assert not result.failure_reason.startswith("Traceback")

    # Serialized check — same guarantee must hold in JSON.
    import json

    serialized = json.loads(result.model_dump_json())
    assert not serialized["failure_reason"].startswith("Traceback")


@pytest.mark.asyncio
async def test_output_bound_final_task_result_has_no_unbounded_fields(tmp_path: Path):
    """FinalTaskResult must not contain any field that bypasses the bound.

    Checks all string fields in the serialized result: no field value should
    exceed MAX_FAILURE_REASON_LEN * 2 due to orchestration-layer error handling
    (actual diff/test output from real tools is outside the orchestration bound
    and is not subject to this check; only orchestration error strings are).
    """
    from app.orchestration.orchestrator import MAX_FAILURE_REASON_LEN

    # Build a failed result directly through the orchestrator.
    huge_msg = "E" * 5000
    orchestrator = _make_orchestrator()
    workspace = _make_workspace(tmp_path)

    with patch(
        "app.orchestration.orchestrator.run_analysis_stage",
        side_effect=AnalysisStageError(huge_msg),
    ):
        result = await orchestrator.run(workspace, "Do something useful here")

    import json

    serialized = json.loads(result.model_dump_json())

    # The orchestration-controlled string fields must be bounded.
    assert len(serialized.get("failure_reason") or "") <= MAX_FAILURE_REASON_LEN
    assert len(serialized.get("final_message") or "") <= MAX_FAILURE_REASON_LEN + len("Task failed: ")
    assert len(serialized.get("plan_summary") or "") <= MAX_FAILURE_REASON_LEN


def test_stage_output_bounding(tmp_path: Path):
    """Proves the _bound_text logic correctly truncates oversized fields inside stages."""
    import asyncio

    from app.mcp.schemas import MAX_TOOL_OUTPUT_BYTES
    from app.orchestration.stages import run_coding_stage, run_testing_stage
    from app.workspace.runner import TestRunner

    workspace = _make_workspace(tmp_path)
    runner = TestRunner()

    huge_text = "X" * (MAX_TOOL_OUTPUT_BYTES + 1000)

    # Test test_runner bounding
    raw_test_result = TestResult(passed=True, exit_code=0, duration_seconds=1.0, stdout=huge_text, stderr=huge_text)
    with patch.object(runner, "run", return_value=raw_test_result):
        bounded_test_result = run_testing_stage(workspace, runner)
        assert len(bounded_test_result.stdout) <= MAX_TOOL_OUTPUT_BYTES + 100  # account for truncation marker
        assert len(bounded_test_result.stderr) <= MAX_TOOL_OUTPUT_BYTES + 100
        assert "output truncated" in bounded_test_result.stdout

    # Test coding stage bounding
    class FakeAgentResult:
        final_output = huge_text

    with (
        patch("app.orchestration.stages.make_coding_agent"),
        patch("app.orchestration.stages.Runner.run", new_callable=AsyncMock, return_value=FakeAgentResult()),
        patch.object(workspace, "get_modified_paths", return_value=["test.py"]),
        patch.object(workspace, "generate_diff", return_value=huge_text)
    ):
        bounded_coding_result = asyncio.run(
            run_coding_stage(
                "task", _make_context(), _make_plan(), workspace, runner, "model"
            )
        )
        assert len(bounded_coding_result.diff) <= MAX_TOOL_OUTPUT_BYTES + 100
        assert len(bounded_coding_result.message) <= MAX_TOOL_OUTPUT_BYTES + 100
        assert "output truncated" in bounded_coding_result.diff
