"""Every GitHub task's plan must be approved before any code is written.

A task started by an @codeforge comment (or through the API, or written
directly to the database) first runs a planning-only job, then waits in
WAITING_APPROVAL.  Only an APPROVED plan approval lets a job generate code,
and only if a permitted test executor exists: otherwise the task stops with
EXECUTION_UNAVAILABLE before anything runs.  Approving a plan never approves
publication, which keeps its own request (CF-05).

The worker enforces this, not only task creation: it treats every GitHub task
as needing plan approval, whatever its approval_config says.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    PUBLICATION_APPROVAL,
    Job,
    JobStatusEnum,
    OutboxEvent,
    Task,
    TaskApproval,
    TaskStatusEnum,
)
from app.db.repositories.outbox_repository import OutboxRepository
from app.db.repositories.user_repository import UserRepository
from app.github.command_consumer import GitHubCommandConsumer
from app.schemas.task import ExecutionTarget, TaskRequest, TaskResult
from app.services.task_service import PlanApprovalExpiredError, TaskService
from app.worker import EXECUTION_UNAVAILABLE, process_job
from tests.github.test_command_consumer import make_command_event_payload
from tests.security.test_publication_approval import generation_result, make_artifact
from tests.test_phase10a_approvals import _sample_plan

REPO = "test-org/test-repo"


@pytest.fixture
def maker(session: AsyncSession) -> Any:
    m = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
    with patch("app.worker.async_session_maker", m):
        yield m


def plan_result(task_id: str) -> TaskResult:
    return generation_result(task_id, None).model_copy(
        update={"full_plan": _sample_plan().model_dump(), "agent_output": "Plan ready."}
    )


def executor_available() -> Any:
    return patch("app.execution.test_executor.get_test_executor", return_value=object())


class Runs:
    """execute_task stand-in: records each call's stage, returns a plan or changes."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.stages: list[str] = []

    async def run(self, request: Any, task_id: str, **kwargs: Any) -> TaskResult:
        stop_after_plan = kwargs.get("stop_after_plan", False)
        self.stages.append("plan" if stop_after_plan else "generate")
        if stop_after_plan:
            return plan_result(task_id)
        return generation_result(task_id, make_artifact(REPO))

    def patch(self) -> Any:
        return patch("app.services.task_service.TaskService.execute_task", new=AsyncMock(side_effect=self.run))


async def snapshot(maker: Any, task_id: str) -> dict[str, Any]:
    async with maker() as s:
        task = (await s.execute(select(Task).where(Task.task_id == task_id))).scalar_one()
        jobs = (await s.execute(select(Job).where(Job.task_id == task_id).order_by(Job.id))).scalars().all()
        approvals = (await s.execute(select(TaskApproval).where(TaskApproval.task_id == task_id)
                                     .order_by(TaskApproval.id))).scalars().all()  # fmt: skip
        return {
            "status": task.status,
            "failure_code": task.failure_code,
            "failure_reason": task.failure_reason,
            "approval_config": task.approval_config,
            "jobs": [(j.id, j.status) for j in jobs],
            "approvals": [(a.approval_type, a.status) for a in approvals],
        }


async def pending_jobs(maker: Any, task_id: str) -> list[int]:
    async with maker() as s:
        return list((await s.execute(select(Job.id).where(
            Job.task_id == task_id, Job.status == JobStatusEnum.PENDING.value))).scalars())  # fmt: skip


async def run_jobs(maker: Any, task_id: str) -> None:
    for job_id in await pending_jobs(maker, task_id):
        await process_job(job_id)


async def comment_task(session: AsyncSession) -> str:
    """A task created by the real comment consumer from an @codeforge command."""
    _, identity = await UserRepository(session).upsert_github_user(
        github_user_id=random.randint(1_000_000, 999_999_999), github_login=f"dev-{uuid.uuid4().hex[:6]}",
        display_name="Dev",
    )  # fmt: skip
    payload = make_command_event_payload(actor_id=identity.github_user_id, actor_login=identity.github_login)
    event = OutboxEvent(event_type="GITHUB_COMMAND_INGESTED", aggregate_id=payload["delivery_id"], payload=payload)
    await OutboxRepository(session).create_event(event)
    await session.commit()
    task = await GitHubCommandConsumer(session).process_event(event)
    assert task is not None
    return task.task_id


async def waiting_for_approval(session: AsyncSession, maker: Any) -> tuple[str, Runs]:
    task_id = await comment_task(session)
    runs = Runs(task_id)
    with runs.patch():
        await run_jobs(maker, task_id)
    return task_id, runs


async def approve(maker: Any, task_id: str) -> None:
    async with maker() as s:
        await TaskService(s).approve_task(task_id, approver="reviewer")
        await s.commit()


# ── Planning first, then wait ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_comment_task_plans_only_and_waits_for_approval(session: AsyncSession, maker: Any) -> None:
    task_id, runs = await waiting_for_approval(session, maker)
    st = await snapshot(maker, task_id)
    assert st["approval_config"] == {"require_plan_approval": True}
    assert runs.stages == ["plan"]  # no code generated
    assert st["status"] == TaskStatusEnum.WAITING_APPROVAL.value
    assert st["approvals"] == [("plan", "PENDING")]
    assert await pending_jobs(maker, task_id) == []  # nothing queued to run


@pytest.mark.parametrize("approval_config", [None, {"require_plan_approval": False}])
@pytest.mark.asyncio
async def test_a_github_task_written_around_the_api_still_only_plans(
    session: AsyncSession, maker: Any, approval_config: Any
) -> None:
    # The worker, not task creation, is the authority.
    task = Task(task_id=str(uuid.uuid4()), status="PENDING", execution_target="github", repository=REPO,
                requested_task="Fix the widget calculation", approval_config=approval_config)  # fmt: skip
    session.add(task)
    await session.flush()
    session.add(Job(task_id=task.task_id, status=JobStatusEnum.PENDING.value))
    await session.commit()
    runs = Runs(task.task_id)
    with runs.patch(), executor_available():
        await run_jobs(maker, task.task_id)
    assert runs.stages == ["plan"]
    assert (await snapshot(maker, task.task_id))["status"] == TaskStatusEnum.WAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_a_queued_task_with_an_unapproved_plan_only_plans_again(session: AsyncSession, maker: Any) -> None:
    # Queued with a stored plan, but its approval request is still PENDING (a
    # state only reachable around the API): only an APPROVED plan resumes.
    task = Task(task_id=str(uuid.uuid4()), status="QUEUED", execution_target="github", repository=REPO,
                requested_task="Fix the widget calculation", implementation_plan=_sample_plan().model_dump(),
                approval_config={"require_plan_approval": True})  # fmt: skip
    session.add(task)
    await session.flush()
    session.add(TaskApproval(task_id=task.task_id, approval_type="plan", status="PENDING", requested_by="worker"))
    session.add(Job(task_id=task.task_id, status=JobStatusEnum.PENDING.value))
    await session.commit()
    runs = Runs(task.task_id)
    with runs.patch(), executor_available():
        await run_jobs(maker, task.task_id)
    assert runs.stages == ["plan"]
    assert (await snapshot(maker, task.task_id))["status"] == TaskStatusEnum.WAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_a_github_task_request_cannot_opt_out_of_plan_approval(session: AsyncSession) -> None:
    task = await TaskService(session).create_task(
        TaskRequest(execution_target=ExecutionTarget.github, github_repository=REPO, description="Fix the widget calculation",
                    require_plan_approval=False, approval_config={"timeout_hours": 2}),  # fmt: skip
    )
    assert task.approval_config == {"timeout_hours": 2, "require_plan_approval": True}


@pytest.mark.asyncio
async def test_redelivering_the_planning_job_runs_nothing_more(session: AsyncSession, maker: Any) -> None:
    task_id, runs = await waiting_for_approval(session, maker)
    [(planning_job, _)] = (await snapshot(maker, task_id))["jobs"]
    with runs.patch(), executor_available():
        await process_job(planning_job)  # a duplicate queue message
    assert runs.stages == ["plan"]
    assert (await snapshot(maker, task_id))["status"] == TaskStatusEnum.WAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_a_stray_job_for_a_waiting_task_never_generates_code(session: AsyncSession, maker: Any) -> None:
    task_id, runs = await waiting_for_approval(session, maker)
    async with maker() as s:
        s.add(Job(task_id=task_id, status=JobStatusEnum.PENDING.value))
        await s.commit()
    with runs.patch(), executor_available():
        await run_jobs(maker, task_id)
    assert runs.stages == ["plan"]
    st = await snapshot(maker, task_id)
    assert st["status"] != TaskStatusEnum.CODING.value
    assert all(kind != PUBLICATION_APPROVAL for kind, _ in st["approvals"])


# ── After approval ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_without_a_test_executor_an_approved_plan_stops_before_any_code(session: AsyncSession, maker: Any) -> None:
    # CODEFORGE_TEST_EXECUTION_MODE defaults to disabled.
    task_id, runs = await waiting_for_approval(session, maker)
    await approve(maker, task_id)
    with runs.patch():
        await run_jobs(maker, task_id)
    st = await snapshot(maker, task_id)
    assert runs.stages == ["plan"]  # the agent never ran again
    assert st["status"] == TaskStatusEnum.FAILED.value
    assert st["failure_code"] == EXECUTION_UNAVAILABLE
    assert "CODEFORGE_TEST_EXECUTION_MODE=disabled" in st["failure_reason"]
    assert "Nothing was run" in st["failure_reason"]
    assert st["approvals"] == [("plan", "APPROVED")]  # no publication request
    assert [status for _, status in st["jobs"]] == ["SUCCEEDED", "FAILED"]


@pytest.mark.asyncio
async def test_an_approved_plan_generates_once_and_still_waits_for_publication(
    session: AsyncSession, maker: Any
) -> None:
    task_id, runs = await waiting_for_approval(session, maker)
    await approve(maker, task_id)
    [generation_job] = await pending_jobs(maker, task_id)
    with runs.patch(), executor_available():
        await process_job(generation_job)
        await process_job(generation_job)  # redelivered after it finished
    st = await snapshot(maker, task_id)
    assert runs.stages == ["plan", "generate"]
    assert st["status"] == TaskStatusEnum.WAITING_PUBLICATION.value
    # Plan approved; publication only requested, never approved by the plan decision.
    assert st["approvals"] == [("plan", "APPROVED"), (PUBLICATION_APPROVAL, "PENDING")]


@pytest.mark.asyncio
async def test_concurrent_approvals_queue_one_generation(session: AsyncSession, maker: Any) -> None:
    task_id, runs = await waiting_for_approval(session, maker)

    async def attempt() -> str:
        try:
            await approve(maker, task_id)
            return "ok"
        except HTTPException as exc:
            return str(exc.status_code)

    results = await asyncio.gather(attempt(), attempt(), attempt())
    assert "ok" in results
    assert len(await pending_jobs(maker, task_id)) == 1
    with runs.patch(), executor_available():
        await run_jobs(maker, task_id)
    assert runs.stages == ["plan", "generate"]


@pytest.mark.parametrize("decision", ["reject", "expire"])
@pytest.mark.asyncio
async def test_rejected_or_expired_plans_never_run(session: AsyncSession, maker: Any, decision: str) -> None:
    task_id, runs = await waiting_for_approval(session, maker)
    async with maker() as s:
        if decision == "reject":
            await TaskService(s).reject_task(task_id, rejecter="reviewer", reason="no")
        else:
            approval = (await s.execute(select(TaskApproval).where(TaskApproval.task_id == task_id))).scalar_one()
            approval.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await s.commit()
    if decision == "expire":
        async with maker() as s:
            with pytest.raises(PlanApprovalExpiredError) as exc_info:
                await TaskService(s).approve_task(task_id, approver="reviewer")
            await s.commit()  # as the approve route does: the expiry is recorded
        assert exc_info.value.status_code == 409
    async with maker() as s:  # even a job queued for it anyway
        s.add(Job(task_id=task_id, status=JobStatusEnum.PENDING.value))
        await s.commit()
    with runs.patch(), executor_available():
        await run_jobs(maker, task_id)
    st = await snapshot(maker, task_id)
    assert runs.stages == ["plan"]
    assert st["status"] == TaskStatusEnum.CANCELLED.value


@pytest.mark.asyncio
async def test_retrying_a_rejected_task_plans_again_and_waits(session: AsyncSession, maker: Any) -> None:
    task_id, _ = await waiting_for_approval(session, maker)
    async with maker() as s:
        await TaskService(s).reject_task(task_id, rejecter="reviewer")
        await s.commit()
    async with maker() as s:
        child = await TaskService(s).retry_task(task_id)
        await s.commit()
        child_id = child.task_id
    runs = Runs(child_id)
    with runs.patch(), executor_available():
        await run_jobs(maker, child_id)
    assert runs.stages == ["plan"]
    assert (await snapshot(maker, child_id))["status"] == TaskStatusEnum.WAITING_APPROVAL.value
    assert (await snapshot(maker, task_id))["status"] == TaskStatusEnum.CANCELLED.value  # not revived
