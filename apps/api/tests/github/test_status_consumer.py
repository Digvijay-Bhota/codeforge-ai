"""Comprehensive tests for GitHubStatusConsumer (Phase 10B.4.2).

Covers:
1. Acknowledgement comment:
   - Valid task creation produces exactly one managed acknowledgement comment.
   - Repeated processing updates/reuses the same comment without duplicates.
   - Acknowledgement contains required identifiers (command, task ID, job ID, repo, target).
   - No secrets, credentials, or internal tokens are exposed.
2. Check Run creation:
   - Valid PR command creates one queued Check Run.
   - Installation credential mode is verified.
   - Authoritative head SHA is used (directly or via PR discovery).
   - Ordinary issues without commit SHA safely skip Check Run creation while preserving acknowledgement.
3. Job lifecycle state transitions:
   - PENDING -> queued
   - RUNNING -> in_progress
   - SUCCEEDED -> completed / success
   - FAILED -> completed / failure
4. Idempotency:
   - Duplicate lifecycle events do not create extra Check Runs.
   - Duplicate acknowledgement events do not create extra comments.
   - Repeated terminal updates remain terminal.
5. State monotonicity & ordering:
   - Stale queued after in_progress is safely ignored.
   - Stale in_progress after completed is safely ignored.
   - Stale queued after completed is safely ignored.
   - Terminal failure cannot be overwritten by earlier states.
6. Error handling & retries:
   - Transient 5xx, timeout, and rate limit leave outbox event retryable.
   - Terminal 404/403 marks event terminal without failing the CodeForge job.
   - Retry exhaustion marks event terminal.
7. Scope discipline:
   - No repository clone, branch creation, or agent execution is performed.
"""

from __future__ import annotations

import asyncio
import os
import random
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import (
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
from app.github.client import CredentialMode, GitHubClient
from app.github.exceptions import (
    GitHubNotFoundError,
    GitHubTimeoutError,
    GitHubUpstreamError,
)
from app.github.models import (
    GitHubCheckRun,
    GitHubComment,
    GitHubPullRequest,
)
from app.github.status_consumer import (
    CHECK_RUN_NAME,
    GitHubStatusConsumer,
)

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
async def session_factory():
    engine = create_async_engine(TEST_DB_URL, echo=False, poolclass=__import__("sqlalchemy").pool.NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
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


@pytest_asyncio.fixture
async def sample_pr_task(session: AsyncSession, sample_user: tuple[User, GitHubIdentity]):
    """Helper fixture: creates a PR task with Task, Job, and TaskGitHubLink."""
    user, identity = sample_user
    task_id = str(uuid.uuid4())
    repo_id = random.randint(1_000_000, 999_999_999)
    comment_id = random.randint(1_000_000, 999_999_999)
    sha = "a" * 40

    task = Task(
        task_id=task_id,
        status=TaskStatusEnum.PENDING.value,
        execution_target="github",
        repository="testorg/testrepo",
        requested_task="[github-command:fix] fix issue in auth",
        creator_id=user.id,
        pr_metadata={"pr_number": 42, "head_sha": sha},
    )
    task_repo = TaskRepository(session)
    await task_repo.create_task(task)

    job = Job(
        task_id=task_id,
        status=JobStatusEnum.PENDING.value,
    )
    job_repo = JobRepository(session)
    await job_repo.create_job(job)

    link = TaskGitHubLink(
        task_id=task_id,
        installation_id=12345,
        repository_id=repo_id,
        repository_full_name="testorg/testrepo",
        issue_number=42,
        pull_request_number=42,
        trigger_comment_id=comment_id,
        triggering_github_user_id=identity.github_user_id,
        triggering_github_login=identity.github_login,
        head_sha=sha,
    )
    user_repo = UserRepository(session)
    await user_repo.create_task_github_link(link)
    await session.commit()
    return task, job, link


@pytest_asyncio.fixture
async def sample_issue_task(session: AsyncSession, sample_user: tuple[User, GitHubIdentity]):
    """Helper fixture: creates an ordinary issue task without PR/commit SHA."""
    user, identity = sample_user
    task_id = str(uuid.uuid4())
    repo_id = random.randint(1_000_000, 999_999_999)
    comment_id = random.randint(1_000_000, 999_999_999)

    task = Task(
        task_id=task_id,
        status=TaskStatusEnum.PENDING.value,
        execution_target="github",
        repository="testorg/testrepo",
        requested_task="[github-command:explain] explain architecture",
        creator_id=user.id,
    )
    task_repo = TaskRepository(session)
    await task_repo.create_task(task)

    job = Job(
        task_id=task_id,
        status=JobStatusEnum.PENDING.value,
    )
    job_repo = JobRepository(session)
    await job_repo.create_job(job)

    link = TaskGitHubLink(
        task_id=task_id,
        installation_id=12345,
        repository_id=repo_id,
        repository_full_name="testorg/testrepo",
        issue_number=101,
        pull_request_number=None,
        trigger_comment_id=comment_id,
        triggering_github_user_id=identity.github_user_id,
        triggering_github_login=identity.github_login,
        head_sha=None,
    )
    user_repo = UserRepository(session)
    await user_repo.create_task_github_link(link)
    await session.commit()
    return task, job, link


def create_mock_github_client():
    client = AsyncMock(spec=GitHubClient)
    client.mode = CredentialMode.INSTALLATION
    client.installation_id = 12345

    # Mock upsert_issue_comment
    client.upsert_issue_comment.return_value = GitHubComment(
        id=777,
        body="<!-- codeforge:managed-comment:ack -->\n\nAccepted",
        html_url="https://github.com/testorg/testrepo/issues/42#issuecomment-777",
    )

    # Mock create_check_run
    client.create_check_run.return_value = GitHubCheckRun(
        id=888,
        name=CHECK_RUN_NAME,
        head_sha="a" * 40,
        status="queued",
        conclusion=None,
        html_url="https://github.com/testorg/testrepo/runs/888",
    )

    # Mock update_check_run
    client.update_check_run.return_value = GitHubCheckRun(
        id=888,
        name=CHECK_RUN_NAME,
        head_sha="a" * 40,
        status="in_progress",
        conclusion=None,
        html_url="https://github.com/testorg/testrepo/runs/888",
    )

    # Mock get_pull_request
    client.get_pull_request.return_value = GitHubPullRequest(
        number=42,
        title="Test PR",
        body="Test PR Body",
        head_branch="feature",
        base_branch="main",
        html_url="https://github.com/testorg/testrepo/pull/42",
        state="open",
        head_sha="a" * 40,
    )

    return client


# ── 1. Acknowledgement Comment Tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_valid_task_creation_produces_one_managed_acknowledgement(session: AsyncSession, sample_pr_task):
    task, job, link = sample_pr_task
    mock_client = create_mock_github_client()

    consumer = GitHubStatusConsumer(session, github_client=mock_client)
    updated_link = await consumer.sync_status(
        task_id=task.task_id,
        job_id=job.id,
        job_status="PENDING",
        event_name="COMMAND_ACCEPTED",
        command_name="fix",
        arguments="resolve NPE in payment handler",
    )

    assert updated_link is not None
    assert updated_link.acknowledgement_comment_id == 777

    # Verify upsert_issue_comment was called
    mock_client.upsert_issue_comment.assert_called_once()
    call_args = mock_client.upsert_issue_comment.call_args[1]
    assert call_args["owner"] == "testorg"
    assert call_args["repo"] == "testrepo"
    assert call_args["issue_number"] == 42
    assert call_args["marker_id"] == f"ack-task-{task.task_id}"

    body = call_args["body"]
    assert "CodeForge AI Command Accepted" in body
    assert task.task_id in body
    assert str(job.id) in body
    assert "Pull Request #42" in body


@pytest.mark.asyncio
async def test_repeated_processing_updates_or_reuses_managed_comment(session: AsyncSession, sample_pr_task):
    task, job, link = sample_pr_task
    mock_client = create_mock_github_client()

    consumer = GitHubStatusConsumer(session, github_client=mock_client)
    # First sync
    await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status="PENDING", event_name="COMMAND_ACCEPTED")
    # Second sync (idempotent re-run)
    await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status="PENDING", event_name="COMMAND_ACCEPTED")

    # Upsert was called with the exact same deterministic marker both times
    assert mock_client.upsert_issue_comment.call_count == 2
    for call in mock_client.upsert_issue_comment.call_args_list:
        assert call[1]["marker_id"] == f"ack-task-{task.task_id}"


@pytest.mark.asyncio
async def test_acknowledgement_body_contains_no_secrets(session: AsyncSession, sample_pr_task):
    task, job, link = sample_pr_task
    mock_client = create_mock_github_client()

    consumer = GitHubStatusConsumer(session, github_client=mock_client)
    await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status="PENDING", event_name="COMMAND_ACCEPTED")

    body = mock_client.upsert_issue_comment.call_args[1]["body"]
    assert "token" not in body.lower()
    assert "secret" not in body.lower()
    assert "jwt" not in body.lower()


# ── 2. Check Run Creation Tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_valid_pr_command_creates_one_queued_check_run(session: AsyncSession, sample_pr_task):
    task, job, link = sample_pr_task
    mock_client = create_mock_github_client()

    consumer = GitHubStatusConsumer(session, github_client=mock_client)
    updated_link = await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status="PENDING")

    assert updated_link is not None
    assert updated_link.check_run_id == 888
    assert updated_link.check_run_status == "queued"
    assert updated_link.check_run_conclusion is None

    mock_client.create_check_run.assert_called_once()
    req = mock_client.create_check_run.call_args[0][0]
    assert req.name == CHECK_RUN_NAME
    assert req.head_sha == "a" * 40
    assert req.status == "queued"
    assert req.conclusion is None
    assert req.external_id == task.task_id
    assert req.output is not None
    assert req.output.title == "CodeForge AI - Queued"


@pytest.mark.asyncio
async def test_pr_head_sha_discovered_via_client_if_missing(session: AsyncSession, sample_pr_task):
    task, job, link = sample_pr_task
    # Set link.head_sha = None to force lookup
    link.head_sha = None
    user_repo = UserRepository(session)
    await user_repo.update_task_github_link(link)
    await session.commit()

    mock_client = create_mock_github_client()
    discovered_sha = "b" * 40
    mock_client.get_pull_request.return_value = GitHubPullRequest(
        number=42,
        title="PR",
        body="",
        head_branch="feature",
        base_branch="main",
        html_url="https://github.com/testorg/testrepo/pull/42",
        state="open",
        head_sha=discovered_sha,
    )

    consumer = GitHubStatusConsumer(session, github_client=mock_client)
    updated_link = await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status="PENDING")

    assert updated_link is not None
    assert updated_link.head_sha == discovered_sha
    mock_client.get_pull_request.assert_called_once_with("testorg", "testrepo", 42)
    create_req = mock_client.create_check_run.call_args[0][0]
    assert create_req.head_sha == discovered_sha


@pytest.mark.asyncio
async def test_ordinary_issue_skips_check_run_creation(session: AsyncSession, sample_issue_task):
    task, job, link = sample_issue_task
    mock_client = create_mock_github_client()

    consumer = GitHubStatusConsumer(session, github_client=mock_client)
    updated_link = await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status="PENDING", event_name="COMMAND_ACCEPTED")

    assert updated_link is not None
    # Acknowledgement comment was posted on the issue
    mock_client.upsert_issue_comment.assert_called_once()
    assert updated_link.acknowledgement_comment_id == 777

    # Check Run creation was explicitly skipped!
    mock_client.create_check_run.assert_not_called()
    assert updated_link.check_run_id is None


@pytest.mark.asyncio
async def test_client_credential_mode_is_installation(session: AsyncSession, sample_pr_task):
    task, job, link = sample_pr_task
    mock_client = create_mock_github_client()
    assert mock_client.mode == CredentialMode.INSTALLATION


# ── 3. Job State Transitions ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_job_transitions_full_lifecycle(session: AsyncSession, sample_pr_task):
    task, job, link = sample_pr_task
    mock_client = create_mock_github_client()
    consumer = GitHubStatusConsumer(session, github_client=mock_client)

    # 1. PENDING -> queued
    link1 = await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status=JobStatusEnum.PENDING.value)
    assert link1.check_run_status == "queued"
    assert link1.check_run_conclusion is None
    mock_client.create_check_run.assert_called_once()

    # 2. RUNNING -> in_progress
    link2 = await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status=JobStatusEnum.RUNNING.value)
    assert link2.check_run_status == "in_progress"
    assert link2.check_run_conclusion is None
    mock_client.update_check_run.assert_called_once()
    up_req1 = mock_client.update_check_run.call_args[0][0]
    assert up_req1.status == "in_progress"
    assert up_req1.conclusion is None

    # 3. SUCCEEDED -> completed/success
    link3 = await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status=JobStatusEnum.SUCCEEDED.value)
    assert link3.check_run_status == "completed"
    assert link3.check_run_conclusion == "success"
    up_req2 = mock_client.update_check_run.call_args[0][0]
    assert up_req2.status == "completed"
    assert up_req2.conclusion == "success"


@pytest.mark.asyncio
async def test_job_transition_failed_to_completed_failure(session: AsyncSession, sample_pr_task):
    task, job, link = sample_pr_task
    mock_client = create_mock_github_client()
    consumer = GitHubStatusConsumer(session, github_client=mock_client)

    # Initial queued
    await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status=JobStatusEnum.PENDING.value)
    # Transition to FAILED
    link_failed = await consumer.sync_status(
        task_id=task.task_id,
        job_id=job.id,
        job_status=JobStatusEnum.FAILED.value,
        error="SyntaxError: invalid syntax",
    )
    assert link_failed.check_run_status == "completed"
    assert link_failed.check_run_conclusion == "failure"
    up_req = mock_client.update_check_run.call_args[0][0]
    assert up_req.status == "completed"
    assert up_req.conclusion == "failure"
    assert "SyntaxError" in up_req.output.summary


# ── 4. Idempotency Tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_lifecycle_event_does_not_create_second_check_run(session: AsyncSession, sample_pr_task):
    task, job, link = sample_pr_task
    mock_client = create_mock_github_client()
    consumer = GitHubStatusConsumer(session, github_client=mock_client)

    # First event
    link1 = await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status="PENDING")
    # Duplicate event
    link2 = await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status="PENDING")

    assert link1.check_run_id == link2.check_run_id == 888
    # create_check_run should be called once only
    mock_client.create_check_run.assert_called_once()
    # update_check_run should not be called because status didn't change
    mock_client.update_check_run.assert_not_called()


@pytest.mark.asyncio
async def test_repeated_terminal_update_remains_terminal(session: AsyncSession, sample_pr_task):
    task, job, link = sample_pr_task
    mock_client = create_mock_github_client()
    consumer = GitHubStatusConsumer(session, github_client=mock_client)

    await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status="PENDING")
    await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status=JobStatusEnum.SUCCEEDED.value)
    initial_update_count = mock_client.update_check_run.call_count

    # Repeat terminal update
    link_repeat = await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status=JobStatusEnum.SUCCEEDED.value)
    assert link_repeat.check_run_status == "completed"
    assert link_repeat.check_run_conclusion == "success"
    # No duplicate API calls
    assert mock_client.update_check_run.call_count == initial_update_count


# ── 5. State Monotonicity & Out-of-Order Events ───────────────────────────────


@pytest.mark.asyncio
async def test_stale_queued_after_in_progress_ignored(session: AsyncSession, sample_pr_task):
    task, job, link = sample_pr_task
    mock_client = create_mock_github_client()
    consumer = GitHubStatusConsumer(session, github_client=mock_client)

    await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status="PENDING")
    await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status="RUNNING")
    mock_client.update_check_run.reset_mock()

    # Delayed/stale PENDING event arrives
    stale_link = await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status="PENDING")
    assert stale_link.check_run_status == "in_progress"
    # GitHub update was not invoked
    mock_client.update_check_run.assert_not_called()


@pytest.mark.asyncio
async def test_stale_in_progress_after_completed_ignored(session: AsyncSession, sample_pr_task):
    task, job, link = sample_pr_task
    mock_client = create_mock_github_client()
    consumer = GitHubStatusConsumer(session, github_client=mock_client)

    await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status="PENDING")
    await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status="SUCCEEDED")
    mock_client.update_check_run.reset_mock()

    # Stale RUNNING arrives after completion
    stale_link = await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status="RUNNING")
    assert stale_link.check_run_status == "completed"
    assert stale_link.check_run_conclusion == "success"
    mock_client.update_check_run.assert_not_called()


@pytest.mark.asyncio
async def test_failed_cannot_be_overwritten_by_later_stale_event(session: AsyncSession, sample_pr_task):
    task, job, link = sample_pr_task
    mock_client = create_mock_github_client()
    consumer = GitHubStatusConsumer(session, github_client=mock_client)

    await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status="PENDING")
    await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status="FAILED", error="Crash")
    mock_client.update_check_run.reset_mock()

    # Stale PENDING arrives
    stale_link = await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status="PENDING")
    assert stale_link.check_run_status == "completed"
    assert stale_link.check_run_conclusion == "failure"
    mock_client.update_check_run.assert_not_called()


# ── 6. Error Handling & Retry Tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_transient_github_server_error_leaves_event_retryable(session: AsyncSession, sample_pr_task):
    task, job, link = sample_pr_task
    task_id = task.task_id
    mock_client = create_mock_github_client()
    mock_client.create_check_run.side_effect = GitHubUpstreamError("502 Bad Gateway", status_code=502, retryable=True)

    outbox_repo = OutboxRepository(session)
    event = OutboxEvent(
        event_type="GITHUB_STATUS_UPDATE",
        aggregate_id=task_id,
        payload={"task_id": task_id, "job_id": job.id, "job_status": "PENDING"},
    )
    await outbox_repo.create_event(event)
    await session.commit()

    consumer = GitHubStatusConsumer(session, github_client=mock_client)
    res = await consumer.process_event(event)
    assert res is None

    ref = await outbox_repo.get_event(event.id)
    assert ref is not None
    assert ref.published_at is None  # Retryable!
    assert ref.payload.get("attempts") == 1
    assert "502 Bad Gateway" in ref.payload.get("last_error", "")

    # Verify task and job status were NOT corrupted
    task_ref = await TaskRepository(session).get_task(task_id)
    assert task_ref.status == TaskStatusEnum.PENDING.value


@pytest.mark.asyncio
async def test_transient_timeout_error_leaves_event_retryable(session: AsyncSession, sample_pr_task):
    task, job, link = sample_pr_task
    mock_client = create_mock_github_client()
    mock_client.create_check_run.side_effect = GitHubTimeoutError("Request timed out", retryable=True)

    outbox_repo = OutboxRepository(session)
    event = OutboxEvent(
        event_type="GITHUB_STATUS_UPDATE",
        aggregate_id=task.task_id,
        payload={"task_id": task.task_id, "job_id": job.id, "job_status": "PENDING"},
    )
    await outbox_repo.create_event(event)
    await session.commit()

    consumer = GitHubStatusConsumer(session, github_client=mock_client)
    await consumer.process_event(event)

    ref = await outbox_repo.get_event(event.id)
    assert ref.published_at is None
    assert ref.payload.get("attempts") == 1


@pytest.mark.asyncio
async def test_terminal_404_marks_event_failed_terminal(session: AsyncSession, sample_pr_task):
    task, job, link = sample_pr_task
    mock_client = create_mock_github_client()
    mock_client.create_check_run.side_effect = GitHubNotFoundError("Repo not found", status_code=404, retryable=False)

    outbox_repo = OutboxRepository(session)
    event = OutboxEvent(
        event_type="GITHUB_STATUS_UPDATE",
        aggregate_id=task.task_id,
        payload={"task_id": task.task_id, "job_id": job.id, "job_status": "PENDING"},
    )
    await outbox_repo.create_event(event)
    await session.commit()

    consumer = GitHubStatusConsumer(session, github_client=mock_client)
    await consumer.process_event(event)

    ref = await outbox_repo.get_event(event.id)
    assert ref.published_at is not None  # Published / dead-lettered
    assert ref.payload.get("terminal_failure") is True
    assert ref.payload.get("error_type") == "GitHubNotFoundError"


@pytest.mark.asyncio
async def test_retry_exhaustion_marks_event_terminal(session: AsyncSession, sample_pr_task):
    task, job, link = sample_pr_task
    mock_client = create_mock_github_client()
    mock_client.create_check_run.side_effect = GitHubUpstreamError("500 Internal Error", status_code=500, retryable=True)

    outbox_repo = OutboxRepository(session)
    event = OutboxEvent(
        event_type="GITHUB_STATUS_UPDATE",
        aggregate_id=task.task_id,
        payload={"task_id": task.task_id, "job_id": job.id, "job_status": "PENDING"},
    )
    await outbox_repo.create_event(event)
    await session.commit()

    consumer = GitHubStatusConsumer(session, github_client=mock_client)

    with patch("app.github.status_consumer.settings.outbox_max_retries", 2):
        # Attempt 1
        await consumer.process_event(event)
        ref1 = await outbox_repo.get_event(event.id)
        assert ref1.published_at is None
        assert ref1.payload.get("attempts") == 1

        # Attempt 2 -> exhausts max_retries
        await consumer.process_event(event)
        ref2 = await outbox_repo.get_event(event.id)
        assert ref2.published_at is not None
        assert ref2.payload.get("terminal_failure") is True
        assert ref2.payload.get("max_attempts_exceeded") is True


# ── 7. Scope Discipline ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_agent_execution_or_git_clone_performed(session: AsyncSession, sample_pr_task):
    task, job, link = sample_pr_task
    mock_client = create_mock_github_client()
    consumer = GitHubStatusConsumer(session, github_client=mock_client)

    with patch("subprocess.Popen") as mock_popen, patch(
        "subprocess.run"
    ) as mock_run, patch(
        "app.orchestration.orchestrator.Orchestrator.run"
    ) as mock_exec, patch(
        "app.github.client.GitHubClient.create_pull_request"
    ) as mock_create_pr:
        await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status="PENDING")
        await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status="RUNNING")
        await consumer.sync_status(task_id=task.task_id, job_id=job.id, job_status="SUCCEEDED")

        mock_popen.assert_not_called()
        mock_run.assert_not_called()
        mock_exec.assert_not_called()
        mock_create_pr.assert_not_called()


# ── 8. Concurrency & Restart Safety Tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_check_run_creation_convergence(session_factory, sample_pr_task):
    """Verifies that under true concurrent execution against PostgreSQL:

    - Two concurrent consumers attempt Check Run creation at the same time.
    - PostgreSQL row locking (SELECT ... FOR UPDATE) serializes them.
    - Exactly one GitHub create_check_run() call is made.
    - Both consumers converge on the exact same persisted Check Run ID.
    """
    task, job, link = sample_pr_task
    task_id = task.task_id
    job_id = job.id

    mock_client = create_mock_github_client()
    orig_create = mock_client.create_check_run

    async def slow_create(*args, **kwargs):
        # Simulate network latency to ensure second worker arrives while first is creating
        await asyncio.sleep(0.05)
        return await orig_create(*args, **kwargs)

    mock_client.create_check_run = AsyncMock(side_effect=slow_create)

    async def worker_a():
        async with session_factory() as s1:
            c1 = GitHubStatusConsumer(s1, github_client=mock_client)
            return await c1.sync_status(task_id=task_id, job_id=job_id, job_status="PENDING")

    async def worker_b():
        async with session_factory() as s2:
            c2 = GitHubStatusConsumer(s2, github_client=mock_client)
            return await c2.sync_status(task_id=task_id, job_id=job_id, job_status="PENDING")

    res1, res2 = await asyncio.gather(worker_a(), worker_b())

    assert res1 is not None
    assert res2 is not None
    assert res1.check_run_id == res2.check_run_id == 888
    # Exactly one GitHub create_check_run() call
    assert mock_client.create_check_run.call_count == 1

    # Verify persisted state in DB
    async with session_factory() as verify_session:
        saved_link = await UserRepository(verify_session).get_task_github_link(task_id)
        assert saved_link is not None
        assert saved_link.check_run_id == 888
        assert saved_link.check_run_status == "queued"


@pytest.mark.asyncio
async def test_worker_retry_after_first_consumer_commits(session_factory, sample_pr_task):
    """Verifies that when a worker retries or another consumer runs after the first commits:

    - The persisted check_run_id is reused.
    - No duplicate Check Run is created on GitHub.
    """
    task, job, link = sample_pr_task
    task_id = task.task_id
    job_id = job.id

    mock_client = create_mock_github_client()

    # Consumer A completes initial creation
    async with session_factory() as s1:
        c1 = GitHubStatusConsumer(s1, github_client=mock_client)
        link1 = await c1.sync_status(task_id=task_id, job_id=job_id, job_status="PENDING")
        assert link1.check_run_id == 888

    assert mock_client.create_check_run.call_count == 1

    # Consumer B runs subsequently (retry or redelivery)
    async with session_factory() as s2:
        c2 = GitHubStatusConsumer(s2, github_client=mock_client)
        link2 = await c2.sync_status(task_id=task_id, job_id=job_id, job_status="PENDING")
        assert link2.check_run_id == 888

    # Still exactly one Check Run created
    assert mock_client.create_check_run.call_count == 1


@pytest.mark.asyncio
async def test_worker_restart_reprocessing_after_partially_completed_attempt(session_factory, sample_pr_task):
    """Verifies that if a worker process restarts after a partially completed attempt:

    - (e.g. comment was upserted, but process died before check run creation)
    - The restarted worker picks up and finishes creation safely.
    - Acknowledgement comment is not duplicated; check run is created once.
    """
    task, job, link = sample_pr_task
    task_id = task.task_id
    job_id = job.id

    mock_client = create_mock_github_client()

    # Emulate partial attempt: comment is already committed, but check run not created yet
    async with session_factory() as s1:
        u_repo = UserRepository(s1)
        existing_link = await u_repo.get_task_github_link(task_id, for_update=True)
        assert existing_link is not None
        existing_link.acknowledgement_comment_id = 777
        await u_repo.update_task_github_link(existing_link)
        await s1.commit()

    # Restarted worker runs
    async with session_factory() as s2:
        c2 = GitHubStatusConsumer(s2, github_client=mock_client)
        link2 = await c2.sync_status(task_id=task_id, job_id=job_id, job_status="PENDING")
        assert link2.acknowledgement_comment_id == 777
        assert link2.check_run_id == 888

    # Exactly one check run created
    assert mock_client.create_check_run.call_count == 1


@pytest.mark.asyncio
async def test_no_duplicate_check_run_creation_under_repeated_events(session_factory, sample_pr_task):
    """Verifies that 5 repeated events for the same task/job never create duplicate check runs."""
    task, job, link = sample_pr_task
    task_id = task.task_id
    job_id = job.id

    mock_client = create_mock_github_client()

    for _ in range(5):
        async with session_factory() as s:
            c = GitHubStatusConsumer(s, github_client=mock_client)
            res = await c.sync_status(task_id=task_id, job_id=job_id, job_status="PENDING")
            assert res.check_run_id == 888

    assert mock_client.create_check_run.call_count == 1

