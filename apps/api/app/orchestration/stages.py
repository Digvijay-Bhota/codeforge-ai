"""Phase 5: Stage execution helpers used by the Orchestrator.

Each function corresponds to one explicit workflow stage.  Functions are pure
(aside from logging) — they accept typed inputs and return typed outputs, or
raise a stage-specific exception.

This keeps the Orchestrator thin: it owns state transitions; these functions
own the per-stage execution detail.
"""

from __future__ import annotations

import logging
import re
from collections import Counter

from agents import Runner
from agents.exceptions import MaxTurnsExceeded
from agents.items import ToolCallItem
from app.agents.coding_agent import AgentFinalOutput, make_coding_agent
from app.agents.planner import make_planner_agent
from app.config import settings
from app.execution.test_executor import ExecutionUnavailableError, TestExecutor
from app.orchestration.analyst import RepositoryAnalyst, RepositoryAnalystError
from app.orchestration.models import CodingResult, RepairRequest
from app.planning.validator import PlanValidationError, validate_plan
from app.repository.context import format_context_for_prompt
from app.repository.models import RepositoryContext
from app.schemas.plan import ImplementationPlan
from app.schemas.task import TestResult
from app.workspace.manager import WorkspaceManager

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


# Tool names listed in a turn-limit message (the rest are summarised as "…").
MAX_TURN_LIMIT_TOOLS_LISTED = 8


class CodingTurnLimitError(CodingStageError):
    """Raised when the Coding Agent exhausts its turn budget.

    The message names the limit, the setting that controls it and the tools
    the agent called.  Tool arguments are never included: they can contain
    file contents or secrets.
    """

    def __init__(
        self,
        max_turns: int,
        phase: str,
        tool_call_counts: dict[str, int],
        changed_file_count: int,
    ) -> None:
        self.max_turns = max_turns
        self.phase = phase
        self.tool_call_counts = tool_call_counts
        self.changed_file_count = changed_file_count

        ranked = sorted(tool_call_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        listed = ", ".join(f"{name}×{count}" for name, count in ranked[:MAX_TURN_LIMIT_TOOLS_LISTED])
        if len(ranked) > MAX_TURN_LIMIT_TOOLS_LISTED:
            listed += ", …"
        total_calls = sum(tool_call_counts.values())
        tools_part = f" after {total_calls} tool call(s) ({listed})" if total_calls else ""
        message = (
            f"Coding agent reached its turn limit ({max_turns} turns, CODEFORGE_CODING_MAX_TURNS) "
            f"during {phase}{tools_part}. "
            f"{changed_file_count} file(s) were changed but not verified, and nothing was committed. "
            "Raise CODEFORGE_CODING_MAX_TURNS or narrow the task."
        )
        # Bounded by construction: at most 8 listed names of at most 40 characters.
        super().__init__(message)


def _tool_call_counts(exc: MaxTurnsExceeded) -> dict[str, int]:
    """Count tool calls by name from the SDK's partial run data (names only)."""
    run_data = getattr(exc, "run_data", None)
    if run_data is None:
        return {}
    counts: Counter[str] = Counter()
    for item in run_data.new_items:
        if not isinstance(item, ToolCallItem):
            continue
        raw = item.raw_item
        name = raw.get("name") if isinstance(raw, dict) else getattr(raw, "name", None)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "", str(name or "unknown"))[:40] or "unknown"
        counts[safe_name] += 1
    return dict(counts)


# Repair feedback limits.  Test output keeps its tail (pytest prints the
# failure summary last); the diff keeps its head.
REPAIR_FEEDBACK_MAX_BYTES = 16 * 1024
REPAIR_DIFF_MAX_BYTES = 32 * 1024


def _tail_bytes(text: str, limit: int) -> str:
    """Keep the last *limit* bytes of *text*, marking any truncation."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return "... (earlier output truncated)\n" + data[-limit:].decode("utf-8", errors="ignore")


def _head_bytes(text: str, limit: int) -> str:
    """Keep the first *limit* bytes of *text*, marking any truncation."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore") + "\n... (diff truncated)"


def build_repair_prompt(repair: RepairRequest) -> str:
    """Return the repair section appended to the coding prompt."""
    test_result = repair.failed_test_result
    output = test_result.stdout
    if test_result.stderr:
        output = f"{output}\n[stderr]\n{test_result.stderr}"
    return (
        f"REPAIR ATTEMPT {repair.attempt} of {repair.max_attempts}\n"
        "Your earlier changes are applied in the workspace, but the test suite failed "
        f"(exit_code={test_result.exit_code}). Fix the failures shown below.\n"
        "Rules:\n"
        "- Work from the workspace as it is now and keep the changes the original task needs.\n"
        "- Do not delete, skip or weaken tests, or change their expectations, unless the task explicitly requires it.\n"
        "- If the failures are unrelated to the task or cannot be fixed, say so in your final message instead of guessing.\n\n"
        "Current changes (unified diff against the original files):\n"
        f"{_head_bytes(repair.current_diff, REPAIR_DIFF_MAX_BYTES) or '(no changes)'}\n\n"
        "Failing test output:\n"
        f"{_tail_bytes(output, REPAIR_FEEDBACK_MAX_BYTES)}"
    )


async def run_coding_stage(
    task_description: str,
    repository_context: RepositoryContext,
    implementation_plan: ImplementationPlan,
    workspace: WorkspaceManager,
    executor: TestExecutor,
    model: str,
    max_turns: int | None = None,
    repair: RepairRequest | None = None,
) -> CodingResult:
    """Run the Coding Agent and return a typed :class:`CodingResult`.

    The agent will NOT make any changes if it cannot determine a safe edit.
    A :class:`CodingResult` with ``changes_made=False`` is a valid — but
    incomplete — outcome; the Orchestrator treats it as FAILED.

    Args:
        max_turns: Turn budget for this run.  Defaults to
            ``settings.codeforge_coding_max_turns``.
        repair: When set, this run is a repair attempt and the prompt
            includes the failing test output and the current diff.

    Raises:
        :class:`CodingTurnLimitError` when the turn budget is exhausted.
        :class:`CodingStageError` on any other agent execution failure.
    """
    turn_limit = max_turns if max_turns is not None else settings.codeforge_coding_max_turns
    phase = f"repair attempt {repair.attempt}/{repair.max_attempts}" if repair else "initial coding"
    agent = make_coding_agent(workspace=workspace, executor=executor, model=model)
    context_str = format_context_for_prompt(repository_context)
    plan_str = implementation_plan.model_dump_json(indent=2)
    prompt = (
        f"Repository Context:\n{context_str}\n\n"
        f"Implementation Plan:\n{plan_str}\n\n"
        f"Original Task description:\n{task_description}"
    )
    if repair is not None:
        prompt = f"{prompt}\n\n{build_repair_prompt(repair)}"

    logger.info("CodingStage: invoking coding agent model=%s max_turns=%d phase=%s", model, turn_limit, phase)
    try:
        result = await Runner.run(agent, prompt, max_turns=turn_limit)
    except MaxTurnsExceeded as exc:
        logger.warning("CodingStage: turn limit %d reached during %s", turn_limit, phase)
        raise CodingTurnLimitError(
            max_turns=turn_limit,
            phase=phase,
            tool_call_counts=_tool_call_counts(exc),
            changed_file_count=len(workspace.get_modified_paths()),
        ) from exc
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
    """Raised when the test executor itself cannot execute (not a test failure)."""


async def run_testing_stage(
    workspace: WorkspaceManager, executor: TestExecutor
) -> TestResult:
    """Run the test suite in *workspace* and return a :class:`TestResult`.

    Note: A ``TestResult`` with ``passed=False`` is *not* a
    :class:`TestingStageError` — it means tests ran but failed.  The
    Orchestrator transitions to FAILED in that case; this function only raises
    on executor-infrastructure errors.

    Raises:
        :class:`TestingStageError` if the executor cannot execute at all,
        including when test execution is disabled or unavailable.
    """
    logger.info(
        "TestingStage: running test suite in %s (isolation=%s)",
        workspace.root,
        executor.isolation.value,
    )
    try:
        raw_result = await executor.run(workspace.root)
        test_result = TestResult(
            passed=raw_result.passed,
            exit_code=raw_result.exit_code,
            duration_seconds=raw_result.duration_seconds,
            stdout=_bound_text(raw_result.stdout),
            stderr=_bound_text(raw_result.stderr),
        )
    except ExecutionUnavailableError as exc:
        raise TestingStageError(f"Test execution unavailable: {_safe_msg(exc)}") from exc
    except Exception as exc:
        raise TestingStageError(
            f"Test executor infrastructure error: {_safe_msg(exc)}"
        ) from exc

    logger.info(
        "TestingStage: finished passed=%s exit_code=%d duration=%.2fs",
        test_result.passed,
        test_result.exit_code,
        test_result.duration_seconds,
    )
    return test_result
