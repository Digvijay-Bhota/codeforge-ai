"""Integration tests for Outbox Dispatcher with GitHub Status events (Phase 10B.4.2).

Tests the integration between:
- outbox_dispatcher.dispatch_batch
- GitHubStatusConsumer
- OutboxRepository
- TaskGitHubLink persistence
- Full lifecycle status dispatching via outbox
"""

from __future__ import annotations

import os
import random
import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import (
    Job,
    OutboxEvent,
    Task,
    TaskGitHubLink,
    TaskStatusEnum,
)
from app.db.repositories.job_repository import JobRepository
from app.db.repositories.outbox_repository import OutboxRepository
from app.db.repositories.task_repository import TaskRepository
from app.db.repositories.user_repository import UserRepository
from app.github.exceptions import GitHubUpstreamError
from app.github.models import GitHubCheckRun, GitHubComment
from app.outbox_dispatcher import dispatch_batch

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    os.getenv("DATABASE_URL", "postgresql+asyncpg://codeforge:codeforge@localhost:5433/codeforge"),
)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(TEST_DB_URL, echo=False, poolclass=__import__("sqlalchemy").pool.NullPool)
    TestingSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with TestingSessionLocal() as session:
        await session.execute(delete(TaskGitHubLink))
        await session.execute(delete(OutboxEvent))
        await session.execute(delete(Job))
        await session.execute(delete(Task))
        await session.commit()
        yield session
        await session.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def sample_pr_setup(session: AsyncSession):
    user_repo = UserRepository(session)
    gh_id = random.randint(1_000_000, 999_999_999)
    login = f"user-{uuid.uuid4().hex[:6]}"
    user, identity = await user_repo.upsert_github_user(
        github_user_id=gh_id,
        github_login=login,
        display_name="Dispatcher Status User",
    )

    task_id = str(uuid.uuid4())
    repo_id = random.randint(1_000_000, 999_999_999)
    comment_id = random.randint(1_000_000, 999_999_999)
    sha = "abcdef1234567890abcdef1234567890abcdef12"

    task = Task(
        task_id=task_id,
        status=TaskStatusEnum.PENDING.value,
        execution_target="github",
        repository="statusorg/statusrepo",
        requested_task="[github-command:fix] dispatcher integration",
        creator_id=user.id,
        pr_metadata={"pr_number": 99, "head_sha": sha},
    )
    task_repo = TaskRepository(session)
    await task_repo.create_task(task)

    job = Job(
        task_id=task.task_id,
        status="PENDING",
    )
    job_repo = JobRepository(session)
    await job_repo.create_job(job)

    link = TaskGitHubLink(
        task_id=task.task_id,
        installation_id=12345,
        repository_id=repo_id,
        repository_full_name="statusorg/statusrepo",
        issue_number=99,
        pull_request_number=99,
        trigger_comment_id=comment_id,
        triggering_github_user_id=gh_id,
        triggering_github_login=login,
        head_sha=sha,
    )
    await user_repo.create_task_github_link(link)
    await session.commit()

    return task, job, link


@pytest.mark.asyncio
async def test_dispatch_batch_processes_github_status_update(session: AsyncSession, sample_pr_setup):
    task, job, link = sample_pr_setup
    task_id = task.task_id

    outbox_repo = OutboxRepository(session)
    event = OutboxEvent(
        event_type="GITHUB_STATUS_UPDATE",
        aggregate_id=task_id,
        payload={
            "task_id": task_id,
            "job_id": job.id,
            "job_status": "PENDING",
            "event": "COMMAND_ACCEPTED",
            "command_name": "fix",
            "arguments": "dispatcher integration",
        },
    )
    await outbox_repo.create_event(event)
    await session.commit()

    mock_client = AsyncMock()
    mock_client.upsert_issue_comment.return_value = GitHubComment(
        id=888,
        body="<!-- codeforge:managed-comment:ack -->\n### CodeForge AI Command Accepted",
        html_url="https://github.com/statusorg/statusrepo/issues/99#issuecomment-888",
        created_at="2026-09-14T10:00:00Z",
        updated_at="2026-09-14T10:00:00Z",
    )
    mock_client.create_check_run.return_value = GitHubCheckRun(
        id=999,
        name="CodeForge AI",
        head_sha=link.head_sha,
        status="queued",
        conclusion=None,
        html_url="https://github.com/statusorg/statusrepo/runs/999",
    )

    mock_queue = AsyncMock()
    count = await dispatch_batch(limit=10, session=session, queue=mock_queue, github_client=mock_client)
    assert count == 1

    mock_client.upsert_issue_comment.assert_called_once()
    mock_client.create_check_run.assert_called_once()

    ref = await outbox_repo.get_event(event.id)
    assert ref is not None
    assert ref.published_at is not None

    user_repo = UserRepository(session)
    updated_link = await user_repo.get_task_github_link(task_id)
    assert updated_link is not None
    assert updated_link.check_run_id == 999
    assert updated_link.check_run_status == "queued"
    assert updated_link.acknowledgement_comment_id == 888


@pytest.mark.asyncio
async def test_dispatch_batch_handles_retryable_status_update_failure(session: AsyncSession, sample_pr_setup):
    task, job, link = sample_pr_setup
    task_id = task.task_id

    outbox_repo = OutboxRepository(session)
    event = OutboxEvent(
        event_type="GITHUB_STATUS_UPDATE",
        aggregate_id=task_id,
        payload={
            "task_id": task_id,
            "job_id": job.id,
            "job_status": "PENDING",
            "event": "COMMAND_ACCEPTED",
        },
    )
    await outbox_repo.create_event(event)
    await session.commit()

    mock_client = AsyncMock()
    mock_client.upsert_issue_comment.side_effect = GitHubUpstreamError("503 Service Unavailable", status_code=503, retryable=True)

    mock_queue = AsyncMock()
    count = await dispatch_batch(limit=10, session=session, queue=mock_queue, github_client=mock_client)
    assert count == 1

    ref = await outbox_repo.get_event(event.id)
    assert ref is not None
    assert ref.published_at is None
    assert ref.payload.get("attempts") == 1
    assert "503 Service Unavailable" in ref.payload.get("last_error", "")


@pytest.mark.asyncio
async def test_dispatch_batch_updates_check_run_on_job_transitions(session: AsyncSession, sample_pr_setup):
    task, job, link = sample_pr_setup
    task_id = task.task_id

    # Existing link has check run created
    link.check_run_id = 999
    link.check_run_status = "queued"
    link.acknowledgement_comment_id = 888
    session.add(link)
    await session.commit()

    outbox_repo = OutboxRepository(session)
    event_running = OutboxEvent(
        event_type="GITHUB_STATUS_UPDATE",
        aggregate_id=task_id,
        payload={
            "task_id": task_id,
            "job_id": job.id,
            "job_status": "RUNNING",
            "event": "JOB_STARTED",
        },
    )
    event_succeeded = OutboxEvent(
        event_type="GITHUB_STATUS_UPDATE",
        aggregate_id=task_id,
        payload={
            "task_id": task_id,
            "job_id": job.id,
            "job_status": "SUCCEEDED",
            "event": "JOB_COMPLETED",
        },
    )
    await outbox_repo.create_event(event_running)
    await outbox_repo.create_event(event_succeeded)
    await session.commit()

    mock_client = AsyncMock()
    mock_client.update_check_run.return_value = GitHubCheckRun(
        id=999,
        name="CodeForge AI",
        head_sha=link.head_sha,
        status="in_progress",
        conclusion=None,
        html_url="https://github.com/statusorg/statusrepo/runs/999",
    )

    mock_queue = AsyncMock()
    count = await dispatch_batch(limit=10, session=session, queue=mock_queue, github_client=mock_client)
    assert count == 2

    assert mock_client.update_check_run.call_count == 2
    # Verify final update was completed/success
    last_req = mock_client.update_check_run.call_args[0][0]
    assert last_req.status == "completed"
    assert last_req.conclusion == "success"

    user_repo = UserRepository(session)
    final_link = await user_repo.get_task_github_link(task_id)
    assert final_link.check_run_status == "completed"
    assert final_link.check_run_conclusion == "success"
