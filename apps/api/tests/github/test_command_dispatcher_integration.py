"""Integration tests for Outbox Dispatcher with GitHub Command events (Phase 10B.4.1).

Tests the integration between:
- outbox_dispatcher.dispatch_batch
- GitHubCommandConsumer
- OutboxRepository
- QueueService (Redis enqueue)
- Full lifecycle: GITHUB_COMMAND_INGESTED -> Task + Job + JOB_CREATED -> Queue enqueue
"""

from __future__ import annotations

import os
import random
import uuid
from datetime import UTC, datetime
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
from app.github.command_parser import CodeForgeCommand, CodeForgeCommandType
from app.github.webhook_models import CodeForgeCommandEvent
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
async def test_user(session: AsyncSession):
    user_repo = UserRepository(session)
    gh_id = random.randint(1_000_000, 999_999_999)
    login = f"user-{uuid.uuid4().hex[:6]}"
    user, identity = await user_repo.upsert_github_user(
        github_user_id=gh_id,
        github_login=login,
        display_name="Dispatcher Integration User",
    )
    await session.commit()
    return user, identity


def build_command_payload(
    actor_id: int,
    actor_login: str,
    command_name: str = "fix",
    arguments: str = "memory leak in consumer",
) -> dict:
    repo_id = random.randint(1_000_000, 999_999_999)
    c_id = random.randint(1_000_000, 999_999_999)
    del_id = f"del-{uuid.uuid4().hex[:8]}"

    event = CodeForgeCommandEvent(
        delivery_id=del_id,
        repository="codeforge-org/codeforge-repo",
        repository_id=repo_id,
        repository_owner="codeforge-org",
        repository_name="codeforge-repo",
        installation_id=12345,
        actor_github_id=actor_id,
        actor_login=actor_login,
        issue_number=17,
        issue_title="High memory usage during batch processing",
        issue_html_url="https://github.com/codeforge-org/codeforge-repo/issues/17",
        is_pull_request=False,
        comment_id=c_id,
        command=CodeForgeCommand(
            name=CodeForgeCommandType(command_name),
            arguments=arguments,
            normalized_text=f"@codeforge {command_name} {arguments}",
        ),
        received_at=datetime.now(UTC),
        ingestion_status="accepted",
    )
    return event.model_dump(mode="json")


@pytest.mark.asyncio
async def test_dispatch_batch_full_lifecycle_to_queue(session: AsyncSession, test_user):
    """Verifies end-to-end lifecycle:

    1. Ingested command event is picked up by dispatch_batch.
    2. Task & Job are created; JOB_CREATED event is emitted.
    3. Next dispatch_batch run consumes JOB_CREATED and enqueues job to Redis.
    """
    user, identity = test_user
    payload = build_command_payload(
        actor_id=identity.github_user_id,
        actor_login=identity.github_login,
    )

    outbox_repo = OutboxRepository(session)
    ingested_event = OutboxEvent(
        event_type="GITHUB_COMMAND_INGESTED",
        aggregate_id=payload["delivery_id"],
        payload=payload,
    )
    await outbox_repo.create_event(ingested_event)
    await session.commit()

    mock_queue = AsyncMock()

    # Step 1: dispatch_batch processes GITHUB_COMMAND_INGESTED
    count1 = await dispatch_batch(limit=10, session=session, queue=mock_queue)
    assert count1 == 1

    # Verify Task was created
    task_repo = TaskRepository(session)
    task = await task_repo.get_task_by_github_comment(payload["repository_id"], payload["comment_id"])
    assert task is not None
    assert task.status == TaskStatusEnum.PENDING.value
    assert task.creator_id == user.id

    # Verify GITHUB_COMMAND_INGESTED event is now published
    ref_ingested = await outbox_repo.get_event(ingested_event.id)
    assert ref_ingested is not None
    assert ref_ingested.published_at is not None

    # Step 2: dispatch_batch processes the emitted JOB_CREATED event
    count2 = await dispatch_batch(limit=10, session=session, queue=mock_queue)
    assert count2 == 1

    # Verify mock_queue.enqueue was called with the job's ID
    mock_queue.enqueue.assert_called_once()
    enqueued_job_id = mock_queue.enqueue.call_args[0][0]

    job_repo = JobRepository(session)
    job = await job_repo.get_job_by_task(task.task_id)
    assert job is not None
    assert enqueued_job_id == job.id


@pytest.mark.asyncio
async def test_dispatch_batch_mixed_events_processed_cleanly(session: AsyncSession, test_user):
    user, identity = test_user
    outbox_repo = OutboxRepository(session)

    # Add a GITHUB_COMMAND_INGESTED event
    payload = build_command_payload(
        actor_id=identity.github_user_id,
        actor_login=identity.github_login,
    )
    event1 = OutboxEvent(
        event_type="GITHUB_COMMAND_INGESTED",
        aggregate_id=payload["delivery_id"],
        payload=payload,
    )
    await outbox_repo.create_event(event1)

    # Add an independent JOB_CREATED event
    event2 = OutboxEvent(
        event_type="JOB_CREATED",
        aggregate_id="job-9999",
        payload={"job_id": 9999},
    )
    await outbox_repo.create_event(event2)
    await session.commit()

    mock_queue = AsyncMock()
    processed_count = await dispatch_batch(limit=10, session=session, queue=mock_queue)
    assert processed_count == 2

    # Verify queue enqueued job 9999
    mock_queue.enqueue.assert_called_with(9999)

    # Verify both events are published
    ref1 = await outbox_repo.get_event(event1.id)
    ref2 = await outbox_repo.get_event(event2.id)
    assert ref1.published_at is not None
    assert ref2.published_at is not None


@pytest.mark.asyncio
async def test_dispatch_batch_handles_terminal_failure_safely(session: AsyncSession):
    outbox_repo = OutboxRepository(session)
    bad_event = OutboxEvent(
        event_type="GITHUB_COMMAND_INGESTED",
        aggregate_id="del-bad-batch",
        payload={"corrupt": "data"},
    )
    await outbox_repo.create_event(bad_event)
    await session.commit()

    mock_queue = AsyncMock()
    # Should handle error gracefully without raising
    count = await dispatch_batch(limit=10, session=session, queue=mock_queue)
    assert count == 1

    ref = await outbox_repo.get_event(bad_event.id)
    assert ref is not None
    assert ref.published_at is not None
    assert ref.payload.get("terminal_failure") is True
