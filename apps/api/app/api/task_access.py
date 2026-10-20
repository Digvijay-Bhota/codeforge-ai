"""Task-level authorization shared by every endpoint that exposes a task.

Repository tasks follow the caller's GitHub permission on the repository;
local tasks are restricted to their creator.  A caller without read access
gets the same 404 as for a task that does not exist, so task existence is
not disclosed.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Task, User
from app.db.repositories.task_repository import TaskRepository
from app.services.authorization_service import AuthorizationService


async def verify_task_read_access(
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


async def verify_task_write_access(
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


async def get_readable_task(task_id: str, user: User, session: AsyncSession) -> Task:
    """Load *task_id* if *user* may read it; otherwise raise 404 (missing or not permitted)."""
    task = await TaskRepository(session).get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await verify_task_read_access(task, user, AuthorizationService(session))
    return task
