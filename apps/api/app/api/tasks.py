from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import ApprovalStatusEnum, Task, TaskStatusEnum, User
from app.db.repositories.task_repository import TaskRepository
from app.db.repositories.user_repository import UserRepository
from app.db.session import get_db_session
from app.schemas.task import (
    ApprovalDetail,
    ApproveTaskRequest,
    DiffResponse,
    ExecutionTarget,
    RejectTaskRequest,
    RetryTaskRequest,
    TaskDetailResponse,
    TaskListResponse,
    TaskRequest,
    TaskSummary,
)
from app.services.authorization_service import AuthorizationService
from app.services.task_service import TaskService
from app.services.task_state_machine import TaskStateMachine

"""Task management and human-in-the-loop approval endpoints.

Phase 10B.1 Security Enforcement:
- Endpoints require an authenticated session token (via get_current_user).
- Access to repository tasks is gated by repository collaboration permissions (read/write/admin).
- Task approvals and retries strictly use the authenticated GitHub identity.
"""

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tasks"])


async def _verify_task_read_access(
    task: Task, user: User, auth_service: AuthorizationService
) -> None:
    """Ensure user has read permission on the task's repository or is creator of local task."""
    if task.repository:
        can_read = await auth_service.can_read_repository(user, task.repository)
        if not can_read:
            # 404 to avoid leaking existence of private repository tasks
            raise HTTPException(status_code=404, detail="Task not found")
    else:
        if task.creator_id and task.creator_id != user.id:
            raise HTTPException(status_code=404, detail="Task not found")


async def _verify_task_write_access(
    task: Task, user: User, auth_service: AuthorizationService
) -> None:
    """Ensure user has write/admin permission on the task's repository or is creator of local task."""
    if task.repository:
        can_write = await auth_service.can_write_repository(user, task.repository)
        if not can_write:
            raise HTTPException(
                status_code=403,
                detail=f"Write permission required for repository {task.repository}",
            )
    else:
        if task.creator_id and task.creator_id != user.id:
            raise HTTPException(
                status_code=403, detail="Only the task creator can modify this task"
            )


@router.get(
    "/tasks", response_model=TaskListResponse, summary="List tasks with filtering and pagination"
)
async def list_tasks(
    repository: str | None = Query(None, description="Filter by repository name (owner/repo)"),
    status: str | None = Query(None, description="Filter by task status"),
    limit: int = Query(20, ge=1, le=100, description="Page size (max 100)"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> TaskListResponse:
    auth_service = AuthorizationService(session)

    if repository:
        can_read = await auth_service.can_read_repository(current_user, repository)
        if not can_read:
            raise HTTPException(status_code=403, detail=f"Access denied to repository {repository}")
        service = TaskService(session)
        tasks, total = await service.list_tasks(
            repository=repository, status=status, limit=limit, offset=offset
        )
    else:
        service = TaskService(session)
        tasks, total = await service.list_tasks(
            status=status, creator_id=current_user.id, limit=limit, offset=offset
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
    request: TaskRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Submit a coding task. Returns immediately; executes asynchronously."""
    logger.info(
        "POST /tasks | execution_target=%s | user=%s",
        request.execution_target,
        current_user.id,
    )

    if request.execution_target == ExecutionTarget.github and request.github_repository:
        auth_service = AuthorizationService(session)
        can_write = await auth_service.can_write_repository(current_user, request.github_repository)
        if not can_write:
            raise HTTPException(
                status_code=403,
                detail=f"Write permission required for repository {request.github_repository}",
            )

    service = TaskService(session)
    task = await service.create_task(request, creator_id=current_user.id)
    await session.commit()
    return {"task_id": task.task_id, "status": task.status}


@router.get("/tasks/{task_id}", response_model=TaskDetailResponse, summary="Get task details")
async def get_task(
    task_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> TaskDetailResponse:
    repo = TaskRepository(session)
    task = await repo.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    auth_service = AuthorizationService(session)
    await _verify_task_read_access(task, current_user, auth_service)

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
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    logger.info("POST /tasks/%s/approve by user %s", task_id, current_user.id)
    repo = TaskRepository(session)
    task = await repo.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    auth_service = AuthorizationService(session)
    await _verify_task_write_access(task, current_user, auth_service)

    user_repo = UserRepository(session)
    identity = await user_repo.get_github_identity(current_user.id)
    approver_name = identity.github_login if identity else current_user.display_name

    req = payload or ApproveTaskRequest()
    service = TaskService(session)
    task = await service.approve_task(task_id, approver=approver_name, comment=req.comment)
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
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    logger.info("POST /tasks/%s/reject by user %s", task_id, current_user.id)
    repo = TaskRepository(session)
    task = await repo.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    auth_service = AuthorizationService(session)
    await _verify_task_write_access(task, current_user, auth_service)

    user_repo = UserRepository(session)
    identity = await user_repo.get_github_identity(current_user.id)
    rejecter_name = identity.github_login if identity else current_user.display_name

    req = payload or RejectTaskRequest()
    service = TaskService(session)
    task = await service.reject_task(task_id, rejecter=rejecter_name, reason=req.reason)
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
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    logger.info("POST /tasks/%s/retry by user %s", task_id, current_user.id)
    repo = TaskRepository(session)
    task = await repo.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    auth_service = AuthorizationService(session)
    await _verify_task_write_access(task, current_user, auth_service)

    req = payload or RetryTaskRequest()
    service = TaskService(session)
    child_task = await service.retry_task(
        task_id,
        additional_instructions=req.additional_instructions,
        workspace_path_override=req.workspace_path,
        creator_id=current_user.id,
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
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> DiffResponse:
    logger.info("GET /tasks/%s/diff by user %s", task_id, current_user.id)
    repo = TaskRepository(session)
    task = await repo.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    auth_service = AuthorizationService(session)
    await _verify_task_read_access(task, current_user, auth_service)

    service = TaskService(session)
    return await service.get_task_diff(task_id)


@router.get("/tasks/{task_id}/events", summary="Get task events")
async def get_task_events(
    task_id: str,
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    repo = TaskRepository(session)
    task = await repo.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    auth_service = AuthorizationService(session)
    await _verify_task_read_access(task, current_user, auth_service)

    if limit > 100:
        limit = 100

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
