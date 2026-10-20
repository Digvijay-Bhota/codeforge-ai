"""CF-03 regression tests: local execution is limited to trusted identities and their own workspaces.

Before this fix any GitHub account could log in and, with
CODEFORGE_ALLOW_LOCAL_EXECUTION=true, point the coding agent at any directory
under WORKSPACE_ROOT (including another user's workspace) and, under
host_unsafe, have repository-controlled code run on the worker host.  GitHub
clones also lived under WORKSPACE_ROOT.

These tests exercise the real boundaries end to end where possible: HTTP
routes with real session tokens and ``get_current_user``, the database, the
worker's ``process_job`` and ``GitHubExecutionService``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import ExecutionMode, Settings, settings
from app.db.models import GitHubIdentity, Job, JobStatusEnum, Task, TaskStatusEnum, User
from app.db.session import get_db_session
from app.execution.github_execution import GitHubExecutionService
from app.execution.local_policy import (
    LocalExecutionError,
    LocalExecutionForbiddenError,
    github_clone_root,
    require_trusted_host_identity,
    resolve_local_workspace,
)
from app.main import create_app
from app.schemas.task import (
    ExecutionTarget,
    GitHubFailureStage,
    TaskRequest,
    TaskResult,
    TaskStatus,
)
from app.services.auth_service import AuthService
from app.services.task_service import TaskService
from app.worker import process_job
from tests.conftest import LOCAL_TEST_GITHUB_ID

TRUSTED_A = LOCAL_TEST_GITHUB_ID
TRUSTED_B = LOCAL_TEST_GITHUB_ID + 1
UNTRUSTED = LOCAL_TEST_GITHUB_ID + 2


# ── Fixtures and helpers ───────────────────────────────────────────────────────


@pytest.fixture
def roots(local_workspace_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Local execution on; TRUSTED_A and TRUSTED_B trusted, each with a workspace."""
    monkeypatch.setattr(settings, "codeforge_local_execution_github_ids", [TRUSTED_A, TRUSTED_B])
    for github_id in (TRUSTED_A, TRUSTED_B):
        (local_workspace_root / str(github_id) / "repo").mkdir(parents=True, exist_ok=True)
    (local_workspace_root / str(UNTRUSTED) / "repo").mkdir(parents=True)
    return local_workspace_root


def ws(root: Path, github_id: int) -> str:
    return str(root / str(github_id) / "repo")


async def make_user(session: AsyncSession, github_id: int, *, active: bool = True) -> User:
    user = User(id=str(uuid.uuid4()), display_name=f"user-{github_id}", is_active=active)
    session.add(user)
    await session.flush()
    session.add(GitHubIdentity(user_id=user.id, github_user_id=github_id, github_login=f"u{github_id}"))
    await session.commit()
    return user


def bearer(user: User, github_id: int) -> dict[str, str]:
    token = AuthService().create_session_jwt(user, github_user_id=github_id, github_login=f"u{github_id}")
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> Any:
    app = create_app()

    async def same_session() -> Any:
        yield session

    app.dependency_overrides[get_db_session] = same_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def local_task(session: AsyncSession, path: str, creator_id: str | None, **extra: Any) -> Task:
    """Insert a local task directly (as anything bypassing the API could)."""
    task = Task(
        task_id=str(uuid.uuid4()),
        status=extra.pop("status", TaskStatusEnum.FAILED.value),
        requested_task="Fix the add function",
        execution_target="local",
        workspace_path=path,
        creator_id=creator_id,
        **extra,
    )
    session.add(task)
    await session.commit()
    return task


async def task_count(session: AsyncSession) -> int:
    return len((await session.execute(select(Task))).scalars().all())


def body(path: str) -> dict[str, str]:
    return {"execution_target": "local", "workspace_path": path, "description": "Fix the add function"}


# ── Settings: defaults and allow-list validation ───────────────────────────────


def test_defaults_trust_nobody_and_keep_local_execution_off(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CODEFORGE_ALLOW_LOCAL_EXECUTION", "CODEFORGE_LOCAL_EXECUTION_GITHUB_IDS",
                 "CODEFORGE_GITHUB_CLONE_ROOT", "APP_ENV"):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    loaded = Settings(_env_file=None)
    assert loaded.codeforge_allow_local_execution is False
    assert loaded.codeforge_local_execution_github_ids == []
    assert loaded.codeforge_test_execution_mode == ExecutionMode.DISABLED


@pytest.mark.parametrize(
    "raw",
    # Booleans, numeric strings and floats must not be coerced: [true] would
    # otherwise silently trust GitHub user ID 1.
    ["alice", "123,456", "[-5]", "[0]", '["octocat"]', "{}", "[true]", '["123"]', "[1.0]", "[1e3]"],
)
def test_malformed_allow_list_fails_at_startup(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("CODEFORGE_LOCAL_EXECUTION_GITHUB_IDS", raw)
    with pytest.raises((ValidationError, ValueError)):
        Settings(_env_file=None)


def test_well_formed_allow_list_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEFORGE_LOCAL_EXECUTION_GITHUB_IDS", "[101, 202]")
    assert Settings(_env_file=None).codeforge_local_execution_github_ids == [101, 202]


@pytest.mark.parametrize("trusted", [[], [TRUSTED_B]], ids=["empty", "other-id"])
@pytest.mark.parametrize("candidate", [None, TRUSTED_A])
def test_identity_check_fails_closed(
    monkeypatch: pytest.MonkeyPatch, trusted: list[int], candidate: int | None
) -> None:
    monkeypatch.setattr(settings, "codeforge_local_execution_github_ids", trusted)
    with pytest.raises(LocalExecutionForbiddenError):
        require_trusted_host_identity(candidate)


@pytest.mark.parametrize("app_env", [None, "production", "staging", "prod", ""])
def test_production_like_environments_cannot_enable_host_execution(
    monkeypatch: pytest.MonkeyPatch, app_env: str | None
) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    kwargs: dict[str, Any] = {"_env_file": None, "codeforge_test_execution_mode": "host_unsafe",
                              "codeforge_local_execution_github_ids": [TRUSTED_A]}  # fmt: skip
    if app_env is not None:
        kwargs["app_env"] = app_env
    # Trusting an identity never loosens the environment rule.
    with pytest.raises(ValidationError, match="host_unsafe"):
        Settings(**kwargs)


# ── API: creating local tasks ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unauthenticated_request_cannot_create_local_task(
    client: AsyncClient, session: AsyncSession, roots: Path
) -> None:
    res = await client.post("/api/v1/tasks", json=body(ws(roots, TRUSTED_A)))
    assert res.status_code == 401
    assert await task_count(session) == 0


@pytest.mark.asyncio
async def test_authenticated_user_outside_allow_list_cannot_create_local_task(
    client: AsyncClient, session: AsyncSession, roots: Path
) -> None:
    user = await make_user(session, UNTRUSTED)
    # Even a path inside its own (pre-created) directory is refused.
    res = await client.post(
        "/api/v1/tasks", json=body(ws(roots, UNTRUSTED)), headers=bearer(user, UNTRUSTED)
    )
    assert res.status_code == 403
    assert "not permitted" in res.json()["detail"]
    assert await task_count(session) == 0


@pytest.mark.asyncio
async def test_untrusted_caller_learns_nothing_about_paths(
    client: AsyncClient, session: AsyncSession, roots: Path
) -> None:
    """Identity is checked before the path: existing and missing paths look identical."""
    user = await make_user(session, UNTRUSTED)
    existing = await client.post("/api/v1/tasks", json=body(ws(roots, TRUSTED_A)), headers=bearer(user, UNTRUSTED))
    missing = await client.post("/api/v1/tasks", json=body("/no/such/dir"), headers=bearer(user, UNTRUSTED))
    assert (existing.status_code, existing.json()) == (missing.status_code, missing.json())


@pytest.mark.asyncio
async def test_inactive_trusted_account_cannot_create_local_task(
    client: AsyncClient, session: AsyncSession, roots: Path
) -> None:
    user = await make_user(session, TRUSTED_A, active=False)
    res = await client.post("/api/v1/tasks", json=body(ws(roots, TRUSTED_A)), headers=bearer(user, TRUSTED_A))
    assert res.status_code == 401  # get_current_user rejects deactivated accounts
    assert await task_count(session) == 0


@pytest.mark.asyncio
async def test_trusted_user_creates_local_task_in_own_directory(
    client: AsyncClient, session: AsyncSession, roots: Path
) -> None:
    user = await make_user(session, TRUSTED_A)
    res = await client.post("/api/v1/tasks", json=body(ws(roots, TRUSTED_A)), headers=bearer(user, TRUSTED_A))
    assert res.status_code == 200
    task = (await session.execute(select(Task).where(Task.task_id == res.json()["task_id"]))).scalar_one()
    assert task.creator_id == user.id
    assert task.workspace_path == ws(roots, TRUSTED_A)


@pytest.mark.asyncio
async def test_trusted_user_cannot_target_another_users_directory(
    client: AsyncClient, session: AsyncSession, roots: Path
) -> None:
    user = await make_user(session, TRUSTED_A)
    for path in (
        ws(roots, TRUSTED_B),  # another trusted user's workspace
        str(roots),  # WORKSPACE_ROOT itself
        f"{roots}/{TRUSTED_A}/repo/../../{TRUSTED_B}/repo",  # traversal into it
    ):
        res = await client.post("/api/v1/tasks", json=body(path), headers=bearer(user, TRUSTED_A))
        assert res.status_code == 400, path
    assert await task_count(session) == 0


@pytest.mark.asyncio
async def test_trusted_user_still_needs_local_execution_enabled(
    client: AsyncClient, session: AsyncSession, roots: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "codeforge_allow_local_execution", False)
    user = await make_user(session, TRUSTED_A)
    res = await client.post("/api/v1/tasks", json=body(ws(roots, TRUSTED_A)), headers=bearer(user, TRUSTED_A))
    assert res.status_code == 403
    assert "CODEFORGE_ALLOW_LOCAL_EXECUTION" in res.json()["detail"]


@pytest.mark.asyncio
async def test_empty_allow_list_refuses_everyone(
    client: AsyncClient, session: AsyncSession, roots: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "codeforge_local_execution_github_ids", [])
    user = await make_user(session, TRUSTED_A)
    res = await client.post("/api/v1/tasks", json=body(ws(roots, TRUSTED_A)), headers=bearer(user, TRUSTED_A))
    assert res.status_code == 403
    assert await task_count(session) == 0


# ── API: reading, modifying and retrying other users' local tasks ──────────────


@pytest.mark.asyncio
async def test_user_cannot_see_or_touch_another_users_local_task(
    client: AsyncClient, session: AsyncSession, roots: Path
) -> None:
    owner = await make_user(session, TRUSTED_B)
    intruder = await make_user(session, TRUSTED_A)  # trusted, but not the owner
    task = await local_task(session, ws(roots, TRUSTED_B), owner.id)
    h = bearer(intruder, TRUSTED_A)
    tid = task.task_id

    for method, path in (
        ("GET", f"/api/v1/tasks/{tid}"),
        ("GET", f"/api/v1/tasks/{tid}/diff"),
        ("GET", f"/api/v1/tasks/{tid}/events"),
        ("POST", f"/api/v1/tasks/{tid}/approve"),
        ("POST", f"/api/v1/tasks/{tid}/reject"),
        ("POST", f"/api/v1/tasks/{tid}/retry"),
    ):
        res = await client.request(method, path, headers=h)
        assert res.status_code == 404, (method, path)
        assert res.json() == {"detail": "Task not found"}
        assert ws(roots, TRUSTED_B) not in res.text

    listed = await client.get("/api/v1/tasks", headers=h)
    assert tid not in listed.text
    assert await task_count(session) == 1  # no retry child was created

    mine = await client.get(f"/api/v1/tasks/{tid}", headers=bearer(owner, TRUSTED_B))
    assert mine.status_code == 200


@pytest.mark.asyncio
async def test_local_task_without_creator_belongs_to_nobody(
    client: AsyncClient, session: AsyncSession, roots: Path
) -> None:
    user = await make_user(session, TRUSTED_A)
    task = await local_task(session, ws(roots, TRUSTED_A), None)
    for method, path in (
        ("GET", f"/api/v1/tasks/{task.task_id}"),
        ("POST", f"/api/v1/tasks/{task.task_id}/retry"),
    ):
        res = await client.request(method, path, headers=bearer(user, TRUSTED_A))
        assert res.status_code == 404, path


@pytest.mark.asyncio
async def test_service_retry_requires_the_original_creator(
    session: AsyncSession, roots: Path
) -> None:
    owner = await make_user(session, TRUSTED_B)
    other = await make_user(session, TRUSTED_A)
    task = await local_task(session, ws(roots, TRUSTED_B), owner.id)
    service = TaskService(session)
    for creator_id in (other.id, None):
        with pytest.raises(Exception) as exc:
            await service.retry_task(task.task_id, creator_id=creator_id)
        assert getattr(exc.value, "status_code", None) == 403
    assert await task_count(session) == 1


@pytest.mark.asyncio
async def test_owner_removed_from_allow_list_cannot_retry_or_approve(
    client: AsyncClient, session: AsyncSession, roots: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await make_user(session, TRUSTED_B)
    failed = await local_task(session, ws(roots, TRUSTED_B), owner.id)
    waiting = await local_task(
        session, ws(roots, TRUSTED_B), owner.id, status=TaskStatusEnum.WAITING_APPROVAL.value
    )
    monkeypatch.setattr(settings, "codeforge_local_execution_github_ids", [TRUSTED_A])
    h = bearer(owner, TRUSTED_B)
    assert (await client.post(f"/api/v1/tasks/{failed.task_id}/retry", headers=h)).status_code == 403
    assert (await client.post(f"/api/v1/tasks/{waiting.task_id}/approve", headers=h)).status_code == 403
    # Rejecting runs nothing, so the owner may still cancel.
    rejected = await client.post(f"/api/v1/tasks/{waiting.task_id}/reject", headers=h)
    assert rejected.status_code not in (401, 403, 404)


@pytest.mark.asyncio
async def test_repository_task_authorization_is_unchanged(
    client: AsyncClient, session: AsyncSession
) -> None:
    user = await make_user(session, UNTRUSTED)
    task = Task(task_id=str(uuid.uuid4()), status=TaskStatusEnum.FAILED.value, requested_task="x",
                execution_target="github", repository="acme/widgets")  # fmt: skip
    session.add(task)
    await session.commit()
    h = bearer(user, UNTRUSTED)
    with patch("app.api.tasks.AuthorizationService.can_read_repository", new=AsyncMock(return_value=True)):
        assert (await client.get(f"/api/v1/tasks/{task.task_id}", headers=h)).status_code == 200
    with patch("app.api.tasks.AuthorizationService.can_read_repository", new=AsyncMock(return_value=False)):
        assert (await client.get(f"/api/v1/tasks/{task.task_id}", headers=h)).status_code == 404


@pytest.mark.asyncio
async def test_repository_field_does_not_open_a_local_task_to_collaborators(
    client: AsyncClient, session: AsyncSession, roots: Path
) -> None:
    """A local task is its creator's alone, even if a repository name is attached to it."""
    owner = await make_user(session, TRUSTED_A)
    collaborator = await make_user(session, UNTRUSTED)
    res = await client.post(
        "/api/v1/tasks",
        json={**body(ws(roots, TRUSTED_A)), "github_repository": "acme/widgets"},
        headers=bearer(owner, TRUSTED_A),
    )
    assert res.status_code == 200
    created = (await session.execute(select(Task).where(Task.task_id == res.json()["task_id"]))).scalar_one()
    assert created.repository is None
    # A legacy row that still carries a repository must not be exposed either.
    legacy = await local_task(session, ws(roots, TRUSTED_A), owner.id, repository="acme/widgets")

    h = bearer(collaborator, UNTRUSTED)
    with patch("app.api.tasks.AuthorizationService.can_read_repository", new=AsyncMock(return_value=True)), \
         patch("app.api.tasks.AuthorizationService.can_write_repository", new=AsyncMock(return_value=True)):  # fmt: skip
        for tid in (created.task_id, legacy.task_id):
            for method, path in (("GET", f"/api/v1/tasks/{tid}"), ("GET", f"/api/v1/tasks/{tid}/diff"),
                                 ("POST", f"/api/v1/tasks/{tid}/reject")):  # fmt: skip
                r = await client.request(method, path, headers=h)
                assert r.status_code == 404, (tid, method, path)
        listed = await client.get("/api/v1/tasks", params={"repository": "acme/widgets"}, headers=h)
    assert listed.status_code == 200
    assert listed.json()["tasks"] == []


# ── Worker: execution re-checks everything ─────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("creator", ["missing", "untrusted", "inactive", "other-owner-path"])
async def test_worker_refuses_unauthorized_local_job(
    session: AsyncSession, roots: Path, creator: str
) -> None:
    """A local task inserted around the API never reaches the agent or the workspace."""
    if creator == "missing":
        creator_id, path = None, ws(roots, TRUSTED_A)
    elif creator == "untrusted":
        creator_id, path = (await make_user(session, UNTRUSTED)).id, ws(roots, UNTRUSTED)
    elif creator == "inactive":
        creator_id, path = (await make_user(session, TRUSTED_A, active=False)).id, ws(roots, TRUSTED_A)
    else:  # trusted creator, but the stored path is another user's workspace
        creator_id, path = (await make_user(session, TRUSTED_A)).id, ws(roots, TRUSTED_B)
    task = await local_task(session, path, creator_id, status=TaskStatusEnum.PENDING.value)
    job = Job(task_id=task.task_id, status=JobStatusEnum.PENDING.value)
    session.add(job)
    await session.commit()

    maker = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
    run = AsyncMock()
    with patch("app.worker.async_session_maker", maker), patch(
        "app.orchestration.orchestrator.Orchestrator.run", new=run
    ):
        await process_job(job.id)

    run.assert_not_awaited()
    async with maker() as check:
        stored = await check.get(Task, task.task_id)
        assert stored is not None
        assert stored.status == TaskStatusEnum.FAILED.value
        assert stored.failure_reason


@pytest.mark.asyncio
async def test_worker_runs_trusted_owners_local_job(session: AsyncSession, roots: Path) -> None:
    owner = await make_user(session, TRUSTED_A)
    task = await local_task(session, ws(roots, TRUSTED_A), owner.id, status=TaskStatusEnum.PENDING.value)
    job = Job(task_id=task.task_id, status=JobStatusEnum.PENDING.value)
    session.add(job)
    await session.commit()

    maker = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
    final = MagicMock(workflow_status="COMPLETED")
    with patch("app.worker.async_session_maker", maker), patch(
        "app.orchestration.orchestrator.Orchestrator.run", new=AsyncMock(return_value=final)
    ) as run, patch("app.services.task_service._map_to_task_result", return_value=TaskResult(
        task_id=task.task_id, status=TaskStatus.success, description="d", plan=[], changed_files=[],
        diff="", test_result=None, agent_output="",
    )):  # fmt: skip
        await process_job(job.id)

    run.assert_awaited_once()
    assert run.call_args.kwargs["workspace"].root == Path(ws(roots, TRUSTED_A))


# ── host_unsafe: only trusted creators' tasks run tests on the host ────────────


def _github_request() -> TaskRequest:
    return TaskRequest(execution_target=ExecutionTarget.github, github_repository="o/r",
                       description="Fix the add function")  # fmt: skip


@pytest.mark.asyncio
async def test_host_unsafe_refuses_github_task_from_untrusted_creator(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "codeforge_test_execution_mode", ExecutionMode.HOST_UNSAFE)
    monkeypatch.setattr(settings, "codeforge_local_execution_github_ids", [TRUSTED_A])
    user = await make_user(session, UNTRUSTED)
    with patch("app.execution.github_execution.GitHubExecutionService") as gh:
        result = await TaskService(session).execute_task(_github_request(), "t", creator_id=user.id)
    assert result.status == TaskStatus.error
    assert "not permitted" in (result.error_message or "")
    gh.assert_not_called()


@pytest.mark.asyncio
async def test_host_unsafe_runs_github_task_from_trusted_creator(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "codeforge_test_execution_mode", ExecutionMode.HOST_UNSAFE)
    monkeypatch.setattr(settings, "codeforge_local_execution_github_ids", [TRUSTED_A])
    user = await make_user(session, TRUSTED_A)
    with patch("app.execution.github_execution.GitHubExecutionService") as gh:
        gh.return_value.execute = AsyncMock(return_value="ran")
        assert await TaskService(session).execute_task(_github_request(), "t", creator_id=user.id) == "ran"


@pytest.mark.asyncio
async def test_isolated_modes_do_not_require_trust_for_github_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docker/gvisor/disabled keep repository code off the host, so GitHub tasks are unaffected."""
    monkeypatch.setattr(settings, "codeforge_local_execution_github_ids", [])
    for mode in (ExecutionMode.DISABLED, ExecutionMode.DOCKER, ExecutionMode.GVISOR):
        monkeypatch.setattr(settings, "codeforge_test_execution_mode", mode)
        with patch("app.execution.github_execution.GitHubExecutionService") as gh:
            gh.return_value.execute = AsyncMock(return_value="ran")
            assert await TaskService().execute_task(_github_request(), "t", creator_id=None) == "ran"


# ── Workspace and clone-root separation ────────────────────────────────────────


def test_owner_directory_symlinked_to_another_owner_is_rejected(roots: Path) -> None:
    import shutil

    shutil.rmtree(roots / str(TRUSTED_A))
    (roots / str(TRUSTED_A)).symlink_to(roots / str(TRUSTED_B), target_is_directory=True)
    with pytest.raises(LocalExecutionError, match="real directory"):
        resolve_local_workspace(ws(roots, TRUSTED_A), owner_github_id=TRUSTED_A)


def test_symlink_inside_own_directory_into_another_owner_is_rejected(roots: Path) -> None:
    link = roots / str(TRUSTED_A) / "sneaky"
    link.symlink_to(roots / str(TRUSTED_B) / "repo", target_is_directory=True)
    with pytest.raises(LocalExecutionError, match="inside"):
        resolve_local_workspace(str(link), owner_github_id=TRUSTED_A)


@pytest.mark.parametrize(
    "layout", ["clones-inside-workspace", "workspace-inside-clones", "same-directory"]
)
def test_overlapping_roots_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    workspace = tmp_path / "ws"
    clones = {"clones-inside-workspace": workspace / "clones",
              "workspace-inside-clones": tmp_path,
              "same-directory": workspace}[layout]  # fmt: skip
    clones.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValidationError, match="must not overlap"):
        Settings(_env_file=None, workspace_root=str(workspace), codeforge_github_clone_root=str(clones))
    monkeypatch.setattr(settings, "workspace_root", str(workspace))
    monkeypatch.setattr(settings, "codeforge_github_clone_root", str(clones))
    with pytest.raises(LocalExecutionError, match="overlaps"):
        github_clone_root()


def test_symlinked_clone_root_into_workspace_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    (workspace / "hidden").mkdir(parents=True)
    link = tmp_path / "clones"
    link.symlink_to(workspace / "hidden", target_is_directory=True)
    monkeypatch.setattr(settings, "workspace_root", str(workspace))
    monkeypatch.setattr(settings, "codeforge_github_clone_root", str(link))
    with pytest.raises(LocalExecutionError, match="overlaps"):
        github_clone_root()


def test_default_clone_root_overlapping_workspace_root_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WORKSPACE_ROOT under the system temp dir needs an explicit, separate clone root."""
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(settings, "workspace_root", str(tmp_path / "ws"))
    monkeypatch.setattr(settings, "codeforge_github_clone_root", "")
    with pytest.raises(LocalExecutionError, match="overlaps"):
        github_clone_root()


def _github_service() -> tuple[GitHubExecutionService, MagicMock]:
    client = MagicMock()
    client.token = "test-token"
    client.get_repository = AsyncMock(return_value=MagicMock(default_branch="main"))
    client.get_branch = AsyncMock(return_value=MagicMock(sha="abc123"))
    client.create_branch = AsyncMock()
    service = GitHubExecutionService.__new__(GitHubExecutionService)
    service.github_client = client
    service._orchestrator = MagicMock()
    service._orchestrator.run = AsyncMock(side_effect=RuntimeError("stop after workspace setup"))
    return service, client


@pytest.mark.asyncio
async def test_github_clones_are_created_outside_workspace_root(
    roots: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clones = tmp_path / "github-clones"  # created by local_workspace_root
    monkeypatch.setattr("app.execution.github_execution.SafeGitWrapper", MagicMock())
    service, _ = _github_service()
    await service.execute(_github_request(), "task-1")
    workspace = service._orchestrator.run.call_args.kwargs["workspace"]
    assert workspace.root.is_relative_to(clones.resolve())
    assert not workspace.root.is_relative_to(roots)


@pytest.mark.asyncio
async def test_github_task_refused_before_any_remote_change_when_roots_overlap(
    roots: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "codeforge_github_clone_root", str(roots / str(TRUSTED_A)))
    git = MagicMock()
    monkeypatch.setattr("app.execution.github_execution.SafeGitWrapper", git)
    service, client = _github_service()
    result = await service.execute(_github_request(), "task-1")
    assert result.status == TaskStatus.error
    assert result.failure_stage == GitHubFailureStage.REPOSITORY_ACQUISITION_FAILED
    client.get_repository.assert_not_awaited()
    client.create_branch.assert_not_awaited()
    git.assert_not_called()
