from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.config import settings
from app.db.models import Job, JobStatusEnum

# Claims a job may consume.  A job that has used them all is never claimed
# again; recovery marks it FAILED (see fail_exhausted_jobs).
MAX_JOB_ATTEMPTS = 3


def _lease(lease_seconds: int | None) -> timedelta:
    """The lease length: *lease_seconds*, or TASK_LEASE_SECONDS when None."""
    return timedelta(seconds=settings.task_lease_seconds if lease_seconds is None else lease_seconds)


class JobRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_job(self, job: Job) -> Job:
        self.session.add(job)
        await self.session.flush()
        return job

    async def get_job(self, job_id: int) -> Job | None:
        result = await self.session.execute(select(Job).where(Job.id == job_id))
        return result.scalars().first()

    async def get_job_by_task(self, task_id: str) -> Job | None:
        """Fetch the latest job associated with a task (highest job id)."""
        result = await self.session.execute(
            select(Job).where(Job.task_id == task_id).order_by(Job.id.desc())
        )
        return result.scalars().first()

    async def get_jobs_for_task(self, task_id: str) -> list[Job]:
        """Fetch all historical and current jobs for a task in chronological order."""
        result = await self.session.execute(
            select(Job).where(Job.task_id == task_id).order_by(Job.id.asc())
        )
        return list(result.scalars().all())

    async def get_job_for_update(self, job_id: int) -> Job | None:
        result = await self.session.execute(select(Job).where(Job.id == job_id).with_for_update())
        return result.scalars().first()

    async def claim_job(
        self,
        job_id: int,
        worker_id: str,
        max_attempts: int = MAX_JOB_ATTEMPTS,
        lease_seconds: int | None = None,
    ) -> Job | None:
        """Atomically claim a job for execution, leased for TASK_LEASE_SECONDS by default."""
        stmt = (
            update(Job)
            .where(
                Job.id == job_id,
                Job.status.in_([JobStatusEnum.PENDING.value, JobStatusEnum.RUNNING.value]),
                Job.attempt_count < max_attempts,
                Job.available_at <= func.now(),
            )
            .values(
                status=JobStatusEnum.RUNNING.value,
                worker_id=worker_id,
                started_at=func.now(),
                attempt_count=Job.attempt_count + 1,
                available_at=func.now() + _lease(lease_seconds),
                lease_version=Job.lease_version + 1,
            )
            .returning(Job)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def renew_lease(
        self, job_id: int, worker_id: str, lease_version: int, lease_seconds: int | None = None
    ) -> bool:
        """Renew the lease (TASK_LEASE_SECONDS by default) if we still own it."""
        stmt = (
            update(Job)
            .where(
                Job.id == job_id,
                Job.worker_id == worker_id,
                Job.lease_version == lease_version,
                Job.status == JobStatusEnum.RUNNING.value,
            )
            .values(available_at=func.now() + _lease(lease_seconds))
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

    # ── Recovery ────────────────────────────────────────────────────────────
    # Each method is one conditional UPDATE ... RETURNING, so concurrent
    # recovery passes (one per worker) never act on the same row twice:
    # whichever commits first changes the row so it no longer matches.

    async def fail_exhausted_jobs(self, error: str) -> list[Job]:
        """Mark FAILED every job that has used all its attempts and is not actively leased.

        That is a PENDING job (it can never be claimed again) or a RUNNING
        job whose lease has expired.  Bumping lease_version fences off any
        worker that still believes it owns the job.
        """
        stmt = (
            update(Job)
            .where(
                Job.attempt_count >= MAX_JOB_ATTEMPTS,
                (Job.status == JobStatusEnum.PENDING.value)
                | ((Job.status == JobStatusEnum.RUNNING.value) & (Job.available_at <= func.now())),
            )
            .values(
                status=JobStatusEnum.FAILED.value,
                worker_id=None,
                completed_at=func.now(),
                last_error=error,
                lease_version=Job.lease_version + 1,
            )
            .returning(Job)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def release_expired_leases(self) -> list[int]:
        """Return to PENDING every RUNNING job whose lease expired and that has attempts left.

        available_at is reset to now, so the job is claimable at once and is
        not mistaken for a stale PENDING job by the same recovery pass.
        """
        stmt = (
            update(Job)
            .where(
                Job.status == JobStatusEnum.RUNNING.value,
                Job.available_at <= func.now(),
                Job.attempt_count < MAX_JOB_ATTEMPTS,
            )
            .values(
                status=JobStatusEnum.PENDING.value,
                worker_id=None,
                lease_version=Job.lease_version + 1,
                available_at=func.now(),
            )
            .returning(Job.id)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def refresh_stale_pending(self, stale_after_seconds: int) -> list[int]:
        """Return PENDING jobs unclaimed for *stale_after_seconds*, refreshing their available_at.

        Such a job has presumably lost its queue message (a worker died
        between dequeue and claim, or Redis lost data).  Refreshing
        available_at means each job is returned at most once per period.
        """
        stmt = (
            update(Job)
            .where(
                Job.status == JobStatusEnum.PENDING.value,
                Job.available_at <= func.now() - timedelta(seconds=stale_after_seconds),
                Job.attempt_count < MAX_JOB_ATTEMPTS,
            )
            .values(available_at=func.now())
            .returning(Job.id)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
