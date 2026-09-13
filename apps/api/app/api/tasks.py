import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApprovalStatusEnum, TaskStatusEnum
from app.db.repositories.task_repository import TaskRepository
from app.db.session import get_db_session
from app.schemas.task import (
    ApprovalDetail,
    ApproveTaskRequest,
    DiffResponse,
    RejectTaskRequest,
    RetryTaskRequest,
    TaskDetailResponse,
    TaskListResponse,
    TaskRequest,
    TaskSummary,
)
from app.services.task_service import TaskService
from app.services.task_state_machine import TaskStateMachine

"""Task management and human-in-the-loop approval endpoints.

SECURITY NOTICE (Phase 10A Boundary):
- These endpoints represent the internal Product API core for task control and plan review.
- In Phase 10A, caller identity is trusted/internal (e.g. internal network or reverse-proxy caller).
- Cryptographic authentication and RBAC are scheduled for Phase 10B/10C.
- Direct public internet exposure without an authenticating reverse proxy is unsafe.
"""

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tasks"])


@router.get(
    "/tasks", response_model=TaskListResponse, summary="List tasks with filtering and pagination"
)
async def list_tasks(
    repository: str | None = Query(None, description="Filter by repository name (owner/repo)"),
    status: str | None = Query(None, description="Filter by task status"),
    limit: int = Query(20, ge=1, le=100, description="Page size (max 100)"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    session: AsyncSession = Depends(get_db_session),
) -> TaskListResponse:
    service = TaskService(session)
    tasks, total = await service.list_tasks(
        repository=repository, status=status, limit=limit, offset=offset
    )

    summaries = [
        TaskSummary(
            task_id=t.task_id,
            status=t.status,
            execution_target=t.execution_target,
            repository=t.repository,
            requested_task=t.requested_task,
            created_at=t.created_at.isoformat() if t.created_at else None,
            started_at=t.started_at.isoformat() if t.started_at else None,
            completed_at=t.completed_at.isoformat() if t.completed_at else None,
            parent_task_id=t.parent_task_id,
            has_plan=bool(t.implementation_plan),
            has_diff=bool(
                t.task_result and isinstance(t.task_result, dict) and t.task_result.get("diff")
            ),
        )
        for t in tasks
    ]
    return TaskListResponse(total=total, limit=limit, offset=offset, tasks=summaries)


@router.post("/tasks", summary="Submit a coding task")
async def create_task(
    request: TaskRequest, session: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    """Submit a coding task. Returns immediately; executes asynchronously."""
    logger.info("POST /tasks | execution_target=%s", request.execution_target)
    service = TaskService(session)
    task = await service.create_task(request)
    await session.commit()
    return {"task_id": task.task_id, "status": task.status}


@router.get("/tasks/{task_id}", response_model=TaskDetailResponse, summary="Get task details")
async def get_task(
    task_id: str, session: AsyncSession = Depends(get_db_session)
) -> TaskDetailResponse:
    repo = TaskRepository(session)
    task = await repo.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    latest_app = await repo.get_latest_approval(task_id)
    now_utc = datetime.now(UTC)
    if (
        latest_app
        and latest_app.status == ApprovalStatusEnum.PENDING.value
        and latest_app.expires_at
    ):
        exp = latest_app.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        if exp < now_utc:
            latest_app.status = ApprovalStatusEnum.EXPIRED.value
            latest_app.responded_at = now_utc
            await repo.update_approval(latest_app)
            if task.status == TaskStatusEnum.WAITING_APPROVAL.value:
                state_machine = TaskStateMachine(repo)
                task = await state_machine.transition(task, TaskStatusEnum.CANCELLED)
                task.failure_reason = "Approval request expired"
                await repo.update_task(task)

            from app.observability.context import (
                reset_observability_context,
                set_observability_context,
            )
            from app.observability.events import AuditEventType, EventType
            from app.observability.tracing import record_audit_event, record_event

            obs_tokens = set_observability_context(
                task_id=task_id,
                db_session=session,
            )
            try:
                await record_event(
                    session,
                    EventType.APPROVAL_EXPIRED,
                    component="api",
                    metadata={"approval_id": latest_app.id},
                )
                await record_audit_event(
                    session,
                    AuditEventType.APPROVAL_EXPIRED,
                    actor_type="SYSTEM",
                    resource_type="task",
                    resource_id=task_id,
                    metadata={"approval_id": latest_app.id, "reason": "Approval request expired"},
                )
            finally:
                reset_observability_context(obs_tokens)

            await session.commit()
            await session.refresh(task)
            await session.refresh(latest_app)

    approval_detail = None
    if latest_app:
        approval_detail = ApprovalDetail(
            id=latest_app.id,
            approval_type=latest_app.approval_type,
            status=latest_app.status,
            requested_by=latest_app.requested_by,
            approved_by=latest_app.approved_by,
            comment=latest_app.comment,
            requested_at=latest_app.requested_at.isoformat(),
            responded_at=latest_app.responded_at.isoformat() if latest_app.responded_at else None,
            expires_at=latest_app.expires_at.isoformat() if latest_app.expires_at else None,
        )

    return TaskDetailResponse(
        task_id=task.task_id,
        status=task.status,
        execution_target=task.execution_target,
        repository=task.repository,
        workspace_path=task.workspace_path,
        requested_task=task.requested_task,
        created_at=task.created_at.isoformat() if task.created_at else None,
        started_at=task.started_at.isoformat() if task.started_at else None,
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
        parent_task_id=task.parent_task_id,
        approval_config=task.approval_config,
        pr_metadata=task.pr_metadata,
        failure_code=task.failure_code,
        failure_reason=task.failure_reason,
        final_message=task.final_message,
        implementation_plan=task.implementation_plan,
        task_result=task.task_result,
        latest_approval=approval_detail,
    )


@router.post("/tasks/{task_id}/approve", summary="Approve pending task plan")
async def approve_task(
    task_id: str,
    payload: ApproveTaskRequest | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    logger.info("POST /tasks/%s/approve", task_id)
    req = payload or ApproveTaskRequest()
    service = TaskService(session)
    task = await service.approve_task(task_id, comment=req.comment)
    await session.commit()
    return {
        "task_id": task.task_id,
        "status": task.status,
        "message": "Task plan approved and queued for execution",
    }


@router.post("/tasks/{task_id}/reject", summary="Reject pending task plan")
async def reject_task(
    task_id: str,
    payload: RejectTaskRequest | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    logger.info("POST /tasks/%s/reject", task_id)
    req = payload or RejectTaskRequest()
    service = TaskService(session)
    task = await service.reject_task(task_id, reason=req.reason)
    await session.commit()
    return {
        "task_id": task.task_id,
        "status": task.status,
        "message": "Task plan rejected and cancelled",
    }


@router.post("/tasks/{task_id}/retry", summary="Retry a completed or failed task")
async def retry_task(
    task_id: str,
    payload: RetryTaskRequest | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    logger.info("POST /tasks/%s/retry", task_id)
    req = payload or RetryTaskRequest()
    service = TaskService(session)
    child_task = await service.retry_task(
        task_id,
        additional_instructions=req.additional_instructions,
        workspace_path_override=req.workspace_path,
    )
    await session.commit()
    return {
        "task_id": child_task.task_id,
        "parent_task_id": task_id,
        "status": child_task.status,
    }


@router.get(
    "/tasks/{task_id}/diff", response_model=DiffResponse, summary="Get structured diff for a task"
)
async def get_task_diff(
    task_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> DiffResponse:
    logger.info("GET /tasks/%s/diff", task_id)
    service = TaskService(session)
    return await service.get_task_diff(task_id)


@router.get("/tasks/{task_id}/events", summary="Get task events")
async def get_task_events(
    task_id: str,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    if limit > 100:
        limit = 100

    repo = TaskRepository(session)
    task = await repo.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    events = await repo.get_task_events(task_id, limit=limit, offset=offset)
    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "from_status": e.from_status,
            "to_status": e.to_status,
            "message": e.message,
            "metadata": e.metadata_obj,
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]
