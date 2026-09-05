"""Phase 5: Stage execution helpers used by the Orchestrator.

Each function corresponds to one explicit workflow stage.  Functions are pure
(aside from logging) — they accept typed inputs and return typed outputs, or
raise a stage-specific exception.

This keeps the Orchestrator thin: it owns state transitions; these functions
own the per-stage execution detail.
"""

from __future__ import annotations

import logging

from agents import Runner
from app.agents.coding_agent import AgentFinalOutput, make_coding_agent
from app.agents.planner import make_planner_agent
from app.orchestration.analyst import RepositoryAnalyst, RepositoryAnalystError
from app.orchestration.models import CodingResult
from app.planning.validator import PlanValidationError, validate_plan
from app.repository.context import format_context_for_prompt
from app.repository.models import RepositoryContext
from app.schemas.plan import ImplementationPlan
from app.schemas.task import TestResult
from app.workspace.manager import WorkspaceManager
from app.workspace.runner import TestRunner

logger = logging.getLogger(__name__)

# Bounded safe message length exposed upward (characters).
MAX_STAGE_ERROR_MSG = 500


def _safe_msg(exc: Exception) -> str:
    """Return a bounded, sanitised error string — no raw stack traces."""
    return str(exc)[:MAX_STAGE_ERROR_MSG]


def _bound_text(text: str) -> str:
    """Deterministically truncate text to MAX_TOOL_OUTPUT_BYTES safely.

    This reuses the exact byte-aware safety limit from the MCP layer,
    ensuring strings generated outside the MCP layer (like diffs and pytest
    output) are equally safe before serialization.
    """
    from app.mcp.schemas import MAX_TOOL_OUTPUT_BYTES

    output_bytes = text.encode("utf-8")
    if len(output_bytes) <= MAX_TOOL_OUTPUT_BYTES:
        return text

    marker = f"\n... (output truncated, exceeded {MAX_TOOL_OUTPUT_BYTES} bytes limit)"
    marker_bytes = marker.encode("utf-8")

    if len(marker_bytes) >= MAX_TOOL_OUTPUT_BYTES:
        return marker_bytes[:MAX_TOOL_OUTPUT_BYTES].decode("utf-8", errors="ignore")

    allowed_bytes = MAX_TOOL_OUTPUT_BYTES - len(marker_bytes)
    truncated = output_bytes[:allowed_bytes].decode("utf-8", errors="ignore")
    return truncated + marker


# ── Stage 1: Repository Analysis ────────────────────────────────────────────


class AnalysisStageError(Exception):
    """Raised when the Repository Analyst stage fails."""


def run_analysis_stage(
    workspace: WorkspaceManager, task_description: str
) -> RepositoryContext:
    """Run the Repository Analyst and return a :class:`RepositoryContext`.

    Raises:
        :class:`AnalysisStageError` on any failure.
    """
    try:
        analyst = RepositoryAnalyst()
        return analyst.analyse(workspace, task_description)
    except RepositoryAnalystError as exc:
        raise AnalysisStageError(_safe_msg(exc)) from exc
    except Exception as exc:
        raise AnalysisStageError(_safe_msg(exc)) from exc


# ── Stage 2: Planning ────────────────────────────────────────────────────────


class PlanningStageError(Exception):
    """Raised when the Planner stage fails (includes validation failures)."""


async def run_planning_stage(
    task_description: str,
    repository_context: RepositoryContext,
    model: str,
) -> ImplementationPlan:
    """Run the Planner Agent and validate the resulting plan.

    Raises:
        :class:`PlanningStageError` on agent failure, wrong output type, or
        plan validation failure.
    """
    planner = make_planner_agent(model=model)
    context_str = format_context_for_prompt(repository_context)
    prompt = (
        f"{context_str}\n\n"
        f"Task description:\n{task_description}"
    )
    logger.info("PlanningStage: invoking planner model=%s", model)
    try:
        result = await Runner.run(planner, prompt)
    except Exception as exc:
        raise PlanningStageError(f"Planner agent failed: {_safe_msg(exc)}") from exc

    if not isinstance(result.final_output, ImplementationPlan):
        raise PlanningStageError(
            f"Planner returned unexpected type: {type(result.final_output).__name__}"
        )

    plan = result.final_output

    try:
        validate_plan(plan)
    except PlanValidationError as exc:
        raise PlanningStageError(f"Plan validation failed: {_safe_msg(exc)}") from exc

    logger.info(
        "PlanningStage: plan validated — goal=%r steps=%d",
        plan.goal[:80],
        len(plan.steps),
    )
    return plan


# ── Stage 3: Coding ─────────────────────────────────────────────────────────


class CodingStageError(Exception):
    """Raised when the Coding Agent stage fails."""


async def run_coding_stage(
    task_description: str,
    repository_context: RepositoryContext,
    implementation_plan: ImplementationPlan,
    workspace: WorkspaceManager,
    runner: TestRunner,
    model: str,
) -> CodingResult:
    """Run the Coding Agent and return a typed :class:`CodingResult`.

    The agent will NOT make any changes if it cannot determine a safe edit.
    A :class:`CodingResult` with ``changes_made=False`` is a valid — but
    incomplete — outcome; the Orchestrator treats it as FAILED.

    Raises:
        :class:`CodingStageError` on agent execution failure.
    """
    agent = make_coding_agent(workspace=workspace, runner=runner, model=model)
    context_str = format_context_for_prompt(repository_context)
    plan_str = implementation_plan.model_dump_json(indent=2)
    prompt = (
        f"Repository Context:\n{context_str}\n\n"
        f"Implementation Plan:\n{plan_str}\n\n"
        f"Original Task description:\n{task_description}"
    )

    logger.info("CodingStage: invoking coding agent model=%s", model)
    try:
        result = await Runner.run(agent, prompt)
    except Exception as exc:
        raise CodingStageError(f"Coding agent failed: {_safe_msg(exc)}") from exc

    if isinstance(result.final_output, AgentFinalOutput):
        message = result.final_output.message
    else:
        message = str(result.final_output)

    modified_paths = workspace.get_modified_paths()
    diff = workspace.generate_diff()

    coding_result = CodingResult(
        changes_made=bool(modified_paths),
        changed_paths=modified_paths,
        diff=_bound_text(diff),
        message=_bound_text(message),
        deviations="",
    )
    logger.info(
        "CodingStage: finished changes_made=%s paths=%d",
        coding_result.changes_made,
        len(coding_result.changed_paths),
    )
    return coding_result


# ── Stage 4: Testing ────────────────────────────────────────────────────────


class TestingStageError(Exception):
    """Raised when the TestRunner itself cannot execute (not a test failure)."""


def run_testing_stage(
    workspace: WorkspaceManager, runner: TestRunner
) -> TestResult:
    """Run the test suite in *workspace* and return a :class:`TestResult`.

    Note: A ``TestResult`` with ``passed=False`` is *not* a
    :class:`TestingStageError` — it means tests ran but failed.  The
    Orchestrator transitions to FAILED in that case; this function only raises
    on runner-infrastructure errors.

    Raises:
        :class:`TestingStageError` if the runner cannot execute at all.
    """
    logger.info("TestingStage: running test suite in %s", workspace.root)
    try:
        raw_result = runner.run(workspace.root)
        test_result = TestResult(
            passed=raw_result.passed,
            exit_code=raw_result.exit_code,
            duration_seconds=raw_result.duration_seconds,
            stdout=_bound_text(raw_result.stdout),
            stderr=_bound_text(raw_result.stderr),
        )
    except Exception as exc:
        raise TestingStageError(
            f"TestRunner infrastructure error: {_safe_msg(exc)}"
        ) from exc

    logger.info(
        "TestingStage: finished passed=%s exit_code=%d duration=%.2fs",
        test_result.passed,
        test_result.exit_code,
        test_result.duration_seconds,
    )
    return test_result
