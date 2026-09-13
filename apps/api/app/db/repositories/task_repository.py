from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Task, TaskApproval, TaskEvent


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
            select(Task).where(Task.task_id == task_id).with_for_update()
        )
        return result.scalars().first()

    async def list_tasks(
        self,
        repository: str | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Task], int]:
        stmt = select(Task)
        count_stmt = select(func.count(Task.task_id))
        if repository:
            stmt = stmt.where(Task.repository == repository)
            count_stmt = count_stmt.where(Task.repository == repository)
        if status:
            stmt = stmt.where(Task.status == status)
            count_stmt = count_stmt.where(Task.status == status)

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
        result = await self.session.execute(
            select(TaskApproval)
            .where(TaskApproval.task_id == task_id)
            .order_by(TaskApproval.id.desc())
        )
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
