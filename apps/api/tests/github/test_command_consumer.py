"""Unit and integration tests for GitHubCommandConsumer (Phase 10B.4.1).

Tests cover:
1. Happy path:
   - Valid GITHUB_COMMAND_INGESTED event creates exactly one Task and Job.
   - Task enters the existing job/dispatch lifecycle (Job status PENDING, OutboxEvent JOB_CREATED).
   - Pull request commands preserve PR metadata (pr_number, html_url).
   - All supported commands (fix, implement, review, explain, analyze) are processed.
2. Identity:
   - GitHub actor resolves to correct CodeForge User.
   - Fallback resolution by github_login.
   - Unresolvable actor identity is rejected safely (terminal, audited, marked published with error).
   - Inactive user is rejected safely.
3. GitHub linkage:
   - Task is linked to originating repository, comment, issue, and installation via TaskGitHubLink.
4. Idempotency:
   - Processing the same event twice returns existing task and does not create duplicate tasks.
   - Separate outbox events for the same trigger comment converge safely on one task.
   - Concurrent execution collision on database unique constraint converges safely.
5. Failure & Retries:
   - Malformed payload is rejected safely as terminal failure without looping.
   - Transient failure leaves event retryable with incremented attempt count.
   - Exhausted retries dead-letter the event safely.
6. Worker Behavior:
   - Bounded batch processing.
   - Already-processed events are safely skipped.
   - No agent execution, git clone, or branch creation is performed.
"""

from __future__ import annotations

import os
import random
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import (
    AuditEvent,
    GitHubIdentity,
    Job,
    JobStatusEnum,
    OutboxEvent,
    Task,
    TaskGitHubLink,
    TaskStatusEnum,
    User,
)
from app.db.repositories.job_repository import JobRepository
from app.db.repositories.outbox_repository import OutboxRepository
from app.db.repositories.task_repository import TaskRepository
from app.db.repositories.user_repository import UserRepository
from app.github.command_consumer import GitHubCommandConsumer
from app.github.command_parser import CodeForgeCommand, CodeForgeCommandType
from app.github.exceptions import (
    InactiveUserError,
    MalformedCommandEventError,
    UnresolvableIdentityError,
)
from app.github.webhook_models import CodeForgeCommandEvent

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    os.getenv("DATABASE_URL", "postgresql+asyncpg://codeforge:codeforge@localhost:5433/codeforge"),
)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(TEST_DB_URL, echo=False, poolclass=__import__("sqlalchemy").pool.NullPool)
    TestingSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with TestingSessionLocal() as session:
        from sqlalchemy import delete
        await session.execute(delete(TaskGitHubLink))
        await session.execute(delete(OutboxEvent))
        await session.execute(delete(Job))
        await session.execute(delete(Task))
        await session.commit()
        yield session
        await session.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def sample_user(session: AsyncSession) -> tuple[User, GitHubIdentity]:
    user_repo = UserRepository(session)
    gh_id = random.randint(1_000_000, 999_999_999)
    login = f"dev-{uuid.uuid4().hex[:6]}"
    user, identity = await user_repo.upsert_github_user(
        github_user_id=gh_id,
        github_login=login,
        display_name="Test Developer",
    )
    await session.commit()
    return user, identity


def make_command_event_payload(
    actor_id: int,
    actor_login: str,
    command_name: str = "fix",
    arguments: str | None = "solve auth race condition",
    repository_id: int | None = None,
    comment_id: int | None = None,
    issue_number: int = 42,
    is_pull_request: bool = False,
    delivery_id: str | None = None,
) -> dict:
    repo_id = repository_id or random.randint(1_000_000, 999_999_999)
    c_id = comment_id or random.randint(1_000_000, 999_999_999)
    del_id = delivery_id or f"del-{uuid.uuid4().hex[:8]}"

    event = CodeForgeCommandEvent(
        delivery_id=del_id,
        repository="test-org/test-repo",
        repository_id=repo_id,
        repository_owner="test-org",
        repository_name="test-repo",
        installation_id=98765,
        actor_github_id=actor_id,
        actor_login=actor_login,
        issue_number=issue_number,
        issue_title="Bug in authentication",
        issue_html_url=f"https://github.com/test-org/test-repo/issues/{issue_number}",
        is_pull_request=is_pull_request,
        comment_id=c_id,
        command=CodeForgeCommand(
            name=CodeForgeCommandType(command_name),
            arguments=arguments,
            normalized_text=f"@codeforge {command_name} {arguments or ''}".strip(),
        ),
        received_at=datetime.now(UTC),
        ingestion_status="accepted",
    )
    return event.model_dump(mode="json")


# ── Happy Path Tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_fix_command_creates_task_and_job(
    session: AsyncSession, sample_user: tuple[User, GitHubIdentity]
):
    user, identity = sample_user
    payload = make_command_event_payload(
        actor_id=identity.github_user_id,
        actor_login=identity.github_login,
        command_name="fix",
        arguments="resolve NPE in payment handler",
    )

    outbox_repo = OutboxRepository(session)
    outbox_event = OutboxEvent(
        event_type="GITHUB_COMMAND_INGESTED",
        aggregate_id=payload["delivery_id"],
        payload=payload,
    )
    await outbox_repo.create_event(outbox_event)
    await session.commit()

    consumer = GitHubCommandConsumer(session)
    task = await consumer.process_event(outbox_event)

    assert task is not None
    assert task.status == TaskStatusEnum.PENDING.value
    assert task.creator_id == user.id
    assert task.repository == "test-org/test-repo"
    assert task.execution_target == "github"
    assert "fix" in task.requested_task
    assert "resolve NPE in payment handler" in task.requested_task

    # Verify TaskGitHubLink
    user_repo = UserRepository(session)
    link = await user_repo.get_task_github_link(task.task_id)
    assert link is not None
    assert link.repository_id == payload["repository_id"]
    assert link.trigger_comment_id == payload["comment_id"]
    assert link.triggering_github_user_id == identity.github_user_id
    assert link.triggering_github_login == identity.github_login
    assert link.issue_number == 42
    assert link.pull_request_number is None

    # Verify downstream Job was created and is PENDING
    job_repo = JobRepository(session)
    job = await job_repo.get_job_by_task(task.task_id)
    assert job is not None
    assert job.status == JobStatusEnum.PENDING.value

    # Verify JOB_CREATED outbox event exists
    job_events = await session.execute(
        select(OutboxEvent).where(
            OutboxEvent.event_type == "JOB_CREATED",
            OutboxEvent.aggregate_id == str(job.id),
        )
    )
    job_event = job_events.scalars().first()
    assert job_event is not None
    assert job_event.payload["job_id"] == job.id

    # Verify original outbox event marked published
    refreshed_event = await outbox_repo.get_event(outbox_event.id)
    assert refreshed_event is not None
    assert refreshed_event.published_at is not None


@pytest.mark.asyncio
async def test_happy_path_pr_command_preserves_pr_metadata(
    session: AsyncSession, sample_user: tuple[User, GitHubIdentity]
):
    user, identity = sample_user
    payload = make_command_event_payload(
        actor_id=identity.github_user_id,
        actor_login=identity.github_login,
        command_name="review",
        arguments="check for performance regressions",
        issue_number=99,
        is_pull_request=True,
    )

    outbox_repo = OutboxRepository(session)
    outbox_event = OutboxEvent(
        event_type="GITHUB_COMMAND_INGESTED",
        aggregate_id=payload["delivery_id"],
        payload=payload,
    )
    await outbox_repo.create_event(outbox_event)
    await session.commit()

    consumer = GitHubCommandConsumer(session)
    task = await consumer.process_event(outbox_event)

    assert task is not None
    assert task.pr_metadata is not None
    assert task.pr_metadata["pr_number"] == 99

    user_repo = UserRepository(session)
    link = await user_repo.get_task_github_link(task.task_id)
    assert link is not None
    assert link.pull_request_number == 99


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd", ["implement", "review", "explain", "analyze"])
async def test_happy_path_all_supported_commands(
    session: AsyncSession, sample_user: tuple[User, GitHubIdentity], cmd: str
):
    user, identity = sample_user
    payload = make_command_event_payload(
        actor_id=identity.github_user_id,
        actor_login=identity.github_login,
        command_name=cmd,
        arguments=f"test command {cmd}",
    )

    outbox_repo = OutboxRepository(session)
    outbox_event = OutboxEvent(
        event_type="GITHUB_COMMAND_INGESTED",
        aggregate_id=payload["delivery_id"],
        payload=payload,
    )
    await outbox_repo.create_event(outbox_event)
    await session.commit()

    consumer = GitHubCommandConsumer(session)
    task = await consumer.process_event(outbox_event)
    assert task is not None
    assert cmd in task.requested_task


# ── Identity Resolution Tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_identity_actor_resolves_by_github_login_fallback(
    session: AsyncSession, sample_user: tuple[User, GitHubIdentity]
):
    user, identity = sample_user
    # ID is different but login matches
    payload = make_command_event_payload(
        actor_id=random.randint(1_000_000, 999_999_999),
        actor_login=identity.github_login,
        command_name="fix",
    )

    outbox_repo = OutboxRepository(session)
    outbox_event = OutboxEvent(
        event_type="GITHUB_COMMAND_INGESTED",
        aggregate_id=payload["delivery_id"],
        payload=payload,
    )
    await outbox_repo.create_event(outbox_event)
    await session.commit()

    consumer = GitHubCommandConsumer(session)
    task = await consumer.process_event(outbox_event)
    assert task is not None
    assert task.creator_id == user.id


@pytest.mark.asyncio
async def test_identity_unresolvable_actor_rejected_safely(session: AsyncSession):
    payload = make_command_event_payload(
        actor_id=999_999_999,
        actor_login="completely-unknown-user-404",
        command_name="fix",
    )

    outbox_repo = OutboxRepository(session)
    outbox_event = OutboxEvent(
        event_type="GITHUB_COMMAND_INGESTED",
        aggregate_id=payload["delivery_id"],
        payload=payload,
    )
    await outbox_repo.create_event(outbox_event)
    await session.commit()

    consumer = GitHubCommandConsumer(session)
    with pytest.raises(UnresolvableIdentityError):
        await consumer.process_event(outbox_event)

    # Verify event is marked published to prevent infinite retry loops
    refreshed = await outbox_repo.get_event(outbox_event.id)
    assert refreshed is not None
    assert refreshed.published_at is not None
    assert refreshed.payload["terminal_failure"] is True
    assert refreshed.payload["error_type"] == "UnresolvableIdentityError"

    # Verify AuditEvent was logged
    audit_events = await session.execute(
        select(AuditEvent).where(
            AuditEvent.event_type == "GITHUB_COMMAND_FAILED",
            AuditEvent.resource_id == str(outbox_event.id),
        )
    )
    audit = audit_events.scalars().first()
    assert audit is not None
    assert audit.result == "FAILED"


@pytest.mark.asyncio
async def test_identity_inactive_user_rejected_safely(
    session: AsyncSession, sample_user: tuple[User, GitHubIdentity]
):
    user, identity = sample_user
    user.is_active = False
    session.add(user)
    await session.commit()

    payload = make_command_event_payload(
        actor_id=identity.github_user_id,
        actor_login=identity.github_login,
        command_name="fix",
    )

    outbox_repo = OutboxRepository(session)
    outbox_event = OutboxEvent(
        event_type="GITHUB_COMMAND_INGESTED",
        aggregate_id=payload["delivery_id"],
        payload=payload,
    )
    await outbox_repo.create_event(outbox_event)
    await session.commit()

    consumer = GitHubCommandConsumer(session)
    with pytest.raises(InactiveUserError):
        await consumer.process_event(outbox_event)

    refreshed = await outbox_repo.get_event(outbox_event.id)
    assert refreshed is not None
    assert refreshed.published_at is not None
    assert refreshed.payload["terminal_failure"] is True
    assert refreshed.payload["error_type"] == "InactiveUserError"


# ── Idempotency Tests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_idempotency_same_event_processed_twice_returns_existing_task(
    session: AsyncSession, sample_user: tuple[User, GitHubIdentity]
):
    user, identity = sample_user
    payload = make_command_event_payload(
        actor_id=identity.github_user_id,
        actor_login=identity.github_login,
        command_name="fix",
    )

    outbox_repo = OutboxRepository(session)
    outbox_event = OutboxEvent(
        event_type="GITHUB_COMMAND_INGESTED",
        aggregate_id=payload["delivery_id"],
        payload=payload,
    )
    await outbox_repo.create_event(outbox_event)
    await session.commit()

    consumer = GitHubCommandConsumer(session)
    task1 = await consumer.process_event(outbox_event)
    assert task1 is not None

    # Reset published_at to simulate redelivery of the same event row
    outbox_event.published_at = None
    session.add(outbox_event)
    await session.commit()

    task2 = await consumer.process_event(outbox_event)
    assert task2 is not None
    assert task1.task_id == task2.task_id

    # Verify only 1 Task exists
    task_repo = TaskRepository(session)
    found_task = await task_repo.get_task_by_github_comment(
        payload["repository_id"], payload["comment_id"]
    )
    assert found_task is not None
    assert found_task.task_id == task1.task_id


@pytest.mark.asyncio
async def test_idempotency_duplicate_delivery_different_event_converges(
    session: AsyncSession, sample_user: tuple[User, GitHubIdentity]
):
    user, identity = sample_user
    repo_id = random.randint(1_000_000, 999_999_999)
    comment_id = random.randint(1_000_000, 999_999_999)

    payload1 = make_command_event_payload(
        actor_id=identity.github_user_id,
        actor_login=identity.github_login,
        repository_id=repo_id,
        comment_id=comment_id,
        delivery_id="delivery-original",
    )
    payload2 = make_command_event_payload(
        actor_id=identity.github_user_id,
        actor_login=identity.github_login,
        repository_id=repo_id,
        comment_id=comment_id,
        delivery_id="delivery-redelivery",
    )

    outbox_repo = OutboxRepository(session)
    event1 = OutboxEvent(
        event_type="GITHUB_COMMAND_INGESTED",
        aggregate_id="delivery-original",
        payload=payload1,
    )
    event2 = OutboxEvent(
        event_type="GITHUB_COMMAND_INGESTED",
        aggregate_id="delivery-redelivery",
        payload=payload2,
    )
    await outbox_repo.create_event(event1)
    await outbox_repo.create_event(event2)
    await session.commit()

    consumer = GitHubCommandConsumer(session)
    task1 = await consumer.process_event(event1)
    task2 = await consumer.process_event(event2)

    assert task1 is not None
    assert task2 is not None
    assert task1.task_id == task2.task_id

    # Both events marked published
    ref1 = await outbox_repo.get_event(event1.id)
    ref2 = await outbox_repo.get_event(event2.id)
    assert ref1.published_at is not None
    assert ref2.published_at is not None


@pytest.mark.asyncio
async def test_concurrency_race_condition_convergence(
    session: AsyncSession, sample_user: tuple[User, GitHubIdentity]
):
    """Simulates two concurrent workers where the unique constraint on TaskGitHubLink fires."""
    user, identity = sample_user
    payload = make_command_event_payload(
        actor_id=identity.github_user_id,
        actor_login=identity.github_login,
        command_name="fix",
    )

    outbox_repo = OutboxRepository(session)
    event = OutboxEvent(
        event_type="GITHUB_COMMAND_INGESTED",
        aggregate_id=payload["delivery_id"],
        payload=payload,
    )
    await outbox_repo.create_event(event)
    await session.commit()

    consumer = GitHubCommandConsumer(session)

    # First, run process_event to create the first link and task
    first_task = await consumer.process_event(event)
    assert first_task is not None

    # Now create another outbox event for the same comment
    event2 = OutboxEvent(
        event_type="GITHUB_COMMAND_INGESTED",
        aggregate_id=f"del-{uuid.uuid4().hex[:6]}",
        payload=payload,
    )
    await outbox_repo.create_event(event2)
    await session.commit()

    # Simulate race condition: bypass initial check so create_task_github_link raises IntegrityError
    call_count = 0
    orig_check = consumer.user_repo.get_task_github_link_by_comment

    async def mock_get_link(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None
        return await orig_check(*args, **kwargs)

    consumer.user_repo.get_task_github_link_by_comment = mock_get_link

    second_task = await consumer.process_event(event2)
    assert second_task is not None
    assert second_task.task_id == first_task.task_id



# ── Failure & Retries Tests ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_failure_malformed_payload_terminal(session: AsyncSession):
    outbox_repo = OutboxRepository(session)
    event = OutboxEvent(
        event_type="GITHUB_COMMAND_INGESTED",
        aggregate_id="del-malformed-1",
        payload={"invalid": "no required fields at all"},
    )
    await outbox_repo.create_event(event)
    await session.commit()

    consumer = GitHubCommandConsumer(session)
    with pytest.raises(MalformedCommandEventError):
        await consumer.process_event(event)

    ref = await outbox_repo.get_event(event.id)
    assert ref is not None
    assert ref.published_at is not None
    assert ref.payload["terminal_failure"] is True
    assert ref.payload["error_type"] == "MalformedCommandEventError"


@pytest.mark.asyncio
async def test_failure_transient_leaves_event_retryable(
    session: AsyncSession, sample_user: tuple[User, GitHubIdentity]
):
    user, identity = sample_user
    payload = make_command_event_payload(
        actor_id=identity.github_user_id,
        actor_login=identity.github_login,
        command_name="fix",
    )

    outbox_repo = OutboxRepository(session)
    event = OutboxEvent(
        event_type="GITHUB_COMMAND_INGESTED",
        aggregate_id=payload["delivery_id"],
        payload=payload,
    )
    await outbox_repo.create_event(event)
    await session.commit()

    consumer = GitHubCommandConsumer(session)

    # Patch TaskService.create_task to simulate a transient operational failure
    with patch(
        "app.github.command_consumer.TaskService.create_task",
        new=AsyncMock(side_effect=OperationalError("connection reset by peer", None, None)),
    ):
        tasks = await consumer.claim_and_process_batch(limit=10)
        assert len(tasks) == 0

    ref = await outbox_repo.get_event(event.id)
    assert ref is not None
    assert ref.published_at is None  # Still unpublished -> retryable!
    assert ref.payload.get("attempts") == 1
    assert "connection reset by peer" in ref.payload.get("last_error", "")


@pytest.mark.asyncio
async def test_failure_terminal_exhausted_retries_does_not_loop_forever(
    session: AsyncSession, sample_user: tuple[User, GitHubIdentity]
):
    user, identity = sample_user
    payload = make_command_event_payload(
        actor_id=identity.github_user_id,
        actor_login=identity.github_login,
        command_name="fix",
    )

    outbox_repo = OutboxRepository(session)
    event = OutboxEvent(
        event_type="GITHUB_COMMAND_INGESTED",
        aggregate_id=payload["delivery_id"],
        payload=payload,
    )
    await outbox_repo.create_event(event)
    await session.commit()

    consumer = GitHubCommandConsumer(session)

    # Simulate transient failures exceeding max retries (e.g. max_retries = 2)
    with patch("app.github.command_consumer.settings.outbox_max_retries", 2):
        with patch(
            "app.github.command_consumer.TaskService.create_task",
            new=AsyncMock(side_effect=OperationalError("transient db error", None, None)),
        ):
            # Attempt 1
            await consumer.claim_and_process_batch(limit=10)
            ref1 = await outbox_repo.get_event(event.id)
            assert ref1.published_at is None
            assert ref1.payload.get("attempts") == 1

            # Attempt 2 (reaches max_retries = 2)
            await consumer.claim_and_process_batch(limit=10)
            ref2 = await outbox_repo.get_event(event.id)
            assert ref2.published_at is not None  # Marked published/dead-lettered!
            assert ref2.payload.get("terminal_failure") is True
            assert ref2.payload.get("max_attempts_exceeded") is True


# ── Worker Behavior & Scope Discipline Tests ──────────────────────────────────


@pytest.mark.asyncio
async def test_worker_bounded_batch_processing(
    session: AsyncSession, sample_user: tuple[User, GitHubIdentity]
):
    user, identity = sample_user
    outbox_repo = OutboxRepository(session)

    for i in range(5):
        payload = make_command_event_payload(
            actor_id=identity.github_user_id,
            actor_login=identity.github_login,
            command_name="fix",
            arguments=f"batch task {i}",
        )
        event = OutboxEvent(
            event_type="GITHUB_COMMAND_INGESTED",
            aggregate_id=payload["delivery_id"],
            payload=payload,
        )
        await outbox_repo.create_event(event)
    await session.commit()

    consumer = GitHubCommandConsumer(session)
    # Process bounded batch of 2
    batch1 = await consumer.claim_and_process_batch(limit=2)
    assert len(batch1) == 2

    # Process remaining
    batch2 = await consumer.claim_and_process_batch(limit=10)
    assert len(batch2) == 3


@pytest.mark.asyncio
async def test_already_processed_event_ignored(session: AsyncSession):
    outbox_repo = OutboxRepository(session)
    event = OutboxEvent(
        event_type="GITHUB_COMMAND_INGESTED",
        aggregate_id="del-already-done",
        payload={"some": "payload"},
        published_at=datetime.now(UTC),
    )
    await outbox_repo.create_event(event)
    await session.commit()

    consumer = GitHubCommandConsumer(session)
    task = await consumer.process_event(event)
    assert task is None


@pytest.mark.asyncio
async def test_no_agent_execution_or_git_clone_performed(
    session: AsyncSession, sample_user: tuple[User, GitHubIdentity]
):
    """Verifies that Phase 10B.4.1 stops strictly at Job creation and never invokes the agent."""
    user, identity = sample_user
    payload = make_command_event_payload(
        actor_id=identity.github_user_id,
        actor_login=identity.github_login,
        command_name="implement",
        arguments="add payment gateway integration",
    )

    outbox_repo = OutboxRepository(session)
    event = OutboxEvent(
        event_type="GITHUB_COMMAND_INGESTED",
        aggregate_id=payload["delivery_id"],
        payload=payload,
    )
    await outbox_repo.create_event(event)
    await session.commit()

    with (
        patch("app.orchestration.orchestrator.Orchestrator.run") as mock_agent,
        patch("subprocess.run") as mock_subproc,
    ):
        consumer = GitHubCommandConsumer(session)
        task = await consumer.process_event(event)
        assert task is not None
        assert task.status == TaskStatusEnum.PENDING.value

        # Neither agent nor subprocess (git) should be called
        mock_agent.assert_not_called()
        mock_subproc.assert_not_called()
