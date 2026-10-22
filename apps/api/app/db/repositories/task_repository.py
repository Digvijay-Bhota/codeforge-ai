from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    PUBLICATION_APPROVAL,
    Task,
    TaskApproval,
    TaskEvent,
    TaskPublicationArtifact,
)


def matches_text(search: str) -> ColumnElement[bool]:
    """Tasks whose request or repository contains *search*, or whose ID starts with it (any case).

    Only narrows a query that is already limited to what the user may read.
    """
    escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return or_(
        Task.requested_task.ilike(f"%{escaped}%", escape="\\"),
        Task.repository.ilike(f"%{escaped}%", escape="\\"),
        Task.task_id.like(f"{escaped.lower()}%", escape="\\"),
    )


class TaskRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_task(self, task: Task) -> Task:
        self.session.add(task)
        await self.session.flush()
        return task

    async def get_task(self, task_id: str) -> Task | None:
        result = await self.session.execute(
            select(Task).where(Task.task_id == task_id)
        )
        return result.scalars().first()

    async def get_task_for_update(self, task_id: str) -> Task | None:
        result = await self.session.execute(
            select(Task)
            .where(Task.task_id == task_id)
            .with_for_update()
            # Refresh an already-loaded instance: after waiting for the lock,
            # the caller must see the row as the previous holder left it.
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def list_tasks(
        self,
        repository: str | None = None,
        statuses: list[str] | None = None,
        creator_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
        search: str | None = None,
    ) -> tuple[list[Task], int]:
        stmt = select(Task)
        count_stmt = select(func.count(Task.task_id))
        if repository:
            # Repository listings are authorized by repository permission, which
            # never extends to local tasks (they are creator-only).
            repo_filter = (Task.repository == repository) & (Task.execution_target != "local")
            stmt = stmt.where(repo_filter)
            count_stmt = count_stmt.where(repo_filter)
        if statuses:
            stmt = stmt.where(Task.status.in_(statuses))
            count_stmt = count_stmt.where(Task.status.in_(statuses))
        if creator_id:
            stmt = stmt.where(Task.creator_id == creator_id)
            count_stmt = count_stmt.where(Task.creator_id == creator_id)
        if search:
            stmt = stmt.where(matches_text(search))
            count_stmt = count_stmt.where(matches_text(search))

        total = (await self.session.execute(count_stmt)).scalar() or 0
        stmt = stmt.order_by(Task.created_at.desc()).limit(limit).offset(offset)
        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total

    async def get_task_events(self, task_id: str, limit: int = 50, offset: int = 0) -> list[TaskEvent]:
        result = await self.session.execute(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id)
            .order_by(TaskEvent.created_at.asc(), TaskEvent.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def add_event(self, event: TaskEvent) -> TaskEvent:
        self.session.add(event)
        await self.session.flush()
        return event

    async def update_task(self, task: Task) -> Task:
        self.session.add(task)
        await self.session.flush()
        return task

    async def create_approval(self, approval: TaskApproval) -> TaskApproval:
        self.session.add(approval)
        await self.session.flush()
        return approval

    async def get_latest_approval(self, task_id: str) -> TaskApproval | None:
        """The latest plan approval.  Publication approvals are never returned here."""
        result = await self.session.execute(
            select(TaskApproval)
            .where(
                TaskApproval.task_id == task_id,
                TaskApproval.approval_type != PUBLICATION_APPROVAL,
            )
            .order_by(TaskApproval.id.desc())
        )
        return result.scalars().first()

    async def get_latest_publication_approval(
        self, task_id: str, for_update: bool = False
    ) -> TaskApproval | None:
        stmt = (
            select(TaskApproval)
            .where(
                TaskApproval.task_id == task_id,
                TaskApproval.approval_type == PUBLICATION_APPROVAL,
            )
            .order_by(TaskApproval.id.desc())
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_publication_artifact(
        self, task_id: str, for_update: bool = False
    ) -> TaskPublicationArtifact | None:
        stmt = select(TaskPublicationArtifact).where(TaskPublicationArtifact.task_id == task_id)
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_approval_for_update(self, approval_id: int) -> TaskApproval | None:
        result = await self.session.execute(
            select(TaskApproval).where(TaskApproval.id == approval_id).with_for_update()
        )
        return result.scalars().first()

    async def update_approval(self, approval: TaskApproval) -> TaskApproval:
        self.session.add(approval)
        await self.session.flush()
        return approval

    async def get_task_by_github_comment(
        self, repository_id: int, comment_id: int
    ) -> Task | None:
        from app.db.models import TaskGitHubLink

        stmt = (
            select(Task)
            .join(TaskGitHubLink, TaskGitHubLink.task_id == Task.task_id)
            .where(
                TaskGitHubLink.repository_id == repository_id,
                TaskGitHubLink.trigger_comment_id == comment_id,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

