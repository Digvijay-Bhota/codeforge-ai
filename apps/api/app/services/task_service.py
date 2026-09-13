from __future__ import annotations

import logging
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import ApprovalStatusEnum, Job, JobStatusEnum, OutboxEvent, Task, TaskStatusEnum
from app.db.repositories.job_repository import JobRepository
from app.db.repositories.outbox_repository import OutboxRepository
from app.db.repositories.task_repository import TaskRepository
from app.orchestration.models import FinalTaskResult, WorkflowStatus
from app.orchestration.orchestrator import Orchestrator
from app.schemas.plan import ImplementationPlan
from app.schemas.task import (
    ChangedFile,
    DiffResponse,
    ExecutionTarget,
    PlanStep,
    TaskRequest,
    TaskResult,
    TaskStatus,
)
from app.services.diff_service import parse_unified_diff
from app.services.queue_service import QueueService
from app.services.task_state_machine import TaskStateMachine
from app.workspace.manager import IGNORED_DIRS, WorkspaceError, WorkspaceManager

logger = logging.getLogger(__name__)


def parse_plan_dict(plan_dict: dict[str, Any] | None) -> ImplementationPlan | None:
    """Safely reconstruct an ImplementationPlan from stored dict or steps list."""
    if not plan_dict:
        return None
    try:
        return ImplementationPlan.model_validate(plan_dict)
    except Exception as exc:
        logger.debug("ImplementationPlan direct validation fallback: %s", exc)

    steps = plan_dict.get("steps")
    if steps and isinstance(steps, list):
        try:
            from app.schemas.plan import PlanAction, RiskLevel
            from app.schemas.plan import PlanStep as SchemaPlanStep

            parsed_steps: list[SchemaPlanStep] = []
            for s in steps:
                if isinstance(s, dict):
                    action_str = s.get("action", "modify")
                    try:
                        action = PlanAction(action_str)
                    except Exception:
                        action = PlanAction.modify
                    parsed_steps.append(
                        SchemaPlanStep(
                            step_number=s.get("step_number", s.get("step", 1)),
                            action=action,
                            description=s.get("description", ""),
                            affected_paths=s.get("affected_paths", []),
                            rationale=s.get("rationale", s.get("description", "")),
                            dependencies=s.get("dependencies", []),
                        )
                    )
            if parsed_steps:
                return ImplementationPlan(
                    goal=plan_dict.get("goal", "Execute approved plan"),
                    assumptions=plan_dict.get("assumptions", []),
                    steps=parsed_steps,
                    affected_files=plan_dict.get("affected_files", []),
                    tests_needed=plan_dict.get("tests_needed", []),
                    validation_strategy=plan_dict.get("validation_strategy", "Run tests"),
                    risks=plan_dict.get("risks", []),
                    risk_level=RiskLevel.low,
                    summary=plan_dict.get("summary", "Approved plan"),
                )
        except Exception as exc:
            logger.warning("Could not reconstruct ImplementationPlan from dict: %s", exc)
    return None


def _copy_isolated_workspace(src: Path, dst: Path) -> None:
    """Safely copy workspace files from src to dst for retry isolation.

    Excludes:
    - Version control internals (.git)
    - Sensitive files / secrets (.env*, *.pem, *.key, id_rsa*, credentials*, secrets*, tokens*)
    - Build & cache artifacts (__pycache__, .pytest_cache, .mypy_cache, .ruff_cache, node_modules, dist, build, *.pyc)
    - Symlinks pointing outside src to prevent path escape vulnerabilities.
    """
    sensitive_prefixes = (".env", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")
    sensitive_suffixes = (".pem", ".key", ".pfx", ".p12", ".token", ".pyc", ".pyo", ".pyd")
    sensitive_names = {"credentials", "secrets", ".credentials", ".secrets"}

    for item in src.iterdir():
        name = item.name
        if not item.is_symlink() and item.is_dir():
            if (
                name in IGNORED_DIRS
                or name.lower() in sensitive_names
                or any(name.lower().startswith(p) for p in sensitive_prefixes)
            ):
                continue
            dst_child = dst / name
            dst_child.mkdir(parents=True, exist_ok=True)
            _copy_isolated_workspace(item, dst_child)
        elif item.is_symlink():
            try:
                target = item.resolve()
                target.relative_to(src.resolve())
                if target.is_file():
                    target_name_lower = target.name.lower()
                    if (
                        not any(target_name_lower.startswith(p) for p in sensitive_prefixes)
                        and not any(target_name_lower.endswith(s) for s in sensitive_suffixes)
                        and target_name_lower not in sensitive_names
                    ):
                        shutil.copy2(target, dst / name)
            except (ValueError, RuntimeError, OSError):
                continue
        elif item.is_file():
            name_lower = name.lower()
            if any(name_lower.startswith(p) for p in sensitive_prefixes):
                continue
            if any(name_lower.endswith(s) for s in sensitive_suffixes):
                continue
            if name_lower in sensitive_names:
                continue
            shutil.copy2(item, dst / name)


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

    async def create_task(self, request: TaskRequest, parent_task_id: str | None = None) -> Task:
        """Create a Task, Job, and OutboxEvent atomically."""
        if not self.session:
            raise RuntimeError("TaskService requires a DB session to create tasks")

        task_id = str(uuid.uuid4())

        approval_config = dict(request.approval_config or {})
        if request.require_plan_approval:
            approval_config["require_plan_approval"] = True

        # 1. Create Task
        task = Task(
            task_id=task_id,
            status=TaskStatusEnum.PENDING.value,
            execution_target=request.execution_target.value,
            repository=request.github_repository,
            workspace_path=request.workspace_path,
            requested_task=request.description,
            parent_task_id=parent_task_id,
            approval_config=approval_config if approval_config else None,
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
            payload=payload,
        )
        await self.outbox_repo.create_event(outbox_event)

        # 4. Observability and Audit
        from app.observability.context import reset_observability_context, set_observability_context
        from app.observability.events import AuditEventType, EventType
        from app.observability.tracing import record_audit_event, record_event

        obs_tokens = set_observability_context(
            task_id=task_id,
            job_id=job.id,
            execution_id=job.execution_id,
            db_session=self.session,
        )
        try:
            await record_event(self.session, EventType.TASK_CREATED, component="api")
            audit_metadata: dict[str, Any] = {"execution_target": request.execution_target.value}
            if parent_task_id:
                audit_metadata["parent_task_id"] = parent_task_id
            await record_audit_event(
                self.session,
                AuditEventType.TASK_CREATED,
                actor_type="SYSTEM",
                resource_type="task",
                resource_id=task_id,
                metadata=audit_metadata,
            )
        finally:
            reset_observability_context(obs_tokens)

        return task

    async def approve_task(
        self, task_id: str, approver: str = "human", comment: str | None = None
    ) -> Task:
        """Approve a task plan in WAITING_APPROVAL and enqueue a resumed execution job."""
        if not self.session:
            raise RuntimeError("TaskService requires a DB session to approve tasks")

        task = await self.task_repo.get_task_for_update(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        # Idempotency check: if already approved and task is queued/running/completed
        if task.status != TaskStatusEnum.WAITING_APPROVAL.value:
            approval = await self.task_repo.get_latest_approval(task_id)
            if approval and approval.status == ApprovalStatusEnum.APPROVED.value:
                return task
            raise HTTPException(
                status_code=409,
                detail=f"Task {task_id} is in status '{task.status}', cannot approve (must be WAITING_APPROVAL)",
            )

        approval = await self.task_repo.get_latest_approval(task_id)
        if not approval or approval.status != ApprovalStatusEnum.PENDING.value:
            raise HTTPException(
                status_code=409,
                detail=f"Task {task_id} does not have a pending plan approval",
            )

        approval_locked = await self.task_repo.get_approval_for_update(approval.id)
        if not approval_locked:
            raise HTTPException(status_code=409, detail="Failed to acquire approval lock")

        # Check expiration
        now_utc = datetime.now(UTC)
        if approval_locked.expires_at:
            exp = approval_locked.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=UTC)
            if exp < now_utc:
                approval_locked.status = ApprovalStatusEnum.EXPIRED.value
                approval_locked.responded_at = now_utc
                await self.state_machine.transition(task, TaskStatusEnum.CANCELLED)
                task.failure_reason = "Approval request expired"
                await self.task_repo.update_task(task)
                await self.task_repo.update_approval(approval_locked)

                from app.observability.context import (
                    reset_observability_context,
                    set_observability_context,
                )
                from app.observability.events import AuditEventType, EventType
                from app.observability.tracing import record_audit_event, record_event

                obs_tokens = set_observability_context(
                    task_id=task_id,
                    db_session=self.session,
                )
                try:
                    await record_event(
                        self.session,
                        EventType.APPROVAL_EXPIRED,
                        component="api",
                        metadata={"approval_id": approval_locked.id},
                    )
                    await record_audit_event(
                        self.session,
                        AuditEventType.APPROVAL_EXPIRED,
                        actor_type="SYSTEM",
                        resource_type="task",
                        resource_id=task_id,
                        metadata={
                            "approval_id": approval_locked.id,
                            "reason": "Approval request expired",
                        },
                    )
                finally:
                    reset_observability_context(obs_tokens)

                raise HTTPException(status_code=409, detail="Approval request has expired")

        # Record approval
        approval_locked.status = ApprovalStatusEnum.APPROVED.value
        approval_locked.approved_by = approver
        approval_locked.comment = comment
        approval_locked.responded_at = func.now()  # type: ignore
        await self.task_repo.update_approval(approval_locked)

        # Transition task WAITING_APPROVAL -> QUEUED
        task = await self.state_machine.transition(task, TaskStatusEnum.QUEUED)

        # Create new Job for Stage B execution
        job = Job(
            task_id=task.task_id,
            status=JobStatusEnum.PENDING.value,
        )
        await self.job_repo.create_job(job)

        # Outbox event
        outbox_event = OutboxEvent(
            event_type="JOB_CREATED",
            aggregate_id=str(job.id),
            payload={"job_id": job.id},
        )
        await self.outbox_repo.create_event(outbox_event)

        # Observability & Audit
        from app.observability.context import reset_observability_context, set_observability_context
        from app.observability.events import AuditEventType, EventType
        from app.observability.tracing import record_audit_event, record_event

        obs_tokens = set_observability_context(
            task_id=task_id,
            job_id=job.id,
            execution_id=job.execution_id,
            db_session=self.session,
        )
        try:
            await record_event(
                self.session,
                EventType.APPROVAL_GRANTED,
                component="api",
                metadata={"approver": approver},
            )
            await record_audit_event(
                self.session,
                AuditEventType.APPROVAL_GRANTED,
                actor_type="USER",
                resource_type="task",
                resource_id=task_id,
                metadata={"approver": approver, "comment": comment, "job_id": job.id},
            )
            await record_event(self.session, EventType.TASK_RESUMED, component="api")
            await record_audit_event(
                self.session,
                AuditEventType.TASK_RESUMED,
                actor_type="SYSTEM",
                resource_type="task",
                resource_id=task_id,
                metadata={"resumed_job_id": job.id},
            )
        finally:
            reset_observability_context(obs_tokens)

        return task

    async def reject_task(
        self, task_id: str, rejecter: str = "human", reason: str | None = None
    ) -> Task:
        """Reject a task plan in WAITING_APPROVAL and cancel the task."""
        if not self.session:
            raise RuntimeError("TaskService requires a DB session to reject tasks")

        task = await self.task_repo.get_task_for_update(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        # Idempotency check: if already cancelled and rejected
        if task.status != TaskStatusEnum.WAITING_APPROVAL.value:
            approval = await self.task_repo.get_latest_approval(task_id)
            if (
                approval
                and approval.status == ApprovalStatusEnum.REJECTED.value
                and task.status == TaskStatusEnum.CANCELLED.value
            ):
                return task
            raise HTTPException(
                status_code=409,
                detail=f"Task {task_id} is in status '{task.status}', cannot reject (must be WAITING_APPROVAL)",
            )

        approval = await self.task_repo.get_latest_approval(task_id)
        if approval:
            approval_locked = await self.task_repo.get_approval_for_update(approval.id)
            if approval_locked:
                approval_locked.status = ApprovalStatusEnum.REJECTED.value
                approval_locked.approved_by = rejecter
                approval_locked.comment = reason
                approval_locked.responded_at = func.now()  # type: ignore
                await self.task_repo.update_approval(approval_locked)

        task = await self.state_machine.transition(task, TaskStatusEnum.CANCELLED)
        task.failure_reason = reason or "Plan rejected by user"
        await self.task_repo.update_task(task)

        from app.observability.context import reset_observability_context, set_observability_context
        from app.observability.events import AuditEventType, EventType
        from app.observability.tracing import record_audit_event, record_event

        obs_tokens = set_observability_context(
            task_id=task_id,
            db_session=self.session,
        )
        try:
            await record_event(
                self.session,
                EventType.APPROVAL_REJECTED,
                component="api",
                metadata={"rejecter": rejecter},
            )
            await record_audit_event(
                self.session,
                AuditEventType.APPROVAL_REJECTED,
                actor_type="USER",
                resource_type="task",
                resource_id=task_id,
                metadata={"rejecter": rejecter, "reason": reason},
            )
        finally:
            reset_observability_context(obs_tokens)

        return task

    async def retry_task(
        self,
        task_id: str,
        additional_instructions: str | None = None,
        workspace_path_override: str | None = None,
    ) -> Task:
        """Retry a completed, failed, or cancelled task by creating a linked child task."""
        if not self.session:
            raise RuntimeError("TaskService requires a DB session to retry tasks")

        original_task = await self.task_repo.get_task(task_id)
        if not original_task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        terminal_statuses = {
            TaskStatusEnum.COMPLETED.value,
            TaskStatusEnum.FAILED.value,
            TaskStatusEnum.CANCELLED.value,
        }
        if original_task.status not in terminal_statuses:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot retry task {task_id} with active status '{original_task.status}'. "
                    "Task must be completed, failed, or cancelled."
                ),
            )

        new_description = original_task.requested_task
        if additional_instructions and additional_instructions.strip():
            new_description = (
                f"{new_description}\n\n[Retry Instructions]:\n{additional_instructions.strip()}"
            )

        require_approval = False
        if original_task.approval_config and isinstance(original_task.approval_config, dict):
            require_approval = original_task.approval_config.get("require_plan_approval", False)

        child_workspace_path = ""
        if original_task.execution_target == ExecutionTarget.local.value:
            enforced_root = (
                Path(settings.workspace_root).resolve() if settings.workspace_root else None
            )
            if workspace_path_override:
                override_path = Path(workspace_path_override).resolve()
                if not override_path.is_dir():
                    raise HTTPException(
                        status_code=400,
                        detail=f"Workspace path override '{workspace_path_override}' does not exist or is not a directory",
                    )
                if enforced_root is not None:
                    try:
                        override_path.relative_to(enforced_root)
                    except ValueError:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Workspace path override '{workspace_path_override}' is outside enforced workspace root '{enforced_root}'",
                        ) from None
                child_workspace_path = str(override_path)
            else:
                orig_workspace = original_task.workspace_path
                if not orig_workspace:
                    raise HTTPException(
                        status_code=400,
                        detail="Original task has no workspace path to retry",
                    )
                orig_path = Path(orig_workspace).resolve()
                if not orig_path.is_dir():
                    raise HTTPException(
                        status_code=400,
                        detail=f"Original workspace path '{orig_workspace}' no longer exists or is not a directory",
                    )
                temp_parent = (
                    str(enforced_root) if (enforced_root and enforced_root.is_dir()) else None
                )
                isolated_dir = Path(
                    tempfile.mkdtemp(prefix=f"retry_task_{task_id[:8]}_", dir=temp_parent)
                )
                _copy_isolated_workspace(orig_path, isolated_dir)
                child_workspace_path = str(isolated_dir)

        request = TaskRequest(
            execution_target=ExecutionTarget(original_task.execution_target),
            workspace_path=child_workspace_path,
            github_repository=original_task.repository,
            description=new_description,
            require_plan_approval=require_approval,
            approval_config=original_task.approval_config,
        )

        child_task = await self.create_task(request, parent_task_id=original_task.task_id)

        from app.observability.context import reset_observability_context, set_observability_context
        from app.observability.events import AuditEventType, EventType
        from app.observability.tracing import record_audit_event, record_event

        obs_tokens = set_observability_context(
            task_id=child_task.task_id,
            db_session=self.session,
        )
        try:
            await record_event(
                self.session,
                EventType.TASK_RETRIED,
                component="api",
                metadata={"parent_task_id": original_task.task_id},
            )
            await record_audit_event(
                self.session,
                AuditEventType.TASK_RETRIED,
                actor_type="USER",
                resource_type="task",
                resource_id=child_task.task_id,
                metadata={"parent_task_id": original_task.task_id},
            )
        finally:
            reset_observability_context(obs_tokens)

        return child_task

    async def list_tasks(
        self,
        repository: str | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Task], int]:
        """List tasks with optional repository and status filtering."""
        return await self.task_repo.list_tasks(
            repository=repository, status=status, limit=limit, offset=offset
        )

    async def get_task_diff(self, task_id: str) -> DiffResponse:
        """Retrieve bounded structured diff for a task."""
        task = await self.task_repo.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        raw_diff: str | None = None
        if task.task_result and isinstance(task.task_result, dict):
            raw_diff = task.task_result.get("diff")

        return parse_unified_diff(task_id, raw_diff)

    async def execute_task(
        self,
        request: TaskRequest,
        task_id: str,
        initial_plan: ImplementationPlan | None = None,
        stop_after_plan: bool = False,
    ) -> TaskResult:
        """Execute *request* through the Orchestrator and map to TaskResult."""
        logger.info(
            "TaskService: executing task %s (target=%s, stop_after_plan=%s, has_initial_plan=%s)",
            task_id,
            request.execution_target,
            stop_after_plan,
            initial_plan is not None,
        )

        if request.execution_target.value == "github":
            from app.execution.github_execution import GitHubExecutionService

            github_service = GitHubExecutionService()
            return await github_service.execute(
                request,
                task_id,
                initial_plan=initial_plan,
                stop_after_plan=stop_after_plan,
            )

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
            initial_plan=initial_plan,
            stop_after_plan=stop_after_plan,
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
    full_plan: dict[str, Any] | None = None
    if final.implementation_plan:
        plan_steps = [
            PlanStep(step=s.step_number, description=s.description)
            for s in final.implementation_plan.steps
        ]
        full_plan = final.implementation_plan.model_dump()

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
        full_plan=full_plan,
    )
