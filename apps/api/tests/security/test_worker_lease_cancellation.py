"""Phase 10B.5 Stage 1A — lease loss cancels the active execution.

When the worker's heartbeat can no longer renew the job lease (or the DB
names another owner), the running execution — agent, test executor and all —
must be cancelled, its cleanup awaited, and nothing finalized: no task/job
state, no task result, no GitHub status event.  Cancellation is scoped to the
job whose lease was lost.

The process_job tests use the real heartbeat with a shortened interval and a
real Postgres; only TaskService.execute_task is replaced.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db.models import Job, JobStatusEnum, OutboxEvent, Task, TaskApproval, TaskStatusEnum
from app.db.repositories.job_repository import JobRepository
from app.db.repositories.task_repository import TaskRepository
from app.execution.host_executor import HostTestExecutor
from app.execution.ownership import OwnershipLostError, run_until_ownership_lost
from app.execution.test_executor import run_tests_in_snapshot
from app.schemas.task import TaskResult, TaskStatus
from app.worker import process_job

# ── run_until_ownership_lost ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_returns_the_result_while_ownership_holds() -> None:
    async def work() -> str:
        await asyncio.sleep(0.01)
        return "ok"

    assert await run_until_ownership_lost(work(), asyncio.Event()) == "ok"


@pytest.mark.asyncio
async def test_lease_loss_cancels_work_and_awaits_its_cleanup() -> None:
    lost = asyncio.Event()
    trail: list[str] = []

    async def work() -> str:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            trail.append("cancelled")
            await asyncio.sleep(0.05)  # e.g. killing a process group
            trail.append("cleaned up")
            raise
        return "stale"

    asyncio.get_running_loop().call_later(0.05, lost.set)
    start = time.monotonic()
    with pytest.raises(OwnershipLostError):
        await run_until_ownership_lost(work(), lost)
    assert trail == ["cancelled", "cleaned up"]
    assert time.monotonic() - start < 5


@pytest.mark.asyncio
async def test_already_lost_lease_never_runs_the_work_to_completion() -> None:
    lost = asyncio.Event()
    lost.set()
    finished = False

    async def work() -> None:
        nonlocal finished
        await asyncio.sleep(1)
        finished = True

    with pytest.raises(OwnershipLostError):
        await run_until_ownership_lost(work(), lost)
    assert finished is False


@pytest.mark.asyncio
async def test_result_from_work_that_ignores_cancellation_is_discarded() -> None:
    lost = asyncio.Event()

    async def stubborn() -> str:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            return "stale result"  # swallows cancellation
        return "never"

    asyncio.get_running_loop().call_later(0.05, lost.set)
    with pytest.raises(OwnershipLostError):
        await run_until_ownership_lost(stubborn(), lost)


@pytest.mark.asyncio
async def test_work_exceptions_propagate_unchanged() -> None:
    async def boom() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await run_until_ownership_lost(boom(), asyncio.Event())


@pytest.mark.asyncio
async def test_lease_loss_on_one_job_does_not_cancel_another() -> None:
    lost_a, lost_b = asyncio.Event(), asyncio.Event()

    async def work(tag: str) -> str:
        await asyncio.sleep(0.3)
        return tag

    job_a = asyncio.create_task(run_until_ownership_lost(work("a"), lost_a))
    job_b = asyncio.create_task(run_until_ownership_lost(work("b"), lost_b))
    await asyncio.sleep(0.05)
    lost_a.set()

    with pytest.raises(OwnershipLostError):
        await job_a
    assert await job_b == "b"


@pytest.mark.asyncio
async def test_cancelling_the_caller_also_cancels_the_work() -> None:
    """Worker shutdown must not leave an orphaned execution behind."""
    cancelled = asyncio.Event()

    async def work() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    outer = asyncio.create_task(run_until_ownership_lost(work(), asyncio.Event()))
    await asyncio.sleep(0.05)
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_lease_loss_kills_the_host_executor_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "app_env", "development")
    pidfile = tmp_path / "child.pid"
    (tmp_path / "test_spawn.py").write_text(
        "import subprocess, time\n"
        "def test_spawn():\n"
        "    p = subprocess.Popen(['sleep', '300'])\n"
        f"    open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "    time.sleep(300)\n"
    )
    lost = asyncio.Event()

    async def lose_lease_once_running() -> None:
        while not (pidfile.exists() and pidfile.read_text().strip()):
            await asyncio.sleep(0.05)
        lost.set()

    trigger = asyncio.create_task(lose_lease_once_running())
    with pytest.raises(OwnershipLostError):
        await asyncio.wait_for(
            run_until_ownership_lost(
                run_tests_in_snapshot(HostTestExecutor(timeout_seconds=120), tmp_path), lost
            ),
            timeout=30,
        )
    await trigger

    child = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
            with open(f"/proc/{child}/stat") as fh:
                if fh.read().split(") ", 1)[1].split()[0] == "Z":
                    break
        except (ProcessLookupError, FileNotFoundError):
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("child process survived lease loss")


# ── process_job integration (real heartbeat, real Postgres) ────────────────────

FAST_HEARTBEAT = 0.05
OTHER_WORKER = "worker-2"


def _maker(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)


async def _github_job(session: AsyncSession, **task_fields: Any) -> tuple[str, int]:
    task = Task(
        task_id=str(uuid.uuid4()),
        status=TaskStatusEnum.PENDING.value,
        execution_target="github",
        repository="owner/repo",
        requested_task="Fix the bug in the parser module",
        **task_fields,
    )
    session.add(task)
    await session.flush()
    job = Job(task_id=task.task_id, status=JobStatusEnum.PENDING.value)
    session.add(job)
    await session.commit()
    return task.task_id, job.id


def _success(task_id: str) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        status=TaskStatus.success,
        description="Fix the bug in the parser module",
        plan=[],
        changed_files=[],
        diff="",
        test_result=None,
        error_message=None,
        agent_output="done",
    )


async def _reclaim_by_other_worker(maker: async_sessionmaker[AsyncSession], job_id: int) -> None:
    """What the reaper plus a second worker's claim do to the row."""
    async with maker() as s:
        await s.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(worker_id=OTHER_WORKER, lease_version=Job.lease_version + 1)
        )
        await s.commit()


async def _state(maker: async_sessionmaker[AsyncSession], task_id: str, job_id: int) -> tuple[Task, Job, list[str]]:
    async with maker() as s:
        task = await TaskRepository(s).get_task(task_id)
        job = await JobRepository(s).get_job(job_id)
        rows = (
            (await s.execute(select(OutboxEvent).where(OutboxEvent.aggregate_id == task_id).order_by(OutboxEvent.id)))
            .scalars()
            .all()
        )
    assert task is not None and job is not None
    return task, job, [r.payload.get("event") for r in rows]


def _run(maker: async_sessionmaker[AsyncSession], execute: Any, heartbeat: float) -> Any:
    return (
        patch("app.worker.async_session_maker", maker),
        patch("app.services.task_service.TaskService.execute_task", new=execute),
        patch("app.worker.HEARTBEAT_INTERVAL_SECONDS", heartbeat),
    )


def _long_execution(trail: list[str], before: Any = None) -> Any:
    async def execute(self: Any, request: Any, task_id: str, **kwargs: Any) -> TaskResult:
        if before is not None:
            await before()
        trail.append("started")
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            trail.append("cancelled")
            raise
        trail.append("finished")
        return _success(task_id)

    return execute


@pytest.mark.asyncio
async def test_reclaimed_job_cancels_execution_and_writes_nothing(session: AsyncSession) -> None:
    maker = _maker(session)
    task_id, job_id = await _github_job(session)
    trail: list[str] = []

    p1, p2, p3 = _run(
        maker, _long_execution(trail, lambda: _reclaim_by_other_worker(maker, job_id)), FAST_HEARTBEAT
    )
    with p1, p2, p3:
        await asyncio.wait_for(process_job(job_id), timeout=15)

    assert trail == ["started", "cancelled"]
    task, job, events = await _state(maker, task_id, job_id)
    # Stale worker wrote nothing after losing the lease.
    assert task.status == TaskStatusEnum.TESTING.value
    assert task.task_result is None and task.failure_reason is None
    # The new owner's row is untouched.
    assert job.worker_id == OTHER_WORKER
    assert job.status == JobStatusEnum.RUNNING.value
    assert job.completed_at is None and job.last_error is None
    assert events == ["JOB_STARTED"]


@pytest.mark.asyncio
async def test_renewal_failure_never_finalizes_even_if_row_still_names_this_worker(
    session: AsyncSession,
) -> None:
    """Heartbeat says the lease is gone; the generic error path must not fail the task."""
    maker = _maker(session)
    task_id, job_id = await _github_job(session)
    trail: list[str] = []

    async def requeue_without_lease_bump() -> None:
        async with maker() as s:
            await s.execute(
                update(Job).where(Job.id == job_id).values(status=JobStatusEnum.PENDING.value)
            )
            await s.commit()

    p1, p2, p3 = _run(maker, _long_execution(trail, requeue_without_lease_bump), FAST_HEARTBEAT)
    with p1, p2, p3:
        await asyncio.wait_for(process_job(job_id), timeout=15)

    assert trail == ["started", "cancelled"]
    task, job, events = await _state(maker, task_id, job_id)
    assert task.status == TaskStatusEnum.TESTING.value
    assert task.failure_reason is None
    assert job.status == JobStatusEnum.PENDING.value
    assert job.worker_id == settings.task_worker_id
    assert job.last_error is None and job.completed_at is None
    assert events == ["JOB_STARTED"]


@pytest.mark.asyncio
async def test_stale_result_returned_after_reclaim_is_rejected(session: AsyncSession) -> None:
    """Execution finishes before the heartbeat notices: the finalize fence still holds."""
    maker = _maker(session)
    task_id, job_id = await _github_job(session)

    async def execute(self: Any, request: Any, tid: str, **kwargs: Any) -> TaskResult:
        await _reclaim_by_other_worker(maker, job_id)
        return _success(tid)

    p1, p2, p3 = _run(maker, execute, 60)
    with p1, p2, p3:
        await asyncio.wait_for(process_job(job_id), timeout=15)

    task, job, events = await _state(maker, task_id, job_id)
    assert task.status == TaskStatusEnum.TESTING.value
    assert task.task_result is None
    assert job.worker_id == OTHER_WORKER and job.status == JobStatusEnum.RUNNING.value
    assert events == ["JOB_STARTED"]


@pytest.mark.asyncio
async def test_plan_only_execution_is_cancelled_on_lease_loss(session: AsyncSession) -> None:
    maker = _maker(session)
    task_id, job_id = await _github_job(session, approval_config={"require_plan_approval": True})
    trail: list[str] = []

    p1, p2, p3 = _run(
        maker, _long_execution(trail, lambda: _reclaim_by_other_worker(maker, job_id)), FAST_HEARTBEAT
    )
    with p1, p2, p3:
        await asyncio.wait_for(process_job(job_id), timeout=15)

    assert trail == ["started", "cancelled"]
    task, job, events = await _state(maker, task_id, job_id)
    assert task.status == TaskStatusEnum.PLANNING.value
    assert task.implementation_plan is None
    assert events == ["JOB_STARTED"]
    async with maker() as s:
        approvals = (await s.execute(select(TaskApproval).where(TaskApproval.task_id == task_id))).scalars().all()
    assert approvals == []


@pytest.mark.asyncio
async def test_owned_job_completes_normally_while_heartbeat_renews(session: AsyncSession) -> None:
    maker = _maker(session)
    task_id, job_id = await _github_job(session)

    async def execute(self: Any, request: Any, tid: str, **kwargs: Any) -> TaskResult:
        await asyncio.sleep(0.4)  # several heartbeat renewals happen meanwhile
        return _success(tid)

    p1, p2, p3 = _run(maker, execute, FAST_HEARTBEAT)
    with p1, p2, p3:
        await asyncio.wait_for(process_job(job_id), timeout=15)

    task, job, events = await _state(maker, task_id, job_id)
    assert task.status == TaskStatusEnum.COMPLETED.value
    assert job.status == JobStatusEnum.SUCCEEDED.value
    assert events == ["JOB_STARTED", "JOB_COMPLETED"]


@pytest.mark.asyncio
async def test_next_job_runs_normally_after_a_lease_loss(session: AsyncSession) -> None:
    maker = _maker(session)
    lost_task, lost_job = await _github_job(session)
    next_task, next_job = await _github_job(session)
    trail: list[str] = []
    long_execute = _long_execution(trail, lambda: _reclaim_by_other_worker(maker, lost_job))

    async def execute(self: Any, request: Any, tid: str, **kwargs: Any) -> TaskResult:
        if tid == lost_task:
            return await long_execute(self, request, tid, **kwargs)
        await asyncio.sleep(0.2)
        return _success(tid)

    p1, p2, p3 = _run(maker, execute, FAST_HEARTBEAT)
    with p1, p2, p3:
        await asyncio.wait_for(process_job(lost_job), timeout=15)
        await asyncio.wait_for(process_job(next_job), timeout=15)

    assert trail == ["started", "cancelled"]
    task, job, events = await _state(maker, next_task, next_job)
    assert task.status == TaskStatusEnum.COMPLETED.value
    assert job.status == JobStatusEnum.SUCCEEDED.value
    assert events == ["JOB_STARTED", "JOB_COMPLETED"]
