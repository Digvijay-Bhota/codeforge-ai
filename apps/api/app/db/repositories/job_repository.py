
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.db.models import Job, JobStatusEnum


class JobRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_job(self, job: Job) -> Job:
        self.session.add(job)
        await self.session.flush()
        return job

    async def get_job(self, job_id: int) -> Job | None:
        result = await self.session.execute(
            select(Job).where(Job.id == job_id)
        )
        return result.scalars().first()

    async def get_job_by_task(self, task_id: str) -> Job | None:
        result = await self.session.execute(
            select(Job).where(Job.task_id == task_id)
        )
        return result.scalars().first()

    async def get_job_for_update(self, job_id: int) -> Job | None:
        result = await self.session.execute(
            select(Job).where(Job.id == job_id).with_for_update()
        )
        return result.scalars().first()

    async def claim_job(self, job_id: int, worker_id: str, max_attempts: int = 3, lease_seconds: int = 3600) -> Job | None:
        """Atomically claim a job for execution."""
        from datetime import timedelta
        stmt = (
            update(Job)
            .where(
                Job.id == job_id,
                Job.status.in_([JobStatusEnum.PENDING.value, JobStatusEnum.RUNNING.value]),
                Job.attempt_count < max_attempts,
                Job.available_at <= func.now()
            )
            .values(
                status=JobStatusEnum.RUNNING.value,
                worker_id=worker_id,
                started_at=func.now(),
                attempt_count=Job.attempt_count + 1,
                available_at=func.now() + timedelta(seconds=lease_seconds),
                lease_version=Job.lease_version + 1
            )
            .returning(Job)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def renew_lease(self, job_id: int, worker_id: str, lease_version: int, lease_seconds: int = 3600) -> bool:
        """Renew the lease if we still own it."""
        from datetime import timedelta
        stmt = (
            update(Job)
            .where(
                Job.id == job_id,
                Job.worker_id == worker_id,
                Job.lease_version == lease_version,
                Job.status == JobStatusEnum.RUNNING.value
            )
            .values(
                available_at=func.now() + timedelta(seconds=lease_seconds)
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0
