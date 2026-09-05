from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Task, TaskEvent


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
