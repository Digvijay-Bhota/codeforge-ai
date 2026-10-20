"""CF-05 regression tests: generated changes are published only after human approval.

Before this fix a GitHub task pushed its branch and opened a pull request as
soon as the agent finished.  The target repository's CI then ran the
generated code - tests, build scripts, workflows - with the repository's
secrets, before any human had seen it.  Plan approval happens before any code
exists, so it never covered what was pushed.

Now generation only captures the changes as an artifact; a user with write
access must approve that exact artifact (by digest) before a separate
publication job pushes it, and the publication job re-checks everything.

The GitHub API client and git are faked; every refused case asserts that no
branch, commit, push or pull request was attempted.
"""

from __future__ import annotations

import random
import subprocess  # nosec B404 - runs git on local test repositories only
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db.models import (
    PUBLICATION_APPROVAL,
    ApprovalStatusEnum,
    GitHubInstallation,
    GitHubInstallationRepository,
    Job,
    JobStatusEnum,
    OutboxEvent,
    Task,
    TaskApproval,
    TaskPublicationArtifact,
    TaskStatusEnum,
    User,
)
from app.db.repositories.job_repository import MAX_JOB_ATTEMPTS
from app.db.repositories.task_repository import TaskRepository
from app.db.repositories.user_repository import UserRepository
from app.db.session import get_db_session
from app.execution.github_execution import GitHubExecutionError, GitHubExecutionService
from app.execution.publication import artifact_digest
from app.github.git import GitError, SafeGitWrapper
from app.main import create_app
from app.schemas.task import ExecutionTarget, TaskRequest, TaskResult, TaskStatus
from app.services.auth_service import AuthService
from app.services.authorization_service import set_collaborator_permission_resolver
from app.services.task_service import TaskService
from app.worker import _request_publication_approval, process_job

BASE_SHA = "b" * 40
DEFAULT_FILES = [{"path": "src/widget.py", "action": "write", "content": "def widget():\n    return 42\n"}]


# ── Fixtures ───────────────────────────────────────────────────────────────────


@dataclass
class Account:
    user: User
    login: str
    headers: dict[str, str]


@dataclass
class World:
    repo: str
    perms: dict[str, str]
    writer: Account
    reader: Account
    outsider: Account


@pytest.fixture
def maker(session: AsyncSession) -> Any:
    m = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
    with patch("app.worker.async_session_maker", m):
        yield m


@pytest_asyncio.fixture
async def client(maker: Any) -> Any:
    app = create_app()

    async def override_get_db() -> Any:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db_session] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def _account(session: AsyncSession, prefix: str) -> Account:
    login = f"{prefix}-{uuid.uuid4().hex[:6]}"
    user, identity = await UserRepository(session).upsert_github_user(
        github_user_id=random.randint(1_000_000, 999_999_999), github_login=login, display_name=prefix,
    )
    await session.commit()
    token = AuthService().create_session_jwt(user, identity.github_user_id, identity.github_login)
    return Account(user=user, login=login, headers={"Authorization": f"Bearer {token}"})


@pytest_asyncio.fixture
async def world(session: AsyncSession) -> Any:
    install_id = random.randint(1_000_000, 999_999_999)
    org = f"org-{uuid.uuid4().hex[:6]}"
    repo = f"{org}/widgets"
    session.add(GitHubInstallation(installation_id=install_id, account_login=org, account_type="Org", active=True))
    session.add(GitHubInstallationRepository(installation_id=install_id, repository=repo, authorized=True))
    await session.commit()
    writer = await _account(session, "writer")
    reader = await _account(session, "reader")
    outsider = await _account(session, "outsider")
    perms = {writer.login: "write", reader.login: "read"}
    set_collaborator_permission_resolver(lambda owner, name, login: perms.get(login, "none"))
    try:
        yield World(repo=repo, perms=perms, writer=writer, reader=reader, outsider=outsider)
    finally:
        set_collaborator_permission_resolver(None)


@dataclass
class FakeGitHub:
    client: MagicMock
    git_cls: MagicMock
    base_files: dict[str, str] = field(default_factory=dict)
    commits: list[dict[str, Any]] = field(default_factory=list)

    @property
    def git(self) -> MagicMock:
        return self.git_cls.return_value

    def workspace(self) -> Path:
        return Path(self.git_cls.call_args.args[0])

    def assert_nothing_published(self) -> None:
        self.client.create_branch.assert_not_awaited()
        self.git.commit_files.assert_not_called()
        self.git.push.assert_not_called()
        self.client.find_open_pull_request.assert_not_awaited()
        self.client.create_pull_request.assert_not_awaited()


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    clones = tmp_path / "clones"
    clones.mkdir()
    monkeypatch.setattr(settings, "codeforge_github_clone_root", str(clones))

    client = MagicMock()
    client.token = "test-token"  # nosec B105 - fake
    client.create_branch = AsyncMock()
    client.find_open_pull_request = AsyncMock(return_value=None)
    client.create_pull_request = AsyncMock(
        return_value=MagicMock(number=17, html_url="https://github.com/o/r/pull/17")
    )
    git_cls = MagicMock()
    fake = FakeGitHub(client=client, git_cls=git_cls)

    def clone(owner: str, repo: str) -> None:
        for path, content in fake.base_files.items():
            target = fake.workspace() / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

    def commit_files(paths: list[str], message: str, commit_timestamp: int | None = None) -> str:
        ws = fake.workspace()
        fake.commits.append({
            "paths": list(paths),
            "timestamp": commit_timestamp,
            "tree": {p: (ws / p).read_text() if (ws / p).exists() else None for p in paths},
        })
        return "c" * 40

    git_cls.return_value.clone.side_effect = clone
    git_cls.return_value.commit_files.side_effect = commit_files
    with (
        patch("app.execution.github_execution.GitHubClient", return_value=client),
        patch("app.execution.github_execution.SafeGitWrapper", git_cls),
        patch("app.services.task_service.Orchestrator"),
    ):
        yield fake


# ── Helpers ────────────────────────────────────────────────────────────────────


def make_artifact(repo: str, files: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    files = sorted(files or DEFAULT_FILES, key=lambda f: f["path"])
    return {
        "repository": repo,
        "base_branch": "main",
        "base_sha": BASE_SHA,
        "files": files,
        "digest": artifact_digest(repo, BASE_SHA, files),
        "total_bytes": sum(len(f["content"].encode()) for f in files if f["content"] is not None),
    }


async def waiting_task(
    session: AsyncSession, world: World, files: list[dict[str, Any]] | None = None
) -> tuple[str, dict[str, Any]]:
    """A GitHub task whose generation finished: WAITING_PUBLICATION with a pending approval."""
    task = Task(
        task_id=str(uuid.uuid4()),
        status=TaskStatusEnum.TESTING.value,
        execution_target="github",
        repository=world.repo,
        requested_task="Fix the widget calculation",
        creator_id=world.writer.user.id,
    )
    session.add(task)
    await session.flush()
    session.add(Job(task_id=task.task_id, status=JobStatusEnum.SUCCEEDED.value))
    artifact = make_artifact(world.repo, files)
    await _request_publication_approval(session, task, artifact)
    await session.commit()
    return task.task_id, artifact


async def approve(client: AsyncClient, account: Account, task_id: str, digest: str, **extra: Any) -> Any:
    return await client.post(
        f"/api/v1/tasks/{task_id}/publication/approve",
        json={"artifact_digest": digest, **extra},
        headers=account.headers,
    )


async def run_pending_jobs(maker: Any, task_id: str) -> list[int]:
    async with maker() as s:
        ids = [
            j.id
            for j in (
                await s.execute(
                    select(Job).where(Job.task_id == task_id, Job.status == JobStatusEnum.PENDING.value)
                )
            ).scalars()
        ]
    for job_id in ids:
        await process_job(job_id)
    return ids


@dataclass
class State:
    task: Task
    approval: TaskApproval | None
    artifact: TaskPublicationArtifact | None
    jobs: list[Job]
    events: list[str]


async def state(maker: Any, task_id: str) -> State:
    async with maker() as s:
        repo = TaskRepository(s)
        task = await repo.get_task(task_id)
        assert task is not None
        jobs = list((await s.execute(select(Job).where(Job.task_id == task_id).order_by(Job.id))).scalars())
        rows = (
            await s.execute(
                select(OutboxEvent)
                .where(OutboxEvent.aggregate_id == task_id, OutboxEvent.event_type == "GITHUB_STATUS_UPDATE")
                .order_by(OutboxEvent.id)
            )
        ).scalars()
        return State(
            task=task,
            approval=await repo.get_latest_publication_approval(task_id),
            artifact=await repo.get_publication_artifact(task_id),
            jobs=jobs,
            events=[r.payload.get("event") for r in rows],
        )


def generation_result(task_id: str, artifact: dict[str, Any] | None) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        status=TaskStatus.success,
        description="Fix the widget calculation",
        plan=[],
        changed_files=[],
        diff="",
        test_result=None,
        error_message=None,
        agent_output="Implemented the fix.",
        publication_artifact=artifact,
    )


async def github_job(session: AsyncSession, world: World, **fields: Any) -> tuple[str, int]:
    task = Task(
        task_id=str(uuid.uuid4()),
        status=TaskStatusEnum.PENDING.value,
        execution_target="github",
        repository=world.repo,
        requested_task="Fix the widget calculation",
        creator_id=world.writer.user.id,
        **fields,
    )
    session.add(task)
    await session.flush()
    job = Job(task_id=task.task_id, status=JobStatusEnum.PENDING.value)
    session.add(job)
    await session.commit()
    return task.task_id, job.id


# ── Generation never publishes (properties 6, 7) ───────────────────────────────


@pytest.mark.asyncio
async def test_generation_waits_for_publication_approval_without_pushing(
    session: AsyncSession, maker: Any, world: World, gh: FakeGitHub
) -> None:
    task_id, job_id = await github_job(session, world)
    artifact = make_artifact(world.repo)
    with patch("app.services.task_service.TaskService.execute_task",
               new=AsyncMock(return_value=generation_result(task_id, artifact))):  # fmt: skip
        await process_job(job_id)

    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.WAITING_PUBLICATION.value
    assert st.approval is not None
    assert st.approval.approval_type == PUBLICATION_APPROVAL
    assert st.approval.status == ApprovalStatusEnum.PENDING.value
    assert st.approval.artifact_digest == artifact["digest"]
    assert st.artifact is not None and st.artifact.digest == artifact["digest"]
    assert st.artifact.files == artifact["files"]
    assert [j.status for j in st.jobs] == [JobStatusEnum.SUCCEEDED.value]
    # The check run stays open: no completion is reported before publication.
    assert st.events == ["JOB_STARTED"]
    assert "publication_artifact" not in (st.task.task_result or {})
    gh.assert_nothing_published()
    gh.git_cls.assert_not_called()


@pytest.mark.asyncio
async def test_github_success_without_reviewable_changes_fails_closed(
    session: AsyncSession, maker: Any, world: World, gh: FakeGitHub
) -> None:
    task_id, job_id = await github_job(session, world)
    with patch("app.services.task_service.TaskService.execute_task",
               new=AsyncMock(return_value=generation_result(task_id, None))):  # fmt: skip
        await process_job(job_id)

    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.FAILED.value
    assert st.approval is None and st.artifact is None
    gh.assert_nothing_published()


# ── Authorization (property 8) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_without_write_access_cannot_approve_publication(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id, artifact = await waiting_task(session, world)

    # Same responses as plan approval: 403 for a write check (tasks/{id}/approve).
    assert (await approve(client, world.reader, task_id, artifact["digest"])).status_code == 403
    assert (await approve(client, world.outsider, task_id, artifact["digest"])).status_code == 403
    res = await client.post(
        f"/api/v1/tasks/{task_id}/publication/approve", json={"artifact_digest": artifact["digest"]}
    )
    assert res.status_code == 401

    await run_pending_jobs(maker, task_id)
    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.WAITING_PUBLICATION.value
    assert st.approval is not None and st.approval.status == ApprovalStatusEnum.PENDING.value
    assert len(st.jobs) == 1
    gh.assert_nothing_published()


@pytest.mark.asyncio
async def test_user_without_write_access_cannot_reject_publication(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World
) -> None:
    task_id, _ = await waiting_task(session, world)
    res = await client.post(f"/api/v1/tasks/{task_id}/publication/reject", headers=world.reader.headers)
    assert res.status_code == 403
    assert (await state(maker, task_id)).task.status == TaskStatusEnum.WAITING_PUBLICATION.value


@pytest.mark.asyncio
async def test_approver_identity_comes_from_the_session_not_the_request(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id, artifact = await waiting_task(session, world)
    res = await approve(client, world.writer, task_id, artifact["digest"],
                        approved_by="ceo", approver="ceo", approved_by_user_id="x")  # fmt: skip
    assert res.status_code == 200
    st = await state(maker, task_id)
    assert st.approval is not None
    assert st.approval.approved_by == world.writer.login
    assert st.approval.approved_by_user_id == world.writer.user.id


@pytest.mark.asyncio
async def test_approver_who_lost_write_access_before_publication_is_refused(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id, artifact = await waiting_task(session, world)
    assert (await approve(client, world.writer, task_id, artifact["digest"])).status_code == 200
    world.perms[world.writer.login] = "read"  # access revoked while the job was queued

    await run_pending_jobs(maker, task_id)
    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.FAILED.value
    assert st.task.failure_code == "PUBLICATION_FAILED"
    assert "write access" in (st.task.failure_reason or "")
    assert st.events[-1] == "JOB_FAILED"
    gh.assert_nothing_published()


@pytest.mark.asyncio
async def test_deactivated_approver_is_refused(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id, artifact = await waiting_task(session, world)
    assert (await approve(client, world.writer, task_id, artifact["digest"])).status_code == 200
    await session.execute(update(User).where(User.id == world.writer.user.id).values(is_active=False))
    await session.commit()

    await run_pending_jobs(maker, task_id)
    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.FAILED.value
    gh.assert_nothing_published()


# ── Digest binding (property 9) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approval_of_a_different_digest_is_rejected(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id, artifact = await waiting_task(session, world)
    other = make_artifact(world.repo, [{"path": "src/widget.py", "action": "write", "content": "evil\n"}])

    res = await approve(client, world.writer, task_id, other["digest"])
    assert res.status_code == 409
    assert (await approve(client, world.writer, task_id, "not-a-digest")).status_code == 422

    await run_pending_jobs(maker, task_id)
    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.WAITING_PUBLICATION.value
    assert st.approval is not None and st.approval.status == ApprovalStatusEnum.PENDING.value
    assert len(st.jobs) == 1
    gh.assert_nothing_published()


@pytest.mark.asyncio
async def test_changes_modified_after_approval_are_not_published(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id, artifact = await waiting_task(session, world)
    assert (await approve(client, world.writer, task_id, artifact["digest"])).status_code == 200
    tampered = [{"path": "src/widget.py", "action": "write", "content": "import os; os.system('curl ...')\n"}]
    await session.execute(
        update(TaskPublicationArtifact).where(TaskPublicationArtifact.task_id == task_id).values(files=tampered)
    )
    await session.commit()

    await run_pending_jobs(maker, task_id)
    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.FAILED.value
    assert "digest" in (st.task.failure_reason or "")
    gh.assert_nothing_published()


@pytest.mark.asyncio
async def test_changes_replaced_with_a_matching_new_digest_after_approval_are_not_published(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id, artifact = await waiting_task(session, world)
    assert (await approve(client, world.writer, task_id, artifact["digest"])).status_code == 200
    swapped = make_artifact(world.repo, [{"path": "src/widget.py", "action": "write", "content": "evil\n"}])
    await session.execute(
        update(TaskPublicationArtifact)
        .where(TaskPublicationArtifact.task_id == task_id)
        .values(files=swapped["files"], digest=swapped["digest"])
    )
    await session.commit()

    await run_pending_jobs(maker, task_id)
    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.FAILED.value
    assert "not been approved" in (st.task.failure_reason or "")
    gh.assert_nothing_published()


@pytest.mark.asyncio
async def test_changes_modified_before_approval_cannot_be_approved(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id, artifact = await waiting_task(session, world)
    await session.execute(
        update(TaskPublicationArtifact)
        .where(TaskPublicationArtifact.task_id == task_id)
        .values(files=[{"path": "src/widget.py", "action": "write", "content": "evil\n"}])
    )
    await session.commit()

    assert (await approve(client, world.writer, task_id, artifact["digest"])).status_code == 409
    await run_pending_jobs(maker, task_id)
    assert (await state(maker, task_id)).task.status == TaskStatusEnum.WAITING_PUBLICATION.value
    gh.assert_nothing_published()


# ── Retries and duplicates (property 10) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_approval_is_idempotent(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id, artifact = await waiting_task(session, world)
    first = await approve(client, world.writer, task_id, artifact["digest"])
    second = await approve(client, world.writer, task_id, artifact["digest"])
    assert (first.status_code, second.status_code) == (200, 200)
    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.PUBLISHING.value
    assert [j.status for j in st.jobs] == [JobStatusEnum.SUCCEEDED.value, JobStatusEnum.PENDING.value]

    await run_pending_jobs(maker, task_id)
    # A repeat after publication is still a no-op, and a different digest is refused.
    assert (await approve(client, world.writer, task_id, artifact["digest"])).status_code == 200
    other = make_artifact(world.repo, [{"path": "x.py", "action": "write", "content": "x\n"}])
    assert (await approve(client, world.writer, task_id, other["digest"])).status_code == 409
    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.COMPLETED.value
    assert len(st.jobs) == 2
    gh.git.push.assert_called_once()
    gh.client.create_pull_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_redelivered_jobs_after_publication_do_not_publish_again(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id, artifact = await waiting_task(session, world)
    assert (await approve(client, world.writer, task_id, artifact["digest"])).status_code == 200
    [job_id] = await run_pending_jobs(maker, task_id)
    gh.git.push.assert_called_once()

    await process_job(job_id)  # duplicate delivery of the finished job
    session.add(Job(task_id=task_id, status=JobStatusEnum.PENDING.value))  # a stray extra job
    await session.commit()
    await run_pending_jobs(maker, task_id)

    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.COMPLETED.value
    gh.git.push.assert_called_once()
    gh.client.create_pull_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_publishing_status_without_an_approval_never_pushes(
    session: AsyncSession, maker: Any, world: World, gh: FakeGitHub
) -> None:
    """A job reaching PUBLISHING by any other route still needs the approval."""
    task_id, _ = await waiting_task(session, world)
    await session.execute(update(Task).where(Task.task_id == task_id).values(status=TaskStatusEnum.PUBLISHING.value))
    session.add(Job(task_id=task_id, status=JobStatusEnum.PENDING.value))
    await session.commit()

    await run_pending_jobs(maker, task_id)
    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.FAILED.value
    assert "not been approved" in (st.task.failure_reason or "")
    gh.assert_nothing_published()


@pytest.mark.asyncio
async def test_generation_job_redelivered_while_awaiting_approval_does_not_publish(
    session: AsyncSession, maker: Any, world: World, gh: FakeGitHub
) -> None:
    task_id, _ = await waiting_task(session, world)
    session.add(Job(task_id=task_id, status=JobStatusEnum.PENDING.value))
    await session.commit()
    with patch("app.services.task_service.TaskService.execute_task", new=AsyncMock()) as execute:
        await run_pending_jobs(maker, task_id)
    execute.assert_not_awaited()
    assert (await state(maker, task_id)).task.status != TaskStatusEnum.COMPLETED.value
    gh.assert_nothing_published()


# ── Stale, rejected or missing approval (property 11) ──────────────────────────


@pytest.mark.asyncio
async def test_expired_publication_request_cannot_be_approved(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id, artifact = await waiting_task(session, world)
    await session.execute(
        update(TaskApproval)
        .where(TaskApproval.task_id == task_id)
        .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await session.commit()

    res = await approve(client, world.writer, task_id, artifact["digest"])
    assert res.status_code == 409 and "expired" in res.json()["detail"]
    st = await state(maker, task_id)  # the expiry was persisted, not rolled back
    assert st.task.status == TaskStatusEnum.CANCELLED.value
    assert st.approval is not None and st.approval.status == ApprovalStatusEnum.EXPIRED.value
    assert st.events[-1] == "JOB_FAILED"

    assert (await approve(client, world.writer, task_id, artifact["digest"])).status_code == 409
    await run_pending_jobs(maker, task_id)
    gh.assert_nothing_published()


@pytest.mark.asyncio
async def test_rejected_publication_cannot_be_approved(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id, artifact = await waiting_task(session, world)
    res = await client.post(
        f"/api/v1/tasks/{task_id}/publication/reject", json={"reason": "Touches billing"}, headers=world.writer.headers
    )
    assert res.status_code == 200
    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.CANCELLED.value
    assert st.approval is not None and st.approval.status == ApprovalStatusEnum.REJECTED.value
    assert st.events[-1] == "JOB_FAILED"

    assert (await approve(client, world.writer, task_id, artifact["digest"])).status_code == 409
    await run_pending_jobs(maker, task_id)
    gh.assert_nothing_published()


@pytest.mark.asyncio
async def test_missing_artifact_fails_closed(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id, artifact = await waiting_task(session, world)
    row = (await session.execute(select(TaskPublicationArtifact).where(TaskPublicationArtifact.task_id == task_id))).scalar_one()
    await session.delete(row)
    await session.commit()

    assert (await approve(client, world.writer, task_id, artifact["digest"])).status_code == 409
    await run_pending_jobs(maker, task_id)
    gh.assert_nothing_published()


# ── Exact publication after approval (property 12) ─────────────────────────────


@pytest.mark.asyncio
async def test_approved_changes_are_published_exactly(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    gh.base_files = {"src/widget.py": "def widget():\n    return 41\n", "old.txt": "obsolete\n"}
    files = [*DEFAULT_FILES, {"path": "old.txt", "action": "delete", "content": None},
             {"path": "tests/test_widget.py", "action": "write", "content": "def test(): pass\n"}]  # fmt: skip
    task_id, artifact = await waiting_task(session, world, files)
    assert (await approve(client, world.writer, task_id, artifact["digest"])).status_code == 200

    await run_pending_jobs(maker, task_id)

    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.COMPLETED.value
    assert st.artifact is not None
    gh.git.checkout_new_branch.assert_called_once_with(f"codeforge/task-{task_id}", BASE_SHA)
    [commit] = gh.commits
    assert commit["paths"] == ["old.txt", "src/widget.py", "tests/test_widget.py"]
    assert commit["tree"] == {f["path"]: f["content"] for f in artifact["files"]}
    assert commit["timestamp"] == int(st.artifact.created_at.timestamp())
    gh.git.push.assert_called_once_with(f"codeforge/task-{task_id}")
    pr = gh.client.create_pull_request.await_args.args[0]
    assert (pr.head_branch, pr.base_branch) == (f"codeforge/task-{task_id}", "main")
    assert artifact["digest"] in pr.body and f"@{world.writer.login}" in pr.body
    assert st.task.pr_metadata is not None and st.task.pr_metadata["pull_request_number"] == 17
    assert st.artifact.published_at is not None and st.artifact.pull_request_number == 17
    assert st.events[-1] == "JOB_COMPLETED"


@pytest.mark.asyncio
async def test_publication_details_show_the_exact_changes_to_readers_only(
    session: AsyncSession, client: AsyncClient, world: World
) -> None:
    task_id, artifact = await waiting_task(session, world)
    res = await client.get(f"/api/v1/tasks/{task_id}/publication", headers=world.reader.headers)
    assert res.status_code == 200
    body = res.json()
    assert body["artifact_digest"] == artifact["digest"]
    assert body["files"] == artifact["files"]
    assert body["approval_status"] == ApprovalStatusEnum.PENDING.value and body["published"] is False
    no_access = await client.get(f"/api/v1/tasks/{task_id}/publication", headers=world.outsider.headers)
    assert no_access.status_code == 404


# ── Boundary re-checks inside publish() ────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_refuses_an_artifact_that_does_not_match_its_digest(gh: FakeGitHub) -> None:
    artifact = {**make_artifact("o/r"), "created_ts": 1_700_000_000}
    artifact["files"] = [{"path": "src/widget.py", "action": "write", "content": "evil\n"}]
    with pytest.raises(GitHubExecutionError):
        await GitHubExecutionService().publish("t1", "d", None, artifact, approved_by="a")
    gh.git_cls.assert_not_called()
    gh.assert_nothing_published()


@pytest.mark.asyncio
async def test_publish_refuses_github_paths_even_when_stored_and_approved(gh: FakeGitHub) -> None:
    files = [{"path": ".github/workflows/ci.yml", "action": "write", "content": "on: push\n"}]
    artifact = {**make_artifact("o/r", files), "created_ts": 1_700_000_000}
    with pytest.raises(GitHubExecutionError, match="dangerous"):
        await GitHubExecutionService().publish("t1", "d", None, artifact, approved_by="a")
    gh.assert_nothing_published()


@pytest.mark.asyncio
async def test_publish_refuses_when_a_base_symlink_redirects_files_into_github(gh: FakeGitHub) -> None:
    """A symlink in the target repository cannot turn an approved path into .github/**."""

    def clone(owner: str, repo: str) -> None:
        ws = gh.workspace()
        (ws / ".github" / "workflows").mkdir(parents=True)
        (ws / "docs").symlink_to(ws / ".github")

    gh.git.clone.side_effect = clone
    files = [{"path": "docs/workflows/ci.yml", "action": "write", "content": "on: push\n"}]
    artifact = {**make_artifact("o/r", files), "created_ts": 1_700_000_000}
    with pytest.raises(GitHubExecutionError, match="differ from the approved"):
        await GitHubExecutionService().publish("t1", "d", None, artifact, approved_by="a")
    gh.assert_nothing_published()


# ── Failure between approval and publication (property 14) ─────────────────────


@pytest.mark.asyncio
async def test_transient_push_failure_is_retried_under_the_same_approval(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id, artifact = await waiting_task(session, world)
    assert (await approve(client, world.writer, task_id, artifact["digest"])).status_code == 200
    gh.git.push.side_effect = [GitError("Git command failed: push"), None]

    [job_id] = await run_pending_jobs(maker, task_id)
    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.PUBLISHING.value
    assert st.jobs[-1].status == JobStatusEnum.PENDING.value and st.jobs[-1].attempt_count == 1
    gh.client.create_pull_request.assert_not_awaited()

    await process_job(job_id)  # what recovery's re-enqueue leads to
    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.COMPLETED.value
    assert gh.git.push.call_count == 2
    gh.client.create_pull_request.assert_awaited_once()
    # Both attempts produced the same commit input: same files, same fixed date.
    assert gh.commits[0] == gh.commits[1]


@pytest.mark.asyncio
async def test_publication_fails_once_its_attempts_are_exhausted(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id, artifact = await waiting_task(session, world)
    assert (await approve(client, world.writer, task_id, artifact["digest"])).status_code == 200
    await session.execute(
        update(Job).where(Job.task_id == task_id, Job.status == JobStatusEnum.PENDING.value)
        .values(attempt_count=MAX_JOB_ATTEMPTS - 1)
    )
    await session.commit()
    gh.git.push.side_effect = GitError("Git command failed: push")

    await run_pending_jobs(maker, task_id)
    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.FAILED.value
    assert st.task.failure_code == "PUBLICATION_FAILED"
    assert st.events[-1] == "JOB_FAILED"
    gh.client.create_pull_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_after_an_interrupted_publication_reuses_the_open_pull_request(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id, artifact = await waiting_task(session, world)
    assert (await approve(client, world.writer, task_id, artifact["digest"])).status_code == 200
    # An earlier attempt opened the pull request but died before recording it.
    gh.client.find_open_pull_request.return_value = MagicMock(number=99, html_url="https://github.com/o/r/pull/99")

    await run_pending_jobs(maker, task_id)
    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.COMPLETED.value
    assert st.task.pr_metadata is not None and st.task.pr_metadata["pull_request_number"] == 99
    gh.client.create_pull_request.assert_not_awaited()


def _git(cwd: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_DATE": "@1600000000 +0000", "GIT_COMMITTER_DATE": "@1600000000 +0000",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin"}  # fmt: skip
    return subprocess.run(  # nosec B603 B607
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, env=env, check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_publication_commit_is_deterministic_across_retries(tmp_path: Path) -> None:
    shas = []
    for name in ("attempt1", "attempt2"):
        repo = tmp_path / name
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        (repo / "README.md").write_text("base\n")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-q", "-m", "base")
        (repo / "src.py").write_text("fixed\n")
        shas.append(SafeGitWrapper(repo, "unused-token").commit_files(["src.py"], "CodeForge: fix", commit_timestamp=1_700_000_000))
    assert shas[0] == shas[1]


# ── Plan approval is not publication approval (properties 13, 15) ──────────────


@pytest.mark.asyncio
async def test_plan_approval_endpoint_cannot_approve_publication(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id, _ = await waiting_task(session, world)
    res = await client.post(f"/api/v1/tasks/{task_id}/approve", json={}, headers=world.writer.headers)
    assert res.status_code >= 400
    await run_pending_jobs(maker, task_id)
    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.WAITING_PUBLICATION.value
    assert st.approval is not None and st.approval.status == ApprovalStatusEnum.PENDING.value
    gh.assert_nothing_published()


@pytest.mark.asyncio
async def test_approved_plan_still_requires_publication_approval(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    from tests.test_phase10a_approvals import _sample_plan

    task = await TaskService(session).create_task(
        TaskRequest(execution_target=ExecutionTarget.github, github_repository=world.repo,
                    description="Fix the widget calculation", require_plan_approval=True),  # fmt: skip
        creator_id=world.writer.user.id,
    )
    await session.commit()
    task_id = task.task_id
    plan_result = generation_result(task_id, None).model_copy(
        update={"full_plan": _sample_plan().model_dump(), "agent_output": "Plan ready."}
    )
    with patch("app.services.task_service.TaskService.execute_task", new=AsyncMock(return_value=plan_result)):
        await run_pending_jobs(maker, task_id)
    assert (await state(maker, task_id)).task.status == TaskStatusEnum.WAITING_APPROVAL.value

    # Publication endpoints do not act on a plan approval.
    plan_digest = "0" * 64
    assert (await approve(client, world.writer, task_id, plan_digest)).status_code == 409
    res = await client.post(f"/api/v1/tasks/{task_id}/approve", json={}, headers=world.writer.headers)
    assert res.status_code == 200

    artifact = make_artifact(world.repo)
    with patch("app.services.task_service.TaskService.execute_task",
               new=AsyncMock(return_value=generation_result(task_id, artifact))):  # fmt: skip
        await run_pending_jobs(maker, task_id)

    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.WAITING_PUBLICATION.value
    assert st.approval is not None and st.approval.status == ApprovalStatusEnum.PENDING.value
    async with maker() as s:
        plan_approval = await TaskRepository(s).get_latest_approval(task_id)
    assert plan_approval is not None
    assert plan_approval.approval_type != PUBLICATION_APPROVAL
    assert plan_approval.status == ApprovalStatusEnum.APPROVED.value
    gh.assert_nothing_published()
