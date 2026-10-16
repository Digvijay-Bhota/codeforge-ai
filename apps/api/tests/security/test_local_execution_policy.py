"""Phase 10B.5 Stage 0 — local execution policy regression tests.

``execution_target=local`` runs the coding agent and the repository test
suite directly on the worker host.  It must be disabled unless the operator
opts in with CODEFORGE_ALLOW_LOCAL_EXECUTION and confines workspaces to a
canonical WORKSPACE_ROOT.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, settings
from app.db.models import Task, TaskStatusEnum
from app.execution.local_policy import (
    LocalExecutionDisabledError,
    LocalExecutionError,
    resolve_local_workspace,
)
from app.orchestration.models import FinalTaskResult, WorkflowStatus
from app.schemas.task import ExecutionTarget, TaskRequest, TaskResult, TaskStatus
from app.services.task_service import TaskService
from app.workspace.snapshot import SnapshotLimitError

# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def local_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "codeforge_allow_local_execution", False)


@pytest.fixture
def workspace_root(local_workspace_root: Path) -> Path:
    """Enable local execution confined to a fresh WORKSPACE_ROOT (see conftest)."""
    return local_workspace_root


def _completed_final() -> FinalTaskResult:
    return FinalTaskResult(
        task_id="t",
        workflow_status=WorkflowStatus.COMPLETED,
        task_description="Fix it",
        plan_summary="",
        changed_files=["a.py"],
        diff="",
        test_result=None,
        final_message="Done",
        failure_reason=None,
    )


# ── Settings ───────────────────────────────────────────────────────────────────


def test_local_execution_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEFORGE_ALLOW_LOCAL_EXECUTION", raising=False)
    assert Settings(_env_file=None).codeforge_allow_local_execution is False


def test_settings_reject_enabled_local_execution_without_workspace_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEFORGE_ALLOW_LOCAL_EXECUTION", "true")
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    with pytest.raises(ValueError, match="WORKSPACE_ROOT"):
        Settings(_env_file=None)


def test_settings_accept_enabled_local_execution_with_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEFORGE_ALLOW_LOCAL_EXECUTION", "true")
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    loaded = Settings(_env_file=None)
    assert loaded.codeforge_allow_local_execution is True
    assert loaded.workspace_root == str(tmp_path)


# ── Policy: enablement and configuration ───────────────────────────────────────


def test_policy_rejects_when_disabled_even_for_valid_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ws").mkdir()
    monkeypatch.setattr(settings, "codeforge_allow_local_execution", False)
    monkeypatch.setattr(settings, "workspace_root", str(tmp_path))
    with pytest.raises(LocalExecutionDisabledError):
        resolve_local_workspace(str(tmp_path / "ws"))


def test_policy_requires_workspace_root_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "codeforge_allow_local_execution", True)
    monkeypatch.setattr(settings, "workspace_root", "")
    with pytest.raises(LocalExecutionError, match="WORKSPACE_ROOT"):
        resolve_local_workspace(str(tmp_path))


def test_policy_requires_absolute_workspace_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "codeforge_allow_local_execution", True)
    monkeypatch.setattr(settings, "workspace_root", "relative/root")
    with pytest.raises(LocalExecutionError, match="WORKSPACE_ROOT"):
        resolve_local_workspace("/tmp")


def test_policy_requires_existing_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "codeforge_allow_local_execution", True)
    monkeypatch.setattr(settings, "workspace_root", str(tmp_path / "missing"))
    with pytest.raises(LocalExecutionError, match="WORKSPACE_ROOT"):
        resolve_local_workspace(str(tmp_path))


# ── Policy: canonical containment ──────────────────────────────────────────────


def test_policy_accepts_directory_inside_root(workspace_root: Path) -> None:
    ws = workspace_root / "repo"
    ws.mkdir()
    assert resolve_local_workspace(str(ws)) == ws


def test_policy_rejects_workspace_root_itself(workspace_root: Path) -> None:
    with pytest.raises(LocalExecutionError, match="inside WORKSPACE_ROOT"):
        resolve_local_workspace(str(workspace_root))


def test_policy_rejects_path_outside_root(workspace_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(LocalExecutionError, match="inside WORKSPACE_ROOT"):
        resolve_local_workspace(str(outside))


def test_policy_rejects_sibling_with_shared_prefix(workspace_root: Path) -> None:
    # /x/workspaces-evil must not be treated as inside /x/workspaces.
    sibling = workspace_root.parent / f"{workspace_root.name}-evil"
    sibling.mkdir()
    with pytest.raises(LocalExecutionError, match="inside WORKSPACE_ROOT"):
        resolve_local_workspace(str(sibling))


def test_policy_rejects_dotdot_traversal(workspace_root: Path, tmp_path: Path) -> None:
    (workspace_root / "repo").mkdir()
    (tmp_path / "outside").mkdir()
    sneaky = f"{workspace_root}/repo/../../outside"
    with pytest.raises(LocalExecutionError, match="inside WORKSPACE_ROOT"):
        resolve_local_workspace(sneaky)


def test_policy_rejects_symlink_escape(workspace_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret-host-dir"
    outside.mkdir()
    link = workspace_root / "innocent"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(LocalExecutionError, match="inside WORKSPACE_ROOT"):
        resolve_local_workspace(str(link))


def test_policy_rejects_nested_symlink_escape(workspace_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "etc-like"
    (outside / "deep").mkdir(parents=True)
    (workspace_root / "a").mkdir()
    (workspace_root / "a" / "b").symlink_to(outside, target_is_directory=True)
    with pytest.raises(LocalExecutionError, match="inside WORKSPACE_ROOT"):
        resolve_local_workspace(str(workspace_root / "a" / "b" / "deep"))


def test_policy_accepts_symlink_that_stays_inside_root(workspace_root: Path) -> None:
    real = workspace_root / "real"
    real.mkdir()
    (workspace_root / "alias").symlink_to(real, target_is_directory=True)
    assert resolve_local_workspace(str(workspace_root / "alias")) == real


def test_policy_canonicalises_symlinked_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_root = tmp_path / "real-root"
    (real_root / "repo").mkdir(parents=True)
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    monkeypatch.setattr(settings, "codeforge_allow_local_execution", True)
    monkeypatch.setattr(settings, "workspace_root", str(linked_root))
    assert resolve_local_workspace(str(linked_root / "repo")) == real_root / "repo"
    assert resolve_local_workspace(str(real_root / "repo")) == real_root / "repo"


def test_policy_rejects_relative_workspace_path(workspace_root: Path) -> None:
    (workspace_root / "repo").mkdir()
    with pytest.raises(LocalExecutionError, match="absolute"):
        resolve_local_workspace("repo")


def test_policy_rejects_missing_workspace(workspace_root: Path) -> None:
    with pytest.raises(LocalExecutionError, match="does not exist"):
        resolve_local_workspace(str(workspace_root / "nope"))


def test_policy_rejects_file_workspace(workspace_root: Path) -> None:
    f = workspace_root / "file.txt"
    f.write_text("x")
    with pytest.raises(LocalExecutionError, match="directory"):
        resolve_local_workspace(str(f))


def test_policy_rejects_empty_workspace_path(workspace_root: Path) -> None:
    with pytest.raises(LocalExecutionError):
        resolve_local_workspace("")


# ── TaskService.execute_task (worker path) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_task_rejects_local_when_disabled(
    tmp_path: Path, local_disabled: None
) -> None:
    service = TaskService()
    run = AsyncMock(return_value=_completed_final())
    with patch.object(service._orchestrator, "run", new=run):
        result = await service.execute_task(
            TaskRequest(workspace_path=str(tmp_path), description="Fix the add function"),
            "task-1",
        )
    assert result.status == TaskStatus.error
    assert "CODEFORGE_ALLOW_LOCAL_EXECUTION" in (result.error_message or "")
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_task_rejects_local_outside_root(
    workspace_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    service = TaskService()
    run = AsyncMock(return_value=_completed_final())
    with patch.object(service._orchestrator, "run", new=run):
        result = await service.execute_task(
            TaskRequest(workspace_path=str(outside), description="Fix the add function"),
            "task-1",
        )
    assert result.status == TaskStatus.error
    assert "inside WORKSPACE_ROOT" in (result.error_message or "")
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_task_rejects_local_symlink_escape(
    workspace_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace_root / "link").symlink_to(outside, target_is_directory=True)
    service = TaskService()
    run = AsyncMock(return_value=_completed_final())
    with patch.object(service._orchestrator, "run", new=run):
        result = await service.execute_task(
            TaskRequest(
                workspace_path=str(workspace_root / "link"), description="Fix the add function"
            ),
            "task-1",
        )
    assert result.status == TaskStatus.error
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_task_runs_local_when_enabled_inside_root(workspace_root: Path) -> None:
    ws = workspace_root / "repo"
    ws.mkdir()
    service = TaskService()
    run = AsyncMock(return_value=_completed_final())
    with patch.object(service._orchestrator, "run", new=run):
        result = await service.execute_task(
            TaskRequest(workspace_path=str(ws), description="Fix the add function"), "task-1"
        )
    assert result.status == TaskStatus.success
    run.assert_awaited_once()
    assert run.call_args.kwargs["workspace"].root == ws


@pytest.mark.asyncio
async def test_execute_task_github_unaffected_by_local_policy(local_disabled: None) -> None:
    """GitHub-targeted execution must keep working with local execution disabled."""
    expected = TaskResult(
        task_id="task-1",
        status=TaskStatus.success,
        description="Fix the add function",
        plan=[],
        changed_files=[],
        diff="",
        test_result=None,
        error_message=None,
        agent_output="",
    )
    with patch("app.execution.github_execution.GitHubExecutionService") as gh_cls:
        gh_cls.return_value.execute = AsyncMock(return_value=expected)
        result = await TaskService().execute_task(
            TaskRequest(
                execution_target=ExecutionTarget.github,
                github_repository="owner/repo",
                description="Fix the add function",
            ),
            "task-1",
        )
    assert result is expected
    gh_cls.return_value.execute.assert_awaited_once()


# ── TaskService.create_task (API path) ─────────────────────────────────────────


def _service_with_mock_session() -> tuple[TaskService, MagicMock]:
    session = MagicMock(spec=AsyncSession)
    service = TaskService(session)
    service.task_repo = MagicMock()
    service.task_repo.create_task = AsyncMock()
    return service, session


@pytest.mark.asyncio
async def test_create_task_rejects_local_when_disabled(
    tmp_path: Path, local_disabled: None
) -> None:
    service, _ = _service_with_mock_session()
    with pytest.raises(HTTPException) as exc:
        await service.create_task(
            TaskRequest(workspace_path=str(tmp_path), description="Fix the add function")
        )
    assert exc.value.status_code == 403
    service.task_repo.create_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_task_rejects_local_outside_root(
    workspace_root: Path, tmp_path: Path
) -> None:
    service, _ = _service_with_mock_session()
    with pytest.raises(HTTPException) as exc:
        await service.create_task(
            TaskRequest(workspace_path=str(tmp_path), description="Fix the add function")
        )
    assert exc.value.status_code == 400
    service.task_repo.create_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_task_persists_canonical_local_path(
    session: AsyncSession, workspace_root: Path
) -> None:
    real = workspace_root / "real"
    real.mkdir()
    (workspace_root / "alias").symlink_to(real, target_is_directory=True)
    task = await TaskService(session).create_task(
        TaskRequest(workspace_path=str(workspace_root / "alias"), description="Fix the add function")
    )
    await session.commit()
    assert task.workspace_path == str(real)


# ── HTTP API ───────────────────────────────────────────────────────────────────


@pytest.fixture
def api_client() -> TestClient:
    from app.api.deps import get_current_user
    from app.db.models import User
    from app.db.session import get_db_session
    from app.main import create_app

    app = create_app()
    user = User(id="policy-user", display_name="Policy", email="p@example.com", is_active=True)
    app.dependency_overrides[get_current_user] = lambda: user

    async def _fake_session():  # type: ignore[no-untyped-def]
        yield MagicMock(spec=AsyncSession)

    app.dependency_overrides[get_db_session] = _fake_session
    return TestClient(app)


def test_api_rejects_local_task_when_disabled(
    api_client: TestClient, tmp_path: Path, local_disabled: None
) -> None:
    with patch("app.db.repositories.task_repository.TaskRepository.create_task") as create:
        response = api_client.post(
            "/api/v1/tasks",
            json={"workspace_path": str(tmp_path), "description": "Fix the add function"},
        )
    assert response.status_code == 403
    assert "CODEFORGE_ALLOW_LOCAL_EXECUTION" in response.json()["detail"]
    create.assert_not_called()


def test_api_accepts_github_task_when_local_disabled(
    api_client: TestClient, local_disabled: None
) -> None:
    mock_task = Task(task_id="gh-task", status=TaskStatusEnum.PENDING.value)
    with (
        patch(
            "app.api.tasks.AuthorizationService.can_write_repository",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.services.task_service.TaskService.create_task",
            new=AsyncMock(return_value=mock_task),
        ),
    ):
        response = api_client.post(
            "/api/v1/tasks",
            json={
                "execution_target": "github",
                "github_repository": "owner/repo",
                "description": "Fix the add function",
            },
        )
    assert response.status_code == 200
    assert response.json()["task_id"] == "gh-task"


# ── Retry path ─────────────────────────────────────────────────────────────────


async def _failed_local_task(session: AsyncSession, workspace_path: str) -> Task:
    task = Task(
        task_id=str(uuid.uuid4()),
        status=TaskStatusEnum.FAILED.value,
        requested_task="Previously failed local task",
        execution_target="local",
        workspace_path=workspace_path,
    )
    session.add(task)
    await session.commit()
    return task


@pytest.mark.asyncio
async def test_retry_rejects_local_when_disabled(
    session: AsyncSession, tmp_path: Path, local_disabled: None
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("x = 1\n")
    original = await _failed_local_task(session, str(src))

    with patch("app.services.task_service._copy_isolated_workspace") as copy:
        with pytest.raises(HTTPException) as exc:
            await TaskService(session).retry_task(original.task_id)
    assert exc.value.status_code == 403
    copy.assert_not_called()
    children = (
        (await session.execute(select(Task).where(Task.parent_task_id == original.task_id)))
        .scalars()
        .all()
    )
    assert children == []


@pytest.mark.asyncio
async def test_retry_rejects_original_workspace_outside_root(
    session: AsyncSession, workspace_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("host data")
    original = await _failed_local_task(session, str(outside))

    with patch("app.services.task_service._copy_isolated_workspace") as copy:
        with pytest.raises(HTTPException) as exc:
            await TaskService(session).retry_task(original.task_id)
    assert exc.value.status_code == 400
    copy.assert_not_called()


@pytest.mark.asyncio
async def test_retry_rejects_override_symlink_escape(
    session: AsyncSession, workspace_root: Path, tmp_path: Path
) -> None:
    ws = workspace_root / "repo"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace_root / "escape").symlink_to(outside, target_is_directory=True)
    original = await _failed_local_task(session, str(ws))

    with pytest.raises(HTTPException) as exc:
        await TaskService(session).retry_task(
            original.task_id, workspace_path_override=str(workspace_root / "escape")
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_retry_copies_local_workspace_inside_root(
    session: AsyncSession, workspace_root: Path
) -> None:
    ws = workspace_root / "repo"
    ws.mkdir()
    (ws / "a.py").write_text("x = 1\n")
    original = await _failed_local_task(session, str(ws))

    child = await TaskService(session).retry_task(original.task_id)
    await session.commit()

    child_path = Path(child.workspace_path)
    assert child_path.parent == workspace_root
    assert (child_path / "a.py").read_text() == "x = 1\n"
    assert os.path.realpath(child_path) == str(child_path)


@pytest.mark.asyncio
async def test_retry_copy_applies_the_snapshot_policy(
    session: AsyncSession, workspace_root: Path
) -> None:
    ws = workspace_root / "repo"
    (ws / ".git").mkdir(parents=True)
    (ws / ".git" / "config").write_text("[core]\n")
    (ws / ".env").write_text("TOKEN=canary\n")
    (ws / "a.py").write_text("x = 1\n")
    (ws / "passwd").symlink_to("/etc/passwd")
    original = await _failed_local_task(session, str(ws))

    child = await TaskService(session).retry_task(original.task_id)
    await session.commit()

    assert sorted(p.name for p in Path(child.workspace_path).iterdir()) == ["a.py"]


@pytest.mark.asyncio
async def test_retry_fails_closed_and_cleans_up_when_the_copy_is_rejected(
    session: AsyncSession, workspace_root: Path
) -> None:
    ws = workspace_root / "repo"
    ws.mkdir()
    (ws / "a.py").write_text("x = 1\n")
    original = await _failed_local_task(session, str(ws))

    with patch(
        "app.services.task_service.copy_workspace",
        side_effect=SnapshotLimitError("Workspace has more than 20000 files"),
    ):
        with pytest.raises(HTTPException) as exc:
            await TaskService(session).retry_task(original.task_id)

    assert exc.value.status_code == 400
    assert "cannot be copied" in str(exc.value.detail)
    assert sorted(p.name for p in workspace_root.iterdir()) == ["repo"]
    children = (
        (await session.execute(select(Task).where(Task.parent_task_id == original.task_id)))
        .scalars()
        .all()
    )
    assert children == []


@pytest.mark.asyncio
async def test_retry_records_what_the_workspace_copy_left_out(
    session: AsyncSession, workspace_root: Path
) -> None:
    from app.db.models import AuditEvent

    ws = workspace_root / "repo"
    (ws / "build").mkdir(parents=True)
    (ws / "build" / "gen.py").write_text("x = 1\n")
    (ws / ".env").write_text("TOKEN=canary\n")
    (ws / "a.py").write_text("x = 1\n")
    original = await _failed_local_task(session, str(ws))

    child = await TaskService(session).retry_task(original.task_id)
    await session.commit()

    audit = (
        await session.execute(
            select(AuditEvent).where(
                AuditEvent.resource_id == child.task_id, AuditEvent.event_type == "TASK_RETRIED"
            )
        )
    ).scalar_one()
    notice = (audit.metadata_payload or {}).get("workspace_copy_notice", "")
    assert "ignored_directory (ignored directory): 1 - build" in notice
    assert "secret (secret-looking name; names withheld): 1" in notice
    assert ".env" not in notice and "canary" not in notice


@pytest.mark.asyncio
async def test_retry_with_an_oversized_source_file_fails_and_leaves_no_partial_copy(
    session: AsyncSession, workspace_root: Path
) -> None:
    import functools

    from app.workspace.snapshot import SnapshotLimits, copy_workspace

    ws = workspace_root / "repo"
    ws.mkdir()
    (ws / "a.py").write_text("x = 1\n")
    (ws / "big_module.py").write_bytes(b"#" * 11)
    original = await _failed_local_task(session, str(ws))

    with patch(
        "app.services.task_service.copy_workspace",
        functools.partial(copy_workspace, limits=SnapshotLimits(max_file_bytes=10)),
    ):
        with pytest.raises(HTTPException) as exc:
            await TaskService(session).retry_task(original.task_id)

    assert exc.value.status_code == 400
    assert "big_module.py (source, 11 bytes)" in str(exc.value.detail)
    assert sorted(p.name for p in workspace_root.iterdir()) == ["repo"]
    children = (
        (await session.execute(select(Task).where(Task.parent_task_id == original.task_id)))
        .scalars()
        .all()
    )
    assert children == []
