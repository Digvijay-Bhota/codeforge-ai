from __future__ import annotations

import logging
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Job, JobStatusEnum, OutboxEvent, Task, TaskStatusEnum
from app.db.repositories.job_repository import JobRepository
from app.db.repositories.outbox_repository import OutboxRepository
from app.db.repositories.task_repository import TaskRepository
from app.orchestration.models import FinalTaskResult, WorkflowStatus
from app.orchestration.orchestrator import Orchestrator
from app.schemas.task import ChangedFile, PlanStep, TaskRequest, TaskResult, TaskStatus
from app.services.queue_service import QueueService
from app.services.task_state_machine import TaskStateMachine
from app.workspace.manager import WorkspaceError, WorkspaceManager

logger = logging.getLogger(__name__)

class TaskService:
    def __init__(self, session: AsyncSession | None = None):
        self.session = session
        self._orchestrator = Orchestrator(session=self.session)

        if session:
            self.task_repo = TaskRepository(session)
            self.job_repo = JobRepository(session)
            self.outbox_repo = OutboxRepository(session)
            self.state_machine = TaskStateMachine(self.task_repo)
            self.queue = QueueService()

    async def create_task(self, request: TaskRequest) -> Task:
        """Create a Task, Job, and OutboxEvent atomically."""
        if not self.session:
            raise RuntimeError("TaskService requires a DB session to create tasks")

        task_id = str(uuid.uuid4())

        # 1. Create Task
        task = Task(
            task_id=task_id,
            status=TaskStatusEnum.PENDING.value,
            execution_target=request.execution_target.value,
            repository=request.github_repository,
            workspace_path=request.workspace_path,
            requested_task=request.description,
        )
        await self.task_repo.create_task(task)

        # 2. Create Job
        job = Job(
            task_id=task_id,
            status=JobStatusEnum.PENDING.value,
        )
        await self.job_repo.create_job(job)

        # 3. Create Outbox Event
        payload = {"job_id": job.id}
        outbox_event = OutboxEvent(
            event_type="JOB_CREATED",
            aggregate_id=str(job.id),
            payload=payload
        )
        await self.outbox_repo.create_event(outbox_event)

        # 4. Observability and Audit
        from app.observability.context import reset_observability_context, set_observability_context
        from app.observability.events import AuditEventType, EventType
        from app.observability.tracing import record_audit_event, record_event

        obs_tokens = set_observability_context(task_id=task_id, job_id=job.id, execution_id=job.execution_id, db_session=self.session)
        try:
            await record_event(self.session, EventType.TASK_CREATED, component="api")
            await record_audit_event(
                self.session, AuditEventType.TASK_CREATED, actor_type="SYSTEM",
                resource_type="task", resource_id=task_id,
                metadata={"execution_target": request.execution_target.value}
            )
        finally:
            reset_observability_context(obs_tokens)

        return task

    async def execute_task(self, request: TaskRequest, task_id: str) -> TaskResult:
        """Execute *request* through the Orchestrator and map to TaskResult."""
        logger.info("TaskService: executing task %s (target=%s)", task_id, request.execution_target)

        if request.execution_target.value == "github":
            from app.execution.github_execution import GitHubExecutionService
            github_service = GitHubExecutionService()
            return await github_service.execute(request, task_id)

        workspace_root = Path(request.workspace_path).resolve()
        try:
            enforced_root = Path(settings.workspace_root) if settings.workspace_root else None
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

        final: FinalTaskResult = await self._orchestrator.run(
            workspace=workspace,
            task_description=request.description,
            model=settings.codeforge_model,
        )
        return _map_to_task_result(final, task_id)

    # Note: run_task is kept purely for test backward compatibility.
    # In production, endpoints should call create_task -> outbox -> worker -> execute_task.
    async def run_task(self, request: TaskRequest) -> TaskResult:
        task_id = str(uuid.uuid4())
        return await self.execute_task(request, task_id)

def _map_to_task_result(final: FinalTaskResult, original_task_id: str = "") -> TaskResult:
    if final.workflow_status == WorkflowStatus.COMPLETED:
        status = TaskStatus.success
    elif final.workflow_status == WorkflowStatus.FAILED:
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

    changed_files = [ChangedFile(path=p, action="modified") for p in final.changed_files]

    plan_steps: list[PlanStep] = []
    if final.implementation_plan:
        plan_steps = [
            PlanStep(step=s.step_number, description=s.description)
            for s in final.implementation_plan.steps
        ]

    return TaskResult(
        task_id=final.task_id or original_task_id,
        status=status,
        description=final.task_description,
        plan=plan_steps,
        changed_files=changed_files,
        diff=final.diff,
        test_result=final.test_result,
        error_message=final.failure_reason,
        agent_output=final.final_message,
    )
