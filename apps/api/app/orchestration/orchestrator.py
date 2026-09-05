"""Phase 5: Orchestrator — controls the multi-agent workflow.

Execution is explicitly ordered and synchronous (within a single async call):

    PENDING
      → ANALYZING  (RepositoryAnalyst)
      → PLANNING   (PlannerAgent)
      → CODING     (CodingAgent)
      → TESTING    (TestRunner)
      → COMPLETED  (or FAILED at any stage)

Design principles:
  - The Orchestrator owns state transitions.  Agents do not.
  - Each stage has explicit failure handling.
  - No automatic retries.
  - No background workers, queues, or Redis in Phase 5.
  - Errors are bounded and sanitised before surfacing to callers.
  - Stack traces are never returned to API consumers.
"""

from __future__ import annotations

import logging
import uuid

from app.config import settings
from app.orchestration.models import (
    CodingResult,
    FinalTaskResult,
    WorkflowState,
    WorkflowStatus,
)
from app.orchestration.stages import (
    AnalysisStageError,
    CodingStageError,
    PlanningStageError,
    TestingStageError,
    run_analysis_stage,
    run_coding_stage,
    run_planning_stage,
    run_testing_stage,
)
from app.workspace.manager import WorkspaceManager
from app.workspace.runner import TestRunner

logger = logging.getLogger(__name__)

MAX_FAILURE_REASON_LEN = 800  # characters — keep failure messages bounded


class Orchestrator:
    """Controls the Phase 5 multi-agent workflow.

    Usage::

        orchestrator = Orchestrator()
        result = await orchestrator.run(workspace, task_description)

    The orchestrator creates and manages its own :class:`TestRunner`.
    External callers should not inject the runner unless testing.
    """

    def __init__(self, runner: TestRunner | None = None) -> None:
        self._runner = runner or TestRunner()

    async def run(
        self,
        workspace: WorkspaceManager,
        task_description: str,
        model: str | None = None,
    ) -> FinalTaskResult:
        """Execute the complete multi-agent workflow.

        Args:
            workspace: Sandboxed workspace.
            task_description: Natural-language task description.
            model: LLM model identifier.  Defaults to ``settings.codeforge_model``.

        Returns:
            A :class:`FinalTaskResult` regardless of success or failure.
            Failures are represented via ``workflow_status=FAILED`` and a
            bounded ``failure_reason``.
        """
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

        # ── Stage 1: Repository Analysis ────────────────────────────────────
        state.transition(WorkflowStatus.ANALYZING)
        try:
            state.repository_context = run_analysis_stage(workspace, task_description)
        except AnalysisStageError as exc:
            return self._fail(state, f"Repository analysis failed: {exc}")

        # ── Stage 2: Planning ────────────────────────────────────────────────
        state.transition(WorkflowStatus.PLANNING)
        try:
            state.implementation_plan = await run_planning_stage(
                task_description, state.repository_context, model
            )
        except PlanningStageError as exc:
            return self._fail(state, f"Planning failed: {exc}")

        # ── Stage 3: Coding ──────────────────────────────────────────────────
        state.transition(WorkflowStatus.CODING)
        try:
            coding_result = await run_coding_stage(
                task_description=task_description,
                repository_context=state.repository_context,
                implementation_plan=state.implementation_plan,
                workspace=workspace,
                runner=self._runner,
                model=model,
            )
            state.coding_result = coding_result
        except CodingStageError as exc:
            return self._fail(state, f"Coding agent failed: {exc}")

        # No-change is a failure — do not proceed to testing.
        if not coding_result.changes_made:
            return self._fail(
                state,
                "Coding agent completed without making any changes to the workspace.",
            )

        # ── Stage 4: Testing ─────────────────────────────────────────────────
        state.transition(WorkflowStatus.TESTING)
        try:
            test_result = run_testing_stage(workspace, self._runner)
            state.test_result = test_result
        except TestingStageError as exc:
            return self._fail(state, f"Test runner infrastructure error: {exc}")

        # Test failure → workflow FAILED (not COMPLETED).
        if not test_result.passed:
            return self._fail(
                state,
                f"Tests failed (exit_code={test_result.exit_code}).",
            )

        # ── Completed ────────────────────────────────────────────────────────
        state.transition(WorkflowStatus.COMPLETED)
        logger.info("Orchestrator: task=%s COMPLETED", task_id)

        return FinalTaskResult(
            task_id=task_id,
            workflow_status=WorkflowStatus.COMPLETED,
            task_description=task_description,
            implementation_plan=state.implementation_plan,
            plan_summary=state.implementation_plan.goal,
            changed_files=coding_result.changed_paths,
            diff=coding_result.diff,
            test_result=test_result,
            final_message=coding_result.message,
            failure_reason=None,
        )

    # ── Private helpers ──────────────────────────────────────────────────────

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
        )
