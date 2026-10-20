"""Task-level authorization shared by every endpoint that exposes a task.

Repository tasks follow the caller's GitHub permission on the repository;
local tasks are restricted to their creator, whatever repository name is
stored on them, and a local task with no recorded creator belongs to nobody.
A caller without read access gets the same 404 as for a task that does not
exist, so task existence is not disclosed.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Task, User
from app.db.repositories.task_repository import TaskRepository
from app.schemas.task import ExecutionTarget
from app.services.authorization_service import AuthorizationService


def _governing_repository(task: Task) -> str | None:
    """The repository whose permissions govern access to *task*, or None (creator-only).

    Local tasks are always creator-only, even if a repository name is stored on them.
    """
    if task.execution_target == ExecutionTarget.local.value:
        return None
    return task.repository or None


async def verify_task_read_access(
    task: Task, user: User, auth_service: AuthorizationService
) -> None:
    """Ensure user has read permission on the task's repository or is creator of local task."""
    repository = _governing_repository(task)
    if repository:
        can_read = await auth_service.can_read_repository(user, repository)
        if not can_read:
            # 404 to avoid leaking existence of private repository tasks
            raise HTTPException(status_code=404, detail="Task not found")
    elif task.creator_id is None or task.creator_id != user.id:
        # A local task belongs to its creator alone; one with no recorded
        # creator belongs to nobody.  404, so existence is not disclosed.
        raise HTTPException(status_code=404, detail="Task not found")


async def verify_task_write_access(
    task: Task, user: User, auth_service: AuthorizationService
) -> None:
    """Ensure user has write/admin permission on the task's repository or is creator of local task."""
    repository = _governing_repository(task)
    if repository:
        can_write = await auth_service.can_write_repository(user, repository)
        if not can_write:
            raise HTTPException(
                status_code=403,
                detail=f"Write permission required for repository {repository}",
            )
    elif task.creator_id is None or task.creator_id != user.id:
        # Same rule as for reading; without read access, 404 rather than 403.
        raise HTTPException(status_code=404, detail="Task not found")


async def get_readable_task(task_id: str, user: User, session: AsyncSession) -> Task:
    """Load *task_id* if *user* may read it; otherwise raise 404 (missing or not permitted)."""
    task = await TaskRepository(session).get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await verify_task_read_access(task, user, AuthorizationService(session))
    return task
