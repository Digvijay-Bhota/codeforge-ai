"""Phase 5: Orchestrator — controls the multi-agent workflow.

Execution is explicitly ordered and synchronous (within a single async call):

    PENDING
      → ANALYZING  (RepositoryAnalyst)
      → PLANNING   (PlannerAgent)
      → CODING     (CodingAgent)
      → TESTING    (TestRunner)
      ↺ CODING     (bounded repair attempts after failed tests, default 0)
      → COMPLETED  (or FAILED at any stage)

Design principles:
  - The Orchestrator owns state transitions.  Agents do not.
  - Each stage has explicit failure handling.
  - No automatic retries, except at most CODEFORGE_MAX_REPAIR_ATTEMPTS
    repair attempts after failed tests (hard limit MAX_REPAIR_ATTEMPTS_LIMIT).
  - No background workers, queues, or Redis in Phase 5.
  - Errors are bounded and sanitised before surfacing to callers.
  - Stack traces are never returned to API consumers.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC
from typing import Any

from app.config import settings
from app.execution.ownership import verify_async_ownership
from app.orchestration.models import (
    CodingResult,
    FinalTaskResult,
    RepairRequest,
    WorkflowState,
    WorkflowStatus,
)
from app.orchestration.stages import (
    AnalysisStageError,
    CodingStageError,
    CodingTurnLimitError,
    PlanningStageError,
    TestingStageError,
    _bound_text,
    run_analysis_stage,
    run_coding_stage,
    run_planning_stage,
    run_testing_stage,
)
from app.schemas.task import TestResult
from app.workspace.manager import WorkspaceManager
from app.workspace.runner import TestRunner

logger = logging.getLogger(__name__)

MAX_FAILURE_REASON_LEN = 800  # characters — keep failure messages bounded
MAX_REPAIR_ATTEMPTS_LIMIT = 5  # hard cap, whatever the configuration says
MAX_TEST_SUMMARY_LEN = 200  # characters of the last test-output line in failure reasons


class Orchestrator:
    """Controls the Phase 5 multi-agent workflow.

    Usage::

        orchestrator = Orchestrator()
        result = await orchestrator.run(workspace, task_description)

    The orchestrator creates and manages its own :class:`TestRunner`.
    External callers should not inject the runner unless testing.
    """

    def __init__(
        self,
        runner: TestRunner | None = None,
        session: Any = None,
        *,
        max_turns: int | None = None,
        max_repair_attempts: int | None = None,
    ) -> None:
        if max_turns is not None and not 1 <= max_turns <= 100:
            raise ValueError("max_turns must be between 1 and 100")
        if max_repair_attempts is not None and not 0 <= max_repair_attempts <= MAX_REPAIR_ATTEMPTS_LIMIT:
            raise ValueError(f"max_repair_attempts must be between 0 and {MAX_REPAIR_ATTEMPTS_LIMIT}")
        self._runner = runner or TestRunner()
        self.session = session
        # None means "read the corresponding setting at run time".
        self._max_turns = max_turns
        self._max_repair_attempts = max_repair_attempts

    async def run(
        self,
        workspace: WorkspaceManager,
        task_description: str,
        model: str | None = None,
        initial_plan: Any | None = None,
        stop_after_plan: bool = False,
    ) -> FinalTaskResult:
        model = model or settings.codeforge_model
        task_id = str(uuid.uuid4())
        state = WorkflowState(
            task_id=task_id,
            task_description=task_description,
        )
        logger.info(
            "Orchestrator: task=%s started workspace=%s model=%s desc=%.80s",
            task_id,
            workspace.root,
            model,
            task_description,
        )

        from datetime import datetime

        from app.observability.events import EventType
        from app.observability.metrics import inc_counter, record_histogram
        from app.observability.tracing import record_event

        async def run_instrumented_stage(stage_name, coro_or_func, *args, **kwargs):
            stage_start = datetime.now(UTC)
            if self.session:
                await record_event(self.session, EventType.STAGE_STARTED, stage=stage_name)
            try:
                import inspect
                res = coro_or_func(*args, **kwargs)
                if inspect.isawaitable(res):
                    res = await res
                duration_ms = int((datetime.now(UTC) - stage_start).total_seconds() * 1000)
                if self.session:
                    await record_event(self.session, EventType.STAGE_COMPLETED, stage=stage_name, status="success", duration_ms=duration_ms)
                record_histogram("stage_duration_ms", float(duration_ms), labels={"stage": stage_name})
                return res
            except Exception as e:
                duration_ms = int((datetime.now(UTC) - stage_start).total_seconds() * 1000)
                if self.session:
                    await record_event(self.session, EventType.STAGE_FAILED, stage=stage_name, status="failed", duration_ms=duration_ms, error_code=e.__class__.__name__)
                inc_counter("codeforge_stage_failures_total", labels={"stage": stage_name})
                raise

        # ── Stage 1: Repository Analysis ────────────────────────────────────
        state.transition(WorkflowStatus.ANALYZING)
        try:
            state.repository_context = await run_instrumented_stage("analyzing", run_analysis_stage, workspace, task_description)
        except AnalysisStageError as exc:
            return self._fail(state, f"Repository analysis failed: {exc}")

        # ── Stage 2: Planning ────────────────────────────────────────────────
        if initial_plan:
            from app.schemas.plan import ImplementationPlan
            if isinstance(initial_plan, dict):
                state.implementation_plan = ImplementationPlan.model_validate(initial_plan)
            else:
                state.implementation_plan = initial_plan
            state.transition(WorkflowStatus.PLANNING)
        else:
            state.transition(WorkflowStatus.PLANNING)
            try:
                state.implementation_plan = await run_instrumented_stage("planning", run_planning_stage, task_description, state.repository_context, model)
            except PlanningStageError as exc:
                return self._fail(state, f"Planning failed: {exc}")

        if stop_after_plan:
            state.transition(WorkflowStatus.COMPLETED)
            plan_goal = state.implementation_plan.goal if state.implementation_plan else ""
            return FinalTaskResult(
                task_id=task_id,
                workflow_status=WorkflowStatus.COMPLETED,
                task_description=task_description,
                implementation_plan=state.implementation_plan,
                plan_summary=plan_goal,
                changed_files=[],
                diff="",
                test_result=None,
                final_message=f"Plan generated: {plan_goal}",
                failure_reason=None,
            )

        # ── Stage 3: Coding ──────────────────────────────────────────────────
        state.transition(WorkflowStatus.CODING)
        try:
            coding_result = await run_instrumented_stage("coding", run_coding_stage,
                task_description=task_description,
                repository_context=state.repository_context,
                implementation_plan=state.implementation_plan,
                workspace=workspace,
                runner=self._runner,
                model=model,
                max_turns=self._max_turns,
            )
            state.coding_result = coding_result
        except CodingTurnLimitError as exc:
            return self._fail(state, str(exc))
        except CodingStageError as exc:
            return self._fail(state, f"Coding agent failed: {exc}")

        if not coding_result.changes_made:
            return self._fail(state, "Coding agent completed without making any changes to the workspace.")

        # ── Stage 4: Testing, with bounded repair attempts ───────────────────
        configured = (
            self._max_repair_attempts
            if self._max_repair_attempts is not None
            else settings.codeforge_max_repair_attempts
        )
        max_repairs = min(max(configured, 0), MAX_REPAIR_ATTEMPTS_LIMIT)

        for attempt in range(max_repairs + 1):
            state.transition(WorkflowStatus.TESTING)
            try:
                test_result = await run_instrumented_stage("testing", run_testing_stage, workspace, self._runner)
                state.test_result = test_result
            except TestingStageError as exc:
                return self._fail(state, f"Test runner infrastructure error: {exc}")

            if test_result.passed:
                break

            if attempt == max_repairs:
                return self._fail(state, self._tests_failed_reason(test_result, max_repairs))

            # ── Repair attempt ───────────────────────────────────────────────
            repair_number = attempt + 1
            # Stop before spending another agent run if the job lease is gone.
            # OwnershipLostError propagates to the worker unchanged.
            await verify_async_ownership()
            state.transition(WorkflowStatus.CODING)
            diff_before = workspace.generate_diff()
            repair = RepairRequest(
                attempt=repair_number,
                max_attempts=max_repairs,
                failed_test_result=test_result,
                current_diff=diff_before,
            )
            state.repair_attempts_used = repair_number
            logger.info(
                "Orchestrator: task=%s repair attempt %d/%d after exit_code=%d",
                task_id,
                repair_number,
                max_repairs,
                test_result.exit_code,
            )
            try:
                state.coding_result = await run_instrumented_stage("repair", run_coding_stage,
                    task_description=task_description,
                    repository_context=state.repository_context,
                    implementation_plan=state.implementation_plan,
                    workspace=workspace,
                    runner=self._runner,
                    model=model,
                    max_turns=self._max_turns,
                    repair=repair,
                )
            except CodingStageError as exc:
                self._refresh_changes(state, workspace)
                prefix = "" if isinstance(exc, CodingTurnLimitError) else f"Repair attempt {repair_number}/{max_repairs} failed: "
                return self._fail(
                    state,
                    f"{prefix}{exc} Tests had failed before this attempt "
                    f"(exit_code={test_result.exit_code}); its edits were not re-tested.",
                )

            if workspace.generate_diff() == diff_before:
                return self._fail(
                    state,
                    f"Tests failed (exit_code={test_result.exit_code}); "
                    f"repair attempt {repair_number}/{max_repairs} made no changes.",
                )

        # ── Completed ────────────────────────────────────────────────────────
        state.transition(WorkflowStatus.COMPLETED)
        logger.info("Orchestrator: task=%s COMPLETED", task_id)

        final_coding = state.coding_result or coding_result
        final_message = final_coding.message
        if state.repair_attempts_used:
            final_message = f"Tests passed after {state.repair_attempts_used} repair attempt(s). {final_message}"

        return FinalTaskResult(
            task_id=task_id,
            workflow_status=WorkflowStatus.COMPLETED,
            task_description=task_description,
            implementation_plan=state.implementation_plan,
            plan_summary=state.implementation_plan.goal if state.implementation_plan else "",
            changed_files=final_coding.changed_paths,
            diff=final_coding.diff,
            test_result=state.test_result,
            final_message=final_message,
            failure_reason=None,
            repair_attempts_used=state.repair_attempts_used,
        )

    # ── Private helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _tests_failed_reason(test_result: TestResult, max_repairs: int) -> str:
        """Failure reason for a final failed test run (keeps the "Tests failed" prefix)."""
        if max_repairs == 0:
            return f"Tests failed (exit_code={test_result.exit_code})."
        lines = [line.strip() for line in (test_result.stdout or "").splitlines() if line.strip()]
        summary = f" Last test output line: {lines[-1][:MAX_TEST_SUMMARY_LEN]}" if lines else ""
        return (
            f"Tests failed (exit_code={test_result.exit_code}) after {max_repairs} repair attempt(s)."
            f"{summary}"
        )

    @staticmethod
    def _refresh_changes(state: WorkflowState, workspace: WorkspaceManager) -> None:
        """Make the reported changes match the workspace after an interrupted repair."""
        if state.coding_result is None:
            return
        state.coding_result = state.coding_result.model_copy(
            update={
                "changes_made": bool(workspace.get_modified_paths()),
                "changed_paths": workspace.get_modified_paths(),
                "diff": _bound_text(workspace.generate_diff()),
            }
        )

    def _fail(self, state: WorkflowState, reason: str) -> FinalTaskResult:
        """Transition to FAILED and return a bounded :class:`FinalTaskResult`."""
        bounded_reason = reason[:MAX_FAILURE_REASON_LEN]
        logger.error(
            "Orchestrator: task=%s FAILED at %s — %s",
            state.task_id,
            state.status.value,
            bounded_reason,
        )
        # Transition to FAILED regardless of current status.
        # (We bypass the state machine helper here because _fail can be called
        # from any stage, so the intermediate status may vary.)
        state.status = WorkflowStatus.FAILED
        state.failure_reason = bounded_reason

        coding_result: CodingResult | None = state.coding_result
        plan_summary = (
            state.implementation_plan.goal if state.implementation_plan else ""
        )

        if coding_result and coding_result.message:
            final_message = coding_result.message
        else:
            final_message = f"Task failed: {bounded_reason}"

        return FinalTaskResult(
            task_id=state.task_id,
            workflow_status=WorkflowStatus.FAILED,
            task_description=state.task_description,
            implementation_plan=state.implementation_plan,
            plan_summary=plan_summary,
            changed_files=coding_result.changed_paths if coding_result else [],
            diff=coding_result.diff if coding_result else "",
            test_result=state.test_result,
            final_message=final_message,
            failure_reason=bounded_reason,
            repair_attempts_used=state.repair_attempts_used,
        )
