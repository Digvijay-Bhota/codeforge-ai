import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.task_repository import TaskRepository
from app.db.session import get_db_session
from app.schemas.task import TaskRequest
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tasks"])

@router.post("/tasks", summary="Submit a coding task")
async def create_task(request: TaskRequest, session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    """Submit a coding task. Returns immediately; executes asynchronously."""
    logger.info("POST /tasks | execution_target=%s", request.execution_target)
    service = TaskService(session)
    task = await service.create_task(request)
    await session.commit()
    return {"task_id": task.task_id, "status": task.status}

@router.get("/tasks/{task_id}", summary="Get task status")
async def get_task(task_id: str, session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    repo = TaskRepository(session)
    task = await repo.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    return {
        "task_id": task.task_id,
        "status": task.status,
        "execution_target": task.execution_target,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "failure_code": task.failure_code,
        "failure_reason": task.failure_reason,
        "final_message": task.final_message,
        "implementation_plan": task.implementation_plan,
        "task_result": task.task_result,
    }

@router.get("/tasks/{task_id}/events", summary="Get task events")
async def get_task_events(
    task_id: str,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_db_session)
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
