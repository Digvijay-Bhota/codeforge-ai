"""CF-04 regression tests: job recovery, retry exhaustion and lease configuration.

Before this fix:
* a job re-delivered after its worker died mid-run could not start (its task
  had already left QUEUED), and process_job returned without committing, which
  rolled the claim back: the job sat PENDING forever, with no queue message
  and its attempt uncounted;
* nothing ever re-enqueued a PENDING job whose queue message was lost, nor
  failed a job that had used all its attempts;
* leases were hard-coded to one hour instead of TASK_LEASE_SECONDS.

Timing is controlled through the database clock (available_at relative to
now()), so nothing here sleeps.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db.models import Job, JobStatusEnum, OutboxEvent, Task, TaskEvent, TaskStatusEnum
from app.db.repositories.job_repository import MAX_JOB_ATTEMPTS, JobRepository
from app.services.task_service import TaskService
from app.worker import (
    JOB_ATTEMPTS_EXHAUSTED,
    JOB_INTERRUPTED,
    heartbeat_interval,
    process_job,
    recover_jobs,
)


class FakeQueue:
    """Records enqueues; is_queued reports what was enqueued and not yet taken."""

    def __init__(self) -> None:
        self.enqueued: list[int] = []
        self.waiting: set[int] = set()

    async def enqueue(self, job_id: int) -> None:
        self.enqueued.append(job_id)
        self.waiting.add(job_id)

    async def is_queued(self, job_id: int) -> bool:
        return job_id in self.waiting

    def take(self, job_id: int) -> None:
        self.waiting.discard(job_id)


@pytest.fixture
def maker(session: AsyncSession) -> Any:
    m = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
    with patch("app.worker.async_session_maker", m):
        yield m


async def make_job(
    session: AsyncSession,
    *,
    job_status: JobStatusEnum = JobStatusEnum.PENDING,
    task_status: TaskStatusEnum = TaskStatusEnum.PENDING,
    attempts: int = 0,
    available_in: str | None = None,
    target: str = "github",
) -> Job:
    """A task and its job; *available_in* is a Postgres interval relative to now()."""
    task = Task(
        task_id=str(uuid.uuid4()),
        status=task_status.value,
        requested_task="Fix the add function",
        execution_target=target,
        repository="o/r" if target == "github" else None,
    )
    session.add(task)
    await session.flush()
    job = Job(task_id=task.task_id, status=job_status.value, attempt_count=attempts,
              worker_id="w-old" if job_status == JobStatusEnum.RUNNING else None)  # fmt: skip
    session.add(job)
    await session.commit()
    if available_in is not None:
        await session.execute(
            text(f"UPDATE jobs SET available_at = now() + INTERVAL '{available_in}' WHERE id = :id"),
            {"id": job.id},
        )
        await session.commit()
    return job


async def reload(maker: Any, job: Job) -> tuple[Job, Task]:
    async with maker() as s:
        j = (await s.execute(select(Job).where(Job.id == job.id))).scalar_one()
        t = (await s.execute(select(Task).where(Task.task_id == job.task_id))).scalar_one()
        return j, t


async def failure_events(maker: Any, task_id: str) -> int:
    async with maker() as s:
        rows = (await s.execute(select(OutboxEvent).where(OutboxEvent.aggregate_id == task_id))).scalars().all()
        return sum(1 for r in rows if r.payload.get("event") == "JOB_FAILED")


async def task_event_count(maker: Any, task_id: str) -> int:
    async with maker() as s:
        return len((await s.execute(select(TaskEvent).where(TaskEvent.task_id == task_id))).scalars().all())


def lease() -> int:
    return settings.task_lease_seconds


# ── Retry exhaustion ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_job_below_the_attempt_limit_can_still_be_claimed(session: AsyncSession) -> None:
    job = await make_job(session, attempts=MAX_JOB_ATTEMPTS - 1)
    claimed = await JobRepository(session).claim_job(job.id, "w1")
    assert claimed is not None and claimed.attempt_count == MAX_JOB_ATTEMPTS
    await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("job_status,available_in", [
    (JobStatusEnum.RUNNING, "-1 second"),   # lease expired after the last attempt
    (JobStatusEnum.PENDING, "-1 second"),   # can never be claimed again
])  # fmt: skip
async def test_exhausted_job_and_its_task_fail_once(
    session: AsyncSession, maker: Any, job_status: JobStatusEnum, available_in: str
) -> None:
    job = await make_job(session, job_status=job_status, task_status=TaskStatusEnum.TESTING,
                         attempts=MAX_JOB_ATTEMPTS, available_in=available_in)  # fmt: skip
    queue = FakeQueue()

    first = await recover_jobs(queue)
    assert first.failed == [job.id]
    j, t = await reload(maker, job)
    assert (j.status, j.worker_id, j.completed_at is not None) == (JobStatusEnum.FAILED.value, None, True)
    assert "exhausted" in (j.last_error or "")
    assert t.status == TaskStatusEnum.FAILED.value
    assert t.failure_code == JOB_ATTEMPTS_EXHAUSTED and t.completed_at is not None
    assert await failure_events(maker, t.task_id) == 1

    # Idempotent: later passes change nothing and emit nothing.
    events_before = await task_event_count(maker, t.task_id)
    second = await recover_jobs(queue)
    assert second.failed == [] and second.released == [] and second.requeued == []
    assert await failure_events(maker, t.task_id) == 1
    assert await task_event_count(maker, t.task_id) == events_before
    assert queue.enqueued == []  # never re-enqueued


@pytest.mark.asyncio
async def test_exhausted_job_with_an_active_lease_is_left_alone(session: AsyncSession, maker: Any) -> None:
    """The worker on its last attempt is still alive; nothing must interrupt it."""
    job = await make_job(session, job_status=JobStatusEnum.RUNNING, task_status=TaskStatusEnum.TESTING,
                         attempts=MAX_JOB_ATTEMPTS, available_in="5 minutes")  # fmt: skip
    assert (await recover_jobs(FakeQueue())).failed == []
    j, t = await reload(maker, job)
    assert (j.status, t.status) == (JobStatusEnum.RUNNING.value, TaskStatusEnum.TESTING.value)


@pytest.mark.asyncio
async def test_exhaustion_leaves_an_already_terminal_task_unchanged(session: AsyncSession, maker: Any) -> None:
    job = await make_job(session, task_status=TaskStatusEnum.CANCELLED, attempts=MAX_JOB_ATTEMPTS)
    await recover_jobs(FakeQueue())
    j, t = await reload(maker, job)
    assert j.status == JobStatusEnum.FAILED.value
    assert (t.status, t.failure_code) == (TaskStatusEnum.CANCELLED.value, None)
    assert await failure_events(maker, t.task_id) == 0


@pytest.mark.asyncio
async def test_user_retry_after_exhaustion_still_works(session: AsyncSession, maker: Any) -> None:
    job = await make_job(session, task_status=TaskStatusEnum.TESTING, attempts=MAX_JOB_ATTEMPTS)
    await recover_jobs(FakeQueue())
    child = await TaskService(session).retry_task(job.task_id)
    await session.commit()
    new_job = (await session.execute(select(Job).where(Job.task_id == child.task_id))).scalar_one()
    assert (child.status, child.parent_task_id) == (TaskStatusEnum.PENDING.value, job.task_id)
    assert (new_job.status, new_job.attempt_count) == (JobStatusEnum.PENDING.value, 0)


# ── Interrupted jobs (the claim must not be rolled back) ───────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("task_status", [TaskStatusEnum.TESTING, TaskStatusEnum.ANALYZING,
                                         TaskStatusEnum.PLANNING, TaskStatusEnum.CODING])  # fmt: skip
async def test_redelivered_interrupted_job_fails_instead_of_vanishing(
    session: AsyncSession, maker: Any, task_status: TaskStatusEnum
) -> None:
    """The reproduced CF-04 hang: before the fix this left the job PENDING, attempt 1, forever."""
    job = await make_job(session, task_status=task_status, attempts=1, available_in="-1 second")
    await process_job(job.id)
    j, t = await reload(maker, job)
    assert (j.status, j.attempt_count) == (JobStatusEnum.FAILED.value, 2)
    assert t.status == TaskStatusEnum.FAILED.value and t.failure_code == JOB_INTERRUPTED
    assert "Retry the task" in (t.failure_reason or "")
    assert await failure_events(maker, t.task_id) == 1
    queue = FakeQueue()
    assert (await recover_jobs(queue)).requeued == [] and queue.enqueued == []


@pytest.mark.asyncio
async def test_job_for_an_already_cancelled_task_is_closed_without_touching_the_task(
    session: AsyncSession, maker: Any
) -> None:
    job = await make_job(session, task_status=TaskStatusEnum.CANCELLED)
    await process_job(job.id)
    j, t = await reload(maker, job)
    assert j.status == JobStatusEnum.FAILED.value and "already CANCELLED" in (j.last_error or "")
    assert (t.status, t.failure_code) == (TaskStatusEnum.CANCELLED.value, None)


@pytest.mark.asyncio
async def test_duplicate_delivery_of_a_running_job_changes_nothing(session: AsyncSession, maker: Any) -> None:
    job = await make_job(session, job_status=JobStatusEnum.RUNNING, task_status=TaskStatusEnum.TESTING,
                         attempts=1, available_in="5 minutes")  # fmt: skip
    await process_job(job.id)  # claim fails: the lease is valid
    j, t = await reload(maker, job)
    assert (j.status, j.attempt_count, t.status) == (
        JobStatusEnum.RUNNING.value, 1, TaskStatusEnum.TESTING.value)


def leaked_heartbeats() -> list[asyncio.Task[Any]]:
    return [t for t in asyncio.all_tasks()
            if not t.done() and getattr(t.get_coro(), "__name__", "") == "heartbeat_loop_wrapper"]  # fmt: skip


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["database-error", "invalid-task-row"])
async def test_failure_before_execution_leaves_no_heartbeat_renewing_the_lease(
    session: AsyncSession, maker: Any, failure: str
) -> None:
    """A leaked heartbeat would renew the lease forever, so recovery could never release the job."""
    if failure == "invalid-task-row":
        # A local task without a workspace path: building its TaskRequest raises.
        job = await make_job(session, target="local")
        get_task = None
    else:
        job = await make_job(session)
        from app.db.repositories.task_repository import TaskRepository

        real_get_task = TaskRepository.get_task
        calls = {"n": 0}

        async def get_task(self: Any, task_id: str) -> Any:
            calls["n"] += 1
            if calls["n"] == 2:  # the execution session, after the claim committed
                raise ConnectionError("database unavailable")
            return await real_get_task(self, task_id)

    before = set(leaked_heartbeats())
    try:
        if get_task is None:
            with pytest.raises(ValidationError):  # TaskRequest rejects the row
                await process_job(job.id)
        else:
            with patch("app.db.repositories.task_repository.TaskRepository.get_task", new=get_task), \
                 pytest.raises(ConnectionError):  # fmt: skip
                await process_job(job.id)
        leaked = [t for t in leaked_heartbeats() if t not in before]
        assert leaked == [], "heartbeat still running after process_job failed"
    finally:
        for t in leaked_heartbeats():
            if t not in before:
                t.cancel()

    # The claim committed, so the job is RUNNING on its claim lease; once that
    # expires (forced here), recovery releases it and the next delivery closes it.
    await session.execute(text("UPDATE jobs SET available_at = now() - INTERVAL '1 second' WHERE id = :id"),
                          {"id": job.id})  # fmt: skip
    await session.commit()
    queue = FakeQueue()
    assert (await recover_jobs(queue)).released == [job.id]


# ── Lost queue messages ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_pending_job_is_requeued_once(session: AsyncSession, maker: Any) -> None:
    job = await make_job(session, available_in=f"-{lease() + 5} seconds")
    queue = FakeQueue()
    assert (await recover_jobs(queue)).requeued == [job.id]
    # The next pass neither re-sends (message still queued, available_at fresh)...
    assert (await recover_jobs(queue)).requeued == []
    # ...nor re-sends after a worker took the message, until a full period passes.
    queue.take(job.id)
    assert (await recover_jobs(queue)).requeued == []
    assert queue.enqueued == [job.id]
    j, _ = await reload(maker, job)
    assert (j.status, j.attempt_count) == (JobStatusEnum.PENDING.value, 0)


@pytest.mark.asyncio
async def test_stale_pending_job_still_in_the_queue_is_not_duplicated(
    session: AsyncSession, maker: Any
) -> None:
    job = await make_job(session, available_in=f"-{lease() + 5} seconds")
    queue = FakeQueue()
    queue.waiting.add(job.id)  # its original message is still waiting
    assert (await recover_jobs(queue)).requeued == []
    assert queue.enqueued == []


@pytest.mark.asyncio
async def test_fresh_pending_job_is_not_treated_as_lost(session: AsyncSession, maker: Any) -> None:
    await make_job(session, available_in=f"-{lease() - 5} seconds")
    await make_job(session)  # just created
    queue = FakeQueue()
    assert (await recover_jobs(queue)).requeued == [] and queue.enqueued == []


@pytest.mark.asyncio
@pytest.mark.parametrize("job_status", [JobStatusEnum.SUCCEEDED, JobStatusEnum.FAILED])
async def test_terminal_jobs_are_never_requeued(
    session: AsyncSession, maker: Any, job_status: JobStatusEnum
) -> None:
    await make_job(session, job_status=job_status, task_status=TaskStatusEnum.COMPLETED,
                   attempts=1, available_in="-1 day")  # fmt: skip
    queue = FakeQueue()
    result = await recover_jobs(queue)
    assert (result.failed, result.released, result.requeued, queue.enqueued) == ([], [], [], [])


@pytest.mark.asyncio
async def test_lost_message_job_runs_after_recovery(session: AsyncSession, maker: Any) -> None:
    """End to end: a PENDING job with no message becomes discoverable and is executed."""
    job = await make_job(session, available_in=f"-{lease() + 5} seconds")
    queue = FakeQueue()
    await recover_jobs(queue)
    assert queue.enqueued == [job.id]
    seen: list[str] = []

    async def execute(self: Any, request: Any, task_id: str, **kwargs: Any) -> Any:
        seen.append(task_id)
        raise RuntimeError("stop after proving the job ran")

    with patch("app.services.task_service.TaskService.execute_task", new=execute):
        await process_job(queue.enqueued[0])
    assert seen == [job.task_id]
    j, _ = await reload(maker, job)
    assert (j.status, j.attempt_count) == (JobStatusEnum.FAILED.value, 1)


# ── Expired and valid leases ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expired_lease_returns_the_job_for_another_attempt(session: AsyncSession, maker: Any) -> None:
    job = await make_job(session, job_status=JobStatusEnum.RUNNING, task_status=TaskStatusEnum.TESTING,
                         attempts=1, available_in="-1 second")  # fmt: skip
    queue = FakeQueue()
    result = await recover_jobs(queue)
    assert (result.released, result.requeued, queue.enqueued) == ([job.id], [], [job.id])
    j, t = await reload(maker, job)
    assert (j.status, j.worker_id, j.lease_version) == (JobStatusEnum.PENDING.value, None, 1)
    assert t.status == TaskStatusEnum.TESTING.value  # decided when the job is next claimed
    # available_at was reset, so the same job is not also counted as stale.
    assert (await recover_jobs(queue)).requeued == []


class BlindQueue(FakeQueue):
    """A queue that cannot report membership (is_queued fails closed to False)."""

    async def is_queued(self, job_id: int) -> bool:
        return False


@pytest.mark.asyncio
async def test_long_expired_lease_is_enqueued_once_even_if_the_queue_cannot_be_checked(
    session: AsyncSession, maker: Any
) -> None:
    """Releasing resets available_at, so the job is not also re-sent as stale."""
    job = await make_job(session, job_status=JobStatusEnum.RUNNING, task_status=TaskStatusEnum.TESTING,
                         attempts=1, available_in=f"-{lease() + 60} seconds")  # fmt: skip
    queue = BlindQueue()
    result = await recover_jobs(queue)
    assert (result.released, result.requeued) == ([job.id], [])
    assert (await recover_jobs(queue)).requeued == []
    assert queue.enqueued == [job.id]


@pytest.mark.asyncio
async def test_valid_lease_is_not_mistaken_for_a_dead_worker(session: AsyncSession, maker: Any) -> None:
    job = await make_job(session, job_status=JobStatusEnum.RUNNING, task_status=TaskStatusEnum.TESTING,
                         attempts=1, available_in="2 seconds")  # fmt: skip
    queue = FakeQueue()
    result = await recover_jobs(queue)
    assert (result.failed, result.released, result.requeued, queue.enqueued) == ([], [], [], [])
    j, _ = await reload(maker, job)
    assert (j.status, j.worker_id) == (JobStatusEnum.RUNNING.value, "w-old")


@pytest.mark.asyncio
async def test_lease_expiry_compares_instants_not_wall_clock_zones(session: AsyncSession, maker: Any) -> None:
    """available_at written in a +05:30 offset is compared as the same instant."""
    ist = timezone(timedelta(hours=5, minutes=30))
    expired = await make_job(session, job_status=JobStatusEnum.RUNNING, attempts=1)
    alive = await make_job(session, job_status=JobStatusEnum.RUNNING, attempts=1)
    db_now = (await session.execute(select(func.now()))).scalar_one()
    for job, offset in ((expired, -2), (alive, 120)):
        stored = await session.get(Job, job.id)
        assert stored is not None
        stored.available_at = (db_now + timedelta(seconds=offset)).astimezone(ist)
    await session.commit()
    assert (await recover_jobs(FakeQueue())).released == [expired.id]


@pytest.mark.asyncio
async def test_claim_and_renewal_use_the_configured_lease(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "task_lease_seconds", 123)
    job = await make_job(session)
    repo = JobRepository(session)
    claimed = await repo.claim_job(job.id, "w1")
    assert claimed is not None
    # now() is fixed within a transaction, so the difference is exact.
    seconds = "SELECT EXTRACT(EPOCH FROM available_at - now()) FROM jobs WHERE id = :id"
    assert (await session.execute(text(seconds), {"id": job.id})).scalar_one() == 123
    await session.commit()
    assert await repo.renew_lease(job.id, "w1", claimed.lease_version)
    assert (await session.execute(text(seconds), {"id": job.id})).scalar_one() == 123
    await session.commit()


@pytest.mark.parametrize("lease_seconds,expected", [(300, 60.0), (90, 30.0), (30, 10.0)])
def test_heartbeat_renews_several_times_per_lease(
    monkeypatch: pytest.MonkeyPatch, lease_seconds: int, expected: float
) -> None:
    monkeypatch.setattr(settings, "task_lease_seconds", lease_seconds)
    assert heartbeat_interval() == expected


def test_no_hard_coded_one_hour_lease_remains() -> None:
    import inspect

    import app.db.repositories.job_repository as job_repository
    import app.worker as worker

    for module in (job_repository, worker):
        assert "3600" not in inspect.getsource(module), module.__name__


# ── Concurrent recovery ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_recovery_passes_act_on_each_job_once(session: AsyncSession, maker: Any) -> None:
    exhausted = await make_job(session, job_status=JobStatusEnum.RUNNING, task_status=TaskStatusEnum.TESTING,
                               attempts=MAX_JOB_ATTEMPTS, available_in="-1 second")  # fmt: skip
    expired = await make_job(session, job_status=JobStatusEnum.RUNNING, task_status=TaskStatusEnum.TESTING,
                             attempts=1, available_in="-1 second")  # fmt: skip
    stale = await make_job(session, available_in=f"-{lease() + 5} seconds")
    queue = FakeQueue()

    a, b = await asyncio.gather(recover_jobs(queue), recover_jobs(queue))

    assert sorted(a.failed + b.failed) == [exhausted.id]
    assert sorted(a.released + b.released) == [expired.id]
    assert sorted(queue.enqueued) == sorted([expired.id, stale.id])
    assert await failure_events(maker, exhausted.task_id) == 1
    j, t = await reload(maker, exhausted)
    assert (j.status, t.status) == (JobStatusEnum.FAILED.value, TaskStatusEnum.FAILED.value)


# ── Failure windows (adversarial review) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_database_error_during_claim_rolls_back_and_the_job_is_recovered(
    session: AsyncSession, maker: Any
) -> None:
    """The claim and the first task transition commit together, or not at all."""
    job = await make_job(session)

    async def unavailable(self: Any, task_id: str) -> Any:
        raise ConnectionError("database unavailable")

    with patch("app.db.repositories.task_repository.TaskRepository.get_latest_approval", new=unavailable), \
         pytest.raises(ConnectionError):  # fmt: skip
        await process_job(job.id)
    j, t = await reload(maker, job)
    assert (j.status, j.attempt_count, t.status) == (JobStatusEnum.PENDING.value, 0, TaskStatusEnum.PENDING.value)
    # Its message was consumed; once stale, recovery sends it again.
    await session.execute(text(f"UPDATE jobs SET available_at = now() - INTERVAL '{lease() + 5} seconds' "
                               "WHERE id = :id"), {"id": job.id})  # fmt: skip
    await session.commit()
    assert (await recover_jobs(FakeQueue())).requeued == [job.id]


class FailingQueue(FakeQueue):
    async def enqueue(self, job_id: int) -> None:
        raise ConnectionError("redis unavailable")


@pytest.mark.asyncio
async def test_crash_between_recovery_commit_and_enqueue_is_recovered_later(
    session: AsyncSession, maker: Any
) -> None:
    job = await make_job(session, job_status=JobStatusEnum.RUNNING, task_status=TaskStatusEnum.TESTING,
                         attempts=1, available_in="-1 second")  # fmt: skip
    with pytest.raises(ConnectionError):
        await recover_jobs(FailingQueue())
    j, _ = await reload(maker, job)
    assert j.status == JobStatusEnum.PENDING.value  # the database change was committed first
    queue = FakeQueue()
    assert (await recover_jobs(queue)).requeued == []  # not yet stale: no message storm
    await session.execute(text(f"UPDATE jobs SET available_at = now() - INTERVAL '{lease() + 5} seconds' "
                               "WHERE id = :id"), {"id": job.id})  # fmt: skip
    await session.commit()
    assert (await recover_jobs(queue)).requeued == [job.id]


class FakeRedis:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    async def lpos(self, name: str, value: str) -> Any:
        self.calls.append((name, value))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.asyncio
@pytest.mark.parametrize("result,expected", [(0, True), (7, True), (None, False),
                                             (ConnectionError("down"), False)])  # fmt: skip
async def test_is_queued_checks_the_task_queue_and_fails_open(result: Any, expected: bool) -> None:
    """Index 0 still means queued; a Redis error means 'not known to be queued'."""
    from app.services.queue_service import QueueService

    redis = FakeRedis(result)
    with patch("app.services.queue_service.get_redis_client", return_value=redis):
        assert await QueueService().is_queued(42) is expected
    assert redis.calls == [(settings.task_queue_name, "42")]


async def wait_until_recovery_blocks(maker: Any) -> None:
    """Return once some session is waiting on a row lock (condition-driven, bounded)."""
    sql = text("SELECT count(*) FROM pg_stat_activity "
               "WHERE wait_event_type = 'Lock' AND datname = current_database()")  # fmt: skip
    for _ in range(500):
        async with maker() as s:
            if (await s.execute(sql)).scalar_one():
                return
        await asyncio.sleep(0.01)
    raise AssertionError("recovery never waited for the row lock")


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["lease-renewal", "job-completion"])
async def test_recovery_racing_with_the_owner_respects_the_owner(
    session: AsyncSession, maker: Any, winner: str
) -> None:
    """The owner's update holds the row; recovery re-checks it after the owner commits."""
    job = await make_job(session, job_status=JobStatusEnum.RUNNING, task_status=TaskStatusEnum.TESTING,
                         attempts=1, available_in="-1 second")  # fmt: skip
    async with maker() as owner:
        repo = JobRepository(owner)
        if winner == "lease-renewal":
            assert await repo.renew_lease(job.id, "w-old", 0)
        else:
            row = await repo.get_job_for_update(job.id)
            assert row is not None
            row.status = JobStatusEnum.SUCCEEDED.value
            await owner.flush()
        recovery = asyncio.create_task(recover_jobs(FakeQueue()))
        await wait_until_recovery_blocks(maker)
        await owner.commit()
    result = await recovery
    assert (result.failed, result.released, result.requeued) == ([], [], [])
    j, _ = await reload(maker, job)
    expected = JobStatusEnum.RUNNING.value if winner == "lease-renewal" else JobStatusEnum.SUCCEEDED.value
    assert (j.status, j.lease_version) == (expected, 0)


@pytest.mark.asyncio
async def test_task_paused_for_approval_is_not_treated_as_abandoned(session: AsyncSession, maker: Any) -> None:
    """Stage A finished (job SUCCEEDED); the task waits for a human, however long."""
    job = await make_job(session, job_status=JobStatusEnum.SUCCEEDED,
                         task_status=TaskStatusEnum.WAITING_APPROVAL, attempts=1, available_in="-7 days")  # fmt: skip
    queue = FakeQueue()
    result = await recover_jobs(queue)
    assert (result.failed, result.released, result.requeued, queue.enqueued) == ([], [], [], [])
    j, t = await reload(maker, job)
    assert (j.status, t.status) == (JobStatusEnum.SUCCEEDED.value, TaskStatusEnum.WAITING_APPROVAL.value)
