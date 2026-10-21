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
    TaskGitHubLink,
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
from app.github.exceptions import GitHubAuthorizationError, GitHubTimeoutError
from app.github.git import GitError, SafeGitWrapper
from app.main import create_app
from app.schemas.task import ExecutionTarget, TaskRequest, TaskResult, TaskStatus
from app.services.auth_service import AuthService
from app.services.authorization_service import set_collaborator_permission_resolver
from app.services.task_service import TaskService
from app.worker import _request_publication_approval, process_job
from tests.conftest import FakeInstallationTokens, RepositoryIdentityDouble, repository_access_for

BASE_SHA = "b" * 40
REPOSITORY_ID = 31337
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


class FakePermissionCache:
    """In-memory stand-in for Redis, so permission caching behaves the same with or without a server."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


@pytest_asyncio.fixture(autouse=True)
async def permission_cache() -> Any:
    """Isolate Redis use: a fake permission cache, and no process-wide Redis
    client left bound to this test's event loop once it closes."""
    import app.services.queue_service as queue_service

    cache = FakePermissionCache()
    queue_service._redis_client = None
    with patch("app.services.authorization_service.get_redis_client", return_value=cache):
        yield cache
    client, queue_service._redis_client = queue_service._redis_client, None
    if client is not None:
        try:
            await client.aclose()
        except Exception:  # nosec B110 - best-effort cleanup of a test client
            pass


@pytest.fixture(autouse=True)
def execution_available() -> Any:
    """Generation is mocked in this module; a permitted test executor only has to exist.

    Without one, an approved GitHub plan stops before coding
    (tests/security/test_github_plan_gate.py).
    """
    with patch("app.execution.test_executor.get_test_executor", return_value=object()):
        yield


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
    tokens: FakeInstallationTokens
    client_cls: MagicMock
    identity: RepositoryIdentityDouble
    base_files: dict[str, str] = field(default_factory=dict)
    commits: list[dict[str, Any]] = field(default_factory=list)

    @property
    def git(self) -> MagicMock:
        return self.git_cls.return_value

    def workspace(self) -> Path:
        return Path(self.git_cls.call_args.args[0])

    def assert_nothing_published(self) -> None:
        # No publication-capable credential was even minted (CF-06).
        assert self.tokens.write_scopes() == []
        for call in self.client_cls.for_installation.call_args_list:
            assert "write" not in dict(call.kwargs["scope"].permissions).values()
        self.client.create_branch.assert_not_awaited()
        self.git.commit_files.assert_not_called()
        self.git.push.assert_not_called()
        self.client.find_open_pull_request.assert_not_awaited()
        self.client.create_pull_request.assert_not_awaited()


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: Any) -> Any:
    clones = tmp_path / "clones"
    clones.mkdir()
    monkeypatch.setattr(settings, "codeforge_github_clone_root", str(clones))

    client = MagicMock()
    client.aclose = AsyncMock()
    client.create_branch = AsyncMock()
    client.find_open_pull_request = AsyncMock(return_value=None)
    client.create_pull_request = AsyncMock(
        return_value=MagicMock(number=17, html_url="https://github.com/o/r/pull/17")
    )
    git_cls = MagicMock()
    client_cls = MagicMock()
    client_cls.for_installation.return_value = client
    tokens = FakeInstallationTokens()
    # GitHub's answers to the repository-identity check in resolve_repository_access.
    identity = RepositoryIdentityDouble(world.repo, repository_id=REPOSITORY_ID)
    fake = FakeGitHub(client=client, git_cls=git_cls, tokens=tokens, client_cls=client_cls, identity=identity)

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
        patch("app.execution.github_execution.GitHubClient", client_cls),
        patch("app.execution.github_execution.get_installation_token_service", return_value=tokens),
        identity.patches(),
        patch("app.execution.github_execution.SafeGitWrapper", git_cls),
        patch("app.services.task_service.Orchestrator"),
    ):
        yield fake


# ── Helpers ────────────────────────────────────────────────────────────────────


async def allow() -> None:
    """An authorize callback that permits publication (for direct publish() tests)."""


def make_artifact(repo: str, files: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    files = sorted(files or DEFAULT_FILES, key=lambda f: f["path"])
    artifact: dict[str, Any] = {
        "repository": repo,
        "base_branch": "main",
        "base_sha": BASE_SHA,
        "commit_message": "CodeForge: Fix the widget calculation",
        "pull_request_title": "CodeForge: Fix the widget calculation",
        "pull_request_body": "## CodeForge AI\n\n**Agent Output:**\n```\nImplemented the fix.\n```\n",
        "files": files,
        "total_bytes": sum(len(f["content"].encode()) for f in files if f["content"] is not None),
    }
    artifact["digest"] = artifact_digest(artifact)
    return artifact


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
    """A GitHub task whose plan was approved, with its generation job pending.

    Every GitHub task's plan must be approved before any code is written, so
    generation always resumes from an approved plan
    (tests/security/test_github_plan_gate.py covers the gate itself).
    """
    from tests.test_phase10a_approvals import _sample_plan

    task = Task(**{
        "task_id": str(uuid.uuid4()),
        "status": TaskStatusEnum.QUEUED.value,
        "execution_target": "github",
        "repository": world.repo,
        "requested_task": "Fix the widget calculation",
        "creator_id": world.writer.user.id,
        "implementation_plan": _sample_plan().model_dump(),
        "approval_config": {"require_plan_approval": True},
        **fields,
    })
    session.add(task)
    await session.flush()
    session.add(TaskApproval(task_id=task.task_id, approval_type="plan", status=ApprovalStatusEnum.APPROVED.value,
                             requested_by="worker", approved_by=world.writer.login))  # fmt: skip
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
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub,
    permission_cache: FakePermissionCache,
) -> None:
    task_id, artifact = await waiting_task(session, world)
    assert (await approve(client, world.writer, task_id, artifact["digest"])).status_code == 200
    # The approval's write check cached "write" (5 minutes); the publication
    # check must not trust that cache.
    assert "write" in permission_cache.values.values()
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
        await GitHubExecutionService(repository_access_for("o/r")).publish("t1", artifact, approved_by="a", authorize=allow)
    gh.git_cls.assert_not_called()
    gh.assert_nothing_published()


@pytest.mark.asyncio
async def test_publish_refuses_github_paths_even_when_stored_and_approved(gh: FakeGitHub) -> None:
    files = [{"path": ".github/workflows/ci.yml", "action": "write", "content": "on: push\n"}]
    artifact = {**make_artifact("o/r", files), "created_ts": 1_700_000_000}
    with pytest.raises(GitHubExecutionError, match="dangerous"):
        await GitHubExecutionService(repository_access_for("o/r")).publish("t1", artifact, approved_by="a", authorize=allow)
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
        await GitHubExecutionService(repository_access_for("o/r")).publish("t1", artifact, approved_by="a", authorize=allow)
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


# ── Review findings (adversarial review of 8fc8603) ────────────────────────────


@pytest.mark.asyncio
async def test_published_pull_request_text_is_exactly_what_was_reviewed(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    """The PR title/body/commit message are agent-influenced and reach CI
    (``github.event.pull_request.body``), so they are part of the approved
    artifact: shown for review, bound by the digest, published verbatim."""
    task_id, artifact = await waiting_task(session, world)
    shown = (await client.get(f"/api/v1/tasks/{task_id}/publication", headers=world.writer.headers)).json()
    assert shown["pull_request_title"] == artifact["pull_request_title"]
    assert shown["pull_request_body"] == artifact["pull_request_body"]
    assert shown["commit_message"] == artifact["commit_message"]
    assert (await approve(client, world.writer, task_id, artifact["digest"])).status_code == 200
    # Text stored elsewhere on the task changes after approval; it must not leak in.
    await session.execute(
        update(Task).where(Task.task_id == task_id).values(final_message="$(curl evil) unreviewed")
    )
    await session.commit()

    await run_pending_jobs(maker, task_id)
    pr = gh.client.create_pull_request.await_args.args[0]
    assert pr.title == artifact["pull_request_title"]
    assert pr.body.startswith(artifact["pull_request_body"])
    assert "unreviewed" not in pr.body
    assert gh.git.commit_files.call_args.args[1] == artifact["commit_message"]


@pytest.mark.parametrize("field_name", ["pull_request_title", "pull_request_body", "commit_message", "base_branch"])
@pytest.mark.asyncio
async def test_publication_text_and_base_branch_changed_after_approval_are_not_published(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub, field_name: str
) -> None:
    task_id, artifact = await waiting_task(session, world)
    assert (await approve(client, world.writer, task_id, artifact["digest"])).status_code == 200
    await session.execute(
        update(TaskPublicationArtifact)
        .where(TaskPublicationArtifact.task_id == task_id)
        .values({field_name: "tampered"})
    )
    await session.commit()

    await run_pending_jobs(maker, task_id)
    assert (await state(maker, task_id)).task.status == TaskStatusEnum.FAILED.value
    gh.assert_nothing_published()


@pytest.mark.asyncio
async def test_expired_publication_request_is_closed_by_recovery_without_any_call(
    session: AsyncSession, maker: Any, world: World, gh: FakeGitHub
) -> None:
    from app.worker import recover_jobs

    expired_id, _ = await waiting_task(session, world)
    fresh_id, _ = await waiting_task(session, world)
    await session.execute(
        update(TaskApproval)
        .where(TaskApproval.task_id == expired_id)
        .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await session.commit()

    class NoQueue:
        async def enqueue(self, job_id: int) -> None:
            raise AssertionError("expiry must not enqueue anything")

        async def is_queued(self, job_id: int) -> bool:
            return False

    await recover_jobs(NoQueue())  # type: ignore[arg-type]
    await recover_jobs(NoQueue())  # idempotent

    st = await state(maker, expired_id)
    assert st.task.status == TaskStatusEnum.CANCELLED.value
    assert st.approval is not None and st.approval.status == ApprovalStatusEnum.EXPIRED.value
    assert st.events.count("JOB_FAILED") == 1
    fresh = await state(maker, fresh_id)
    assert fresh.task.status == TaskStatusEnum.WAITING_PUBLICATION.value
    assert fresh.approval is not None and fresh.approval.status == ApprovalStatusEnum.PENDING.value
    await run_pending_jobs(maker, expired_id)
    gh.assert_nothing_published()


@pytest.mark.asyncio
async def test_content_that_cannot_be_stored_fails_generation_cleanly(
    session: AsyncSession, maker: Any, world: World, gh: FakeGitHub
) -> None:
    """A NUL character cannot be stored in JSONB; generation must fail closed, not strand the job."""
    task_id, job_id = await github_job(session, world)
    artifact = make_artifact(world.repo, [{"path": "a.txt", "action": "write", "content": "a\x00b"}])
    with patch("app.services.task_service.TaskService.execute_task",
               new=AsyncMock(return_value=generation_result(task_id, artifact))):  # fmt: skip
        await process_job(job_id)

    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.FAILED.value
    assert [j.status for j in st.jobs] == [JobStatusEnum.FAILED.value]
    assert st.approval is None and st.artifact is None
    gh.assert_nothing_published()


@pytest.mark.asyncio
async def test_concurrent_approve_and_reject_leave_one_coherent_outcome(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    import asyncio

    task_id, artifact = await waiting_task(session, world)
    approved, rejected = await asyncio.gather(
        approve(client, world.writer, task_id, artifact["digest"]),
        client.post(f"/api/v1/tasks/{task_id}/publication/reject", headers=world.writer.headers),
    )
    assert sorted([approved.status_code, rejected.status_code]) == [200, 409]
    st = await state(maker, task_id)
    assert st.approval is not None
    if approved.status_code == 200:
        assert (st.task.status, st.approval.status) == (TaskStatusEnum.PUBLISHING.value, ApprovalStatusEnum.APPROVED.value)
        assert len(st.jobs) == 2
    else:
        assert (st.task.status, st.approval.status) == (TaskStatusEnum.CANCELLED.value, ApprovalStatusEnum.REJECTED.value)
        assert len(st.jobs) == 1
        await run_pending_jobs(maker, task_id)
        gh.assert_nothing_published()


@pytest.mark.asyncio
async def test_concurrent_duplicate_approvals_create_one_publication_job(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World
) -> None:
    import asyncio

    task_id, artifact = await waiting_task(session, world)
    results = await asyncio.gather(*(approve(client, world.writer, task_id, artifact["digest"]) for _ in range(4)))
    assert [r.status_code for r in results] == [200] * 4
    st = await state(maker, task_id)
    assert [j.status for j in st.jobs] == [JobStatusEnum.SUCCEEDED.value, JobStatusEnum.PENDING.value]


@pytest.mark.asyncio
async def test_stray_job_after_rejection_does_not_publish(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id, _ = await waiting_task(session, world)
    res = await client.post(f"/api/v1/tasks/{task_id}/publication/reject", headers=world.writer.headers)
    assert res.status_code == 200
    session.add(Job(task_id=task_id, status=JobStatusEnum.PENDING.value))
    await session.commit()
    await run_pending_jobs(maker, task_id)
    assert (await state(maker, task_id)).task.status == TaskStatusEnum.CANCELLED.value
    gh.assert_nothing_published()


# ── Digest and artifact validation (unit) ──────────────────────────────────────


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("repository", "other/widgets"),
        ("base_branch", "release"),
        ("base_sha", "c" * 40),
        ("commit_message", "CodeForge: something else"),
        ("pull_request_title", "Different title"),
        ("pull_request_body", "Different body"),
        ("files", [{"path": "src/widget.py", "action": "write", "content": "def widget():\n    return 43\n"}]),
        ("files", [{"path": "src/widget.py", "action": "delete", "content": None}]),
        ("files", [{"path": "src/other.py", "action": "write", "content": "def widget():\n    return 42\n"}]),
    ],
)
def test_digest_covers_every_published_field(field_name: str, value: Any) -> None:
    artifact = make_artifact("o/r")
    assert artifact_digest({**artifact, field_name: value}) != artifact["digest"]


def test_digest_is_independent_of_key_order() -> None:
    artifact = make_artifact("o/r")
    reordered = dict(reversed(list(artifact.items())))
    reordered["files"] = [dict(reversed(list(f.items()))) for f in artifact["files"]]
    assert artifact_digest(reordered) == artifact["digest"]


@pytest.mark.parametrize(
    "files",
    [
        [],
        [{"path": "a.py", "action": "write"}],
        [{"path": "a.py", "action": "write", "content": None}],
        [{"path": "a.py", "action": "delete", "content": "x"}],
        [{"path": "a.py", "action": "chmod", "content": "x"}],
        [{"path": "", "action": "write", "content": "x"}],
        [{"path": 1, "action": "write", "content": "x"}],
        [{"path": "b.py", "action": "write", "content": "x"}, {"path": "a.py", "action": "write", "content": "x"}],
        [{"path": "a.py", "action": "write", "content": "x"}, {"path": "a.py", "action": "write", "content": "y"}],
        [{"path": "a.py", "action": "write", "content": "a\x00b"}],
        [{"path": "a\x00.py", "action": "write", "content": "x"}],
        [{"path": "a.py", "action": "write", "content": "x", "mode": "100755"}],
        ["a.py"],
    ],
)
def test_malformed_artifacts_are_rejected(files: Any) -> None:
    from app.execution.publication import PublicationError, verify_artifact

    artifact = {**make_artifact("o/r"), "files": files}
    with pytest.raises(PublicationError):
        verify_artifact(artifact, artifact_digest(artifact))


@pytest.mark.parametrize("field_name", ["commit_message", "pull_request_title", "pull_request_body", "base_branch"])
@pytest.mark.parametrize("bad", [None, 7, "a\x00b", "x" * 70_000])
def test_malformed_publication_text_is_rejected(field_name: str, bad: Any) -> None:
    from app.execution.publication import PublicationError, verify_artifact

    artifact = {**make_artifact("o/r"), field_name: bad}
    with pytest.raises(PublicationError):
        verify_artifact(artifact, "0" * 64)


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "src/../../outside.txt", "/abs.txt", "./src/a.py", "src//a.py", ".git/config", ".git/hooks/pre-push"],
)
@pytest.mark.asyncio
async def test_publish_refuses_paths_that_escape_or_are_not_canonical(gh: FakeGitHub, path: str) -> None:
    """Even with a valid digest, a stored path is written only where it says, inside the clone."""
    artifact = {**make_artifact("o/r", [{"path": path, "action": "write", "content": "x\n"}]), "created_ts": 1}
    with pytest.raises(GitHubExecutionError):
        await GitHubExecutionService(repository_access_for("o/r")).publish("t1", artifact, approved_by="a", authorize=allow)
    clones = Path(settings.codeforge_github_clone_root)
    assert not (clones / "outside.txt").exists() and not (clones.parent / "outside.txt").exists()
    gh.assert_nothing_published()


@pytest.mark.asyncio
async def test_a_failing_expiry_never_blocks_job_recovery(
    session: AsyncSession, maker: Any, world: World
) -> None:
    from sqlalchemy import text

    from app.worker import recover_jobs

    task_id, _ = await waiting_task(session, world)
    await session.execute(
        update(TaskApproval).where(TaskApproval.task_id == task_id)
        .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    other_id, other_job = await github_job(session, world)
    await session.execute(
        update(Job).where(Job.id == other_job)
        .values(status=JobStatusEnum.RUNNING.value, worker_id="dead-worker", attempt_count=1)
    )
    await session.execute(text("UPDATE jobs SET available_at = now() - INTERVAL '1 hour' WHERE id = :id"), {"id": other_job})
    await session.commit()

    class Queue:
        enqueued: list[int] = []

        async def enqueue(self, job_id: int) -> None:
            self.enqueued.append(job_id)

        async def is_queued(self, job_id: int) -> bool:
            return False

    queue = Queue()
    with patch("app.services.task_service._expire_publication_request", side_effect=RuntimeError("boom")):
        result = await recover_jobs(queue)  # type: ignore[arg-type]

    assert result.released == [other_job] and queue.enqueued == [other_job]
    assert result.expired_publications == []
    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.WAITING_PUBLICATION.value  # retried on the next pass
    assert (await state(maker, other_id)).jobs[0].status == JobStatusEnum.PENDING.value


# ── Authorization re-checked at each remote side effect ────────────────────────
# The write-access check before publish() is not enough on its own: cloning
# can take a long time, and permission may be revoked meanwhile.  Each test
# revokes access at a controlled point (a fake GitHub step) and asserts that
# no later remote side effect happens.  GitHub permissions can still change
# right after any check; these checks minimize that window, they do not make
# authorization atomic.


async def _approved_task(session: AsyncSession, client: AsyncClient, world: World) -> str:
    task_id, artifact = await waiting_task(session, world)
    assert (await approve(client, world.writer, task_id, artifact["digest"])).status_code == 200
    return task_id


def _revoke(world: World, original: Any = None) -> Any:
    def side_effect(*args: Any, **kwargs: Any) -> Any:
        world.perms[world.writer.login] = "read"
        return original(*args, **kwargs) if original is not None else None
    return side_effect


@pytest.mark.asyncio
async def test_permission_revoked_during_clone_stops_publication_before_any_remote_change(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub,
    permission_cache: FakePermissionCache,
) -> None:
    task_id = await _approved_task(session, client, world)
    gh.git.clone.side_effect = _revoke(world, gh.git.clone.side_effect)
    assert "write" in permission_cache.values.values()  # a cached read would allow it

    await run_pending_jobs(maker, task_id)

    gh.git.clone.assert_called_once()
    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.FAILED.value
    assert st.task.failure_code == "PUBLICATION_FAILED"
    assert "write access" in (st.task.failure_reason or "")
    assert st.jobs[-1].status == JobStatusEnum.FAILED.value  # refused, not retried
    gh.assert_nothing_published()


@pytest.mark.asyncio
async def test_approver_deactivated_during_clone_stops_publication(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id = await _approved_task(session, client, world)
    original_clone = gh.git.clone.side_effect

    def clone_then_deactivate(owner: str, repo: str) -> None:
        import asyncio
        import threading

        original_clone(owner, repo)
        # clone runs synchronously inside the worker's event loop; commit the
        # deactivation from another thread, with its own loop and connection.
        t = threading.Thread(target=lambda: asyncio.run(_deactivate_via_new_engine(world.writer.user.id)))
        t.start()
        t.join()

    gh.git.clone.side_effect = clone_then_deactivate
    await run_pending_jobs(maker, task_id)

    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.FAILED.value
    assert "no longer active" in (st.task.failure_reason or "")
    gh.assert_nothing_published()


async def _deactivate_via_new_engine(user_id: str) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from tests.conftest import TEST_DB_URL

    engine = create_async_engine(TEST_DB_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(update(User).where(User.id == user_id).values(is_active=False))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_permission_revoked_after_branch_creation_stops_push_and_pull_request(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id = await _approved_task(session, client, world)
    gh.client.create_branch.side_effect = _revoke(world)

    await run_pending_jobs(maker, task_id)

    gh.client.create_branch.assert_awaited_once()
    assert (await state(maker, task_id)).task.status == TaskStatusEnum.FAILED.value
    gh.git.push.assert_not_called()
    gh.client.find_open_pull_request.assert_not_awaited()
    gh.client.create_pull_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_permission_revoked_after_push_stops_pull_request(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id = await _approved_task(session, client, world)
    gh.git.push.side_effect = _revoke(world)

    await run_pending_jobs(maker, task_id)

    gh.git.push.assert_called_once()
    assert (await state(maker, task_id)).task.status == TaskStatusEnum.FAILED.value
    gh.client.find_open_pull_request.assert_not_awaited()
    gh.client.create_pull_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_requires_an_authorization_check(gh: FakeGitHub) -> None:
    """publish() has no default that skips authorization."""
    import inspect

    params = inspect.signature(GitHubExecutionService.publish).parameters
    assert "authorize" in params
    assert params["authorize"].default is inspect.Parameter.empty


@pytest.mark.asyncio
async def test_publish_checks_authorization_immediately_before_each_remote_step(gh: FakeGitHub) -> None:
    events: list[str] = []

    async def authorize() -> None:
        events.append("authorize")

    def record(name: str, result: Any = None) -> Any:
        def side_effect(*args: Any, **kwargs: Any) -> Any:
            events.append(name)
            return result
        return side_effect

    original_clone = gh.git.clone.side_effect
    gh.git.clone.side_effect = lambda *a: (events.append("clone"), original_clone(*a))[1]
    gh.client.create_branch.side_effect = record("create_branch")
    gh.git.push.side_effect = record("push")
    gh.client.find_open_pull_request.side_effect = record("find_open_pull_request")
    gh.client.create_pull_request.side_effect = record(
        "create_pull_request", MagicMock(number=5, html_url="https://github.com/o/r/pull/5")
    )
    artifact = {**make_artifact("o/r"), "created_ts": 1}

    await GitHubExecutionService(repository_access_for("o/r")).publish("t1", artifact, approved_by="a", authorize=authorize)

    assert events == ["clone", "authorize", "create_branch", "authorize", "push",
                      "authorize", "find_open_pull_request", "create_pull_request"]  # fmt: skip



# ── Repository-scoped credentials during publication (CF-06) ───────────────────


@pytest.mark.asyncio
async def test_write_credentials_exist_only_after_approval_and_reauthorization(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    from app.github.repository_access import GENERATION_PERMISSIONS, PUBLICATION_PERMISSIONS
    from app.github.token_service import TokenScope

    events: list[str] = []
    original_get_token = gh.tokens.get_token

    async def recording_get_token(installation_id: int, scope: Any) -> str:
        events.append("token:" + ("write" if "write" in dict(scope.permissions).values() else "read"))
        return await original_get_token(installation_id, scope)

    gh.tokens.get_token = recording_get_token  # type: ignore[method-assign]
    gh.client_cls.for_installation.side_effect = lambda *a, **k: (events.append("write-client"), gh.client)[1]
    gh.git.clone.side_effect = lambda *a: (events.append("clone"), None)[1]
    gh.client.create_branch.side_effect = lambda *a, **k: events.append("create_branch")
    gh.git.set_token.side_effect = lambda token: events.append("set_token")
    gh.git.push.side_effect = lambda *a: events.append("push")

    task_id = await _approved_task(session, client, world)
    assert gh.tokens.minted == []  # approval alone mints nothing
    await run_pending_jobs(maker, task_id)

    assert (await state(maker, task_id)).task.status == TaskStatusEnum.COMPLETED.value
    # The clone uses a read-only token; the write client and the write token
    # come only after the CF-05 checks, right before the change they make.
    assert events[:2] == ["token:read", "clone"]
    assert events.index("write-client") < events.index("create_branch")
    assert events.index("token:write") < events.index("set_token") < events.index("push")
    access_id = REPOSITORY_ID
    read_scope = TokenScope.for_repository_id(access_id, GENERATION_PERMISSIONS)
    write_scope = TokenScope.for_repository_id(access_id, PUBLICATION_PERMISSIONS)
    assert gh.git_cls.call_args.args[1] == FakeInstallationTokens.token_for(read_scope)
    gh.git.set_token.assert_called_once_with(FakeInstallationTokens.token_for(write_scope))
    assert {scope for _, scope in gh.tokens.minted} == {read_scope, write_scope}
    [client_call] = gh.client_cls.for_installation.call_args_list
    assert client_call.kwargs["scope"] == write_scope


@pytest.mark.parametrize(
    ("error", "retried"),
    [(GitHubTimeoutError("slow"), True), (GitHubAuthorizationError("app lacks contents:write"), False)],
)
@pytest.mark.asyncio
async def test_write_token_failure_never_pushes_and_never_falls_back(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch, error: Exception, retried: bool,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_static_token_must_not_be_used")
    original_get_token = gh.tokens.get_token

    async def failing_write(installation_id: int, scope: Any) -> str:
        if "write" in dict(scope.permissions).values():
            raise error
        return await original_get_token(installation_id, scope)

    gh.tokens.get_token = failing_write  # type: ignore[method-assign]
    task_id = await _approved_task(session, client, world)
    await run_pending_jobs(maker, task_id)

    st = await state(maker, task_id)
    gh.git.push.assert_not_called()
    gh.client.create_pull_request.assert_not_awaited()
    if retried:
        assert st.task.status == TaskStatusEnum.PUBLISHING.value
        assert st.jobs[-1].status == JobStatusEnum.PENDING.value
    else:
        assert st.task.status == TaskStatusEnum.FAILED.value
        assert "credential" in (st.task.failure_reason or "")


@pytest.mark.asyncio
async def test_publication_refused_when_the_repository_is_no_longer_authorized(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub
) -> None:
    task_id = await _approved_task(session, client, world)
    await session.execute(
        update(GitHubInstallationRepository)
        .where(GitHubInstallationRepository.repository == world.repo)
        .values(authorized=False)
    )
    await session.commit()
    # Approver checks pass (resolver override); repository resolution must still refuse.
    with patch("app.worker._approver_refusal", new=AsyncMock(return_value=None)):
        await run_pending_jobs(maker, task_id)
    st = await state(maker, task_id)
    assert st.task.status == TaskStatusEnum.FAILED.value
    assert "not authorized" in (st.task.failure_reason or "")
    assert gh.tokens.minted == []
    gh.assert_nothing_published()


@pytest.mark.parametrize(("signed_repository_id", "published"), [(REPOSITORY_ID, True), (REPOSITORY_ID + 1, False)])
@pytest.mark.asyncio
async def test_github_triggered_publication_targets_only_the_signed_repository(
    session: AsyncSession, maker: Any, client: AsyncClient, world: World, gh: FakeGitHub,
    signed_repository_id: int, published: bool,
) -> None:
    # A task started by a webhook names the installation and repository ID
    # GitHub signed.  If the name now denotes another repository (renamed
    # away and replaced), nothing may be published to it.
    task_id = await _approved_task(session, client, world)
    installation_id = (
        await session.execute(
            select(GitHubInstallationRepository.installation_id).where(GitHubInstallationRepository.repository == world.repo)
        )
    ).scalar_one()
    session.add(TaskGitHubLink(
        task_id=task_id, installation_id=installation_id, repository_id=signed_repository_id,
        repository_full_name=world.repo, issue_number=3, triggering_github_user_id=1, triggering_github_login="dev",
    ))  # fmt: skip
    await session.commit()

    await run_pending_jobs(maker, task_id)

    st = await state(maker, task_id)
    if published:
        assert st.task.status == TaskStatusEnum.COMPLETED.value
        gh.git.push.assert_called_once()
    else:
        assert st.task.status == TaskStatusEnum.FAILED.value
        assert "named by its GitHub event" in (st.task.failure_reason or "")
        assert gh.tokens.minted == []
        gh.assert_nothing_published()
