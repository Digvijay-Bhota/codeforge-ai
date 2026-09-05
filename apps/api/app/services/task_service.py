"""Task Service — delegates to the Phase 5 Orchestrator.

Phase 5 replaces the flat sequential logic in TaskService with a proper
multi-agent Orchestrator.  The public API surface (TaskService.run_task
accepting a TaskRequest and returning a TaskResult) is preserved unchanged
so that the existing POST /api/v1/tasks endpoint and its tests continue to
work without modification.

Execution flow (Phase 5):

    TaskService.run_task(request)
        │
        ▼
    Orchestrator.run(workspace, task_description)
        │
        ├── ANALYZING  → RepositoryAnalyst  (Phase 2 scanner)
        ├── PLANNING   → PlannerAgent       (Phase 3, no tools)
        ├── CODING     → CodingAgent        (Phase 1/4, WRITE MCP tools)
        ├── TESTING    → TestRunner         (Phase 1, controlled subprocess)
        └── COMPLETED / FAILED
        │
        ▼
    FinalTaskResult  →  mapped to legacy TaskResult for API compatibility
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from app.config import settings
from app.orchestration.models import FinalTaskResult, WorkflowStatus
from app.orchestration.orchestrator import Orchestrator
from app.schemas.task import (
    ChangedFile,
    PlanStep,
    TaskRequest,
    TaskResult,
    TaskStatus,
)
from app.workspace.manager import WorkspaceError, WorkspaceManager

logger = logging.getLogger(__name__)


class TaskService:
    """Runs coding tasks end-to-end via the Phase 5 Orchestrator."""

    def __init__(self) -> None:
        self._orchestrator = Orchestrator()

    async def run_task(self, request: TaskRequest) -> TaskResult:
        """Execute *request* through the Orchestrator and map to TaskResult.

        Returns a :class:`TaskResult` in all cases — failures are expressed
        via ``status: error`` or ``status: failure`` in the response body.
        Stack traces are never returned.
        """
        logger.info(
            "TaskService: received task target=%s desc=%.80s",
            request.execution_target,
            request.description,
        )

        task_id = str(uuid.uuid4())

        if request.execution_target.value == "github":
            from app.execution.github_execution import GitHubExecutionService
            github_service = GitHubExecutionService()
            return await github_service.execute(request, task_id)

        # ── Validate workspace ────────────────────────────────────────────────
        workspace_root = Path(request.workspace_path).resolve()
        try:
            enforced_root = (
                Path(settings.workspace_root) if settings.workspace_root else None
            )
            workspace = WorkspaceManager(workspace_root, enforced_root=enforced_root)
        except WorkspaceError as exc:
            logger.error("TaskService: workspace error — %s", exc)
            return TaskResult(
                task_id=task_id,
                status=TaskStatus.error,
                description=request.description,
                plan=[],
                changed_files=[],
                diff="",
                test_result=None,
                error_message=str(exc),
                agent_output="",
            )

        # ── Delegate to Orchestrator ─────────────────────────────────────────
        final: FinalTaskResult = await self._orchestrator.run(
            workspace=workspace,
            task_description=request.description,
            model=settings.codeforge_model,
        )

        return _map_to_task_result(final)


# ── Mapping helper ────────────────────────────────────────────────────────────


def _map_to_task_result(final: FinalTaskResult) -> TaskResult:
    """Convert a :class:`FinalTaskResult` to the legacy :class:`TaskResult`.

    This shim preserves the existing API response contract so callers and
    existing tests do not need to change.
    """
    if final.workflow_status == WorkflowStatus.COMPLETED:
        status = TaskStatus.success
    elif final.workflow_status == WorkflowStatus.FAILED:
        # Distinguish "failure" (tests failed, no changes made) from "error" (infra/planner).
        failure_reason = final.failure_reason or ""
        failure_lower = failure_reason.lower()
        is_soft_failure = (
            "tests failed" in failure_lower
            or "without making any changes" in failure_lower
            or "no changes" in failure_lower
        )
        status = TaskStatus.failure if is_soft_failure else TaskStatus.error
    else:
        status = TaskStatus.error

    changed_files = [
        ChangedFile(path=p, action="modified") for p in final.changed_files
    ]

    plan_steps: list[PlanStep] = []
    if final.implementation_plan:
        plan_steps = [
            PlanStep(step=s.step_number, description=s.description)
            for s in final.implementation_plan.steps
        ]

    return TaskResult(
        task_id=final.task_id,
        status=status,
        description=final.task_description,
        plan=plan_steps,
        changed_files=changed_files,
        diff=final.diff,
        test_result=final.test_result,
        error_message=final.failure_reason,
        agent_output=final.final_message,
    )
