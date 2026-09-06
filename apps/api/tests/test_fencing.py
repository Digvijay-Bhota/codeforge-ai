import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Job, JobStatusEnum, Task
from app.db.repositories.job_repository import JobRepository


@pytest.mark.asyncio
async def test_heartbeat_renewal(session: AsyncSession):
    task_id = str(uuid.uuid4())
    task = Task(task_id=task_id, execution_target="local", requested_task="fencing test")
    session.add(task)
    await session.flush()
    job = Job(task_id=task_id, status=JobStatusEnum.PENDING.value)
    session.add(job)
    await session.commit()

    repo = JobRepository(session)
    claimed_job = await repo.claim_job(job.id, worker_id="worker_a", lease_seconds=10)
    assert claimed_job is not None
    assert claimed_job.lease_version == 1

    # Check that renew_lease extends it
    await asyncio.sleep(0.1) # just to ensure clock ticks

    success = await repo.renew_lease(job.id, "worker_a", 1, lease_seconds=10)
    assert success is True

@pytest.mark.asyncio
async def test_stale_worker_rejected(session: AsyncSession):
    task_id = str(uuid.uuid4())
    task = Task(task_id=task_id, execution_target="local", requested_task="reject stale")
    session.add(task)
    await session.flush()
    job = Job(task_id=task_id, status=JobStatusEnum.PENDING.value)
    session.add(job)
    await session.commit()

    repo = JobRepository(session)

    # 1. Worker A claims
    job_a = await repo.claim_job(job.id, worker_id="worker_a")
    version_a = job_a.lease_version

    # 2. Worker A's lease expires
    await session.execute(text(f"UPDATE jobs SET available_at = NOW() - INTERVAL '1 hour' WHERE id = {job.id}"))
    await session.commit()

    # 3. Reaper reclaims it
    from sqlalchemy import func, update
    stmt = (
        update(Job)
        .where(
            Job.status == JobStatusEnum.RUNNING.value,
            Job.available_at <= func.now()
        )
        .values(
            status=JobStatusEnum.PENDING.value,
            worker_id=None,
            lease_version=Job.lease_version + 1
        )
        .returning(Job.id)
    )
    reclaimed = await session.execute(stmt)
    await session.commit()
    assert len(reclaimed.scalars().all()) == 1

    # 4. Worker B claims it
    job_b = await repo.claim_job(job.id, worker_id="worker_b")
    assert job_b.worker_id == "worker_b"
    assert job_b.lease_version > version_a

    # 5. Worker A tries to finalize (it should fail the lock/verify check)
    job_verify_a = await repo.get_job_for_update(job.id)
    # The check from worker.py:
    can_finalize_a = (job_verify_a and job_verify_a.worker_id == "worker_a" and job_verify_a.lease_version == version_a)
    assert not can_finalize_a

    # 6. Worker B tries to finalize (it should succeed)
    can_finalize_b = (job_verify_a and job_verify_a.worker_id == "worker_b" and job_verify_a.lease_version == job_b.lease_version)
    assert can_finalize_b


import tempfile
from pathlib import Path

from app.execution.ownership import OwnershipLostError
from app.workspace.manager import WorkspaceManager


def test_stale_workspace_write_rejected():
    with tempfile.TemporaryDirectory() as d:
        wm = WorkspaceManager(Path(d))

        # Setup active ownership
        flag = [False]
        def verifier():
            if flag[0]:
                raise OwnershipLostError("stale")
        from app.execution.ownership import _ownership_verifier
        token = _ownership_verifier.set(verifier)

        try:
            # Should succeed
            wm.write_file("test.txt", "hello")
            assert (Path(d) / "test.txt").exists()

            # Simulate ownership lost
            flag[0] = True

            with pytest.raises(OwnershipLostError):
                wm.write_file("test2.txt", "hello2")
            assert not (Path(d) / "test2.txt").exists()
        finally:
            _ownership_verifier.reset(token)

def test_stale_git_push_rejected():
    from unittest.mock import patch

    from app.github.git import SafeGitWrapper
    wrapper = SafeGitWrapper("/tmp", "dummy_token")

    flag = [False]
    def verifier():
        if flag[0]:
            raise OwnershipLostError("stale")

    from app.execution.ownership import _ownership_verifier
    token = _ownership_verifier.set(verifier)

    try:
        with patch.object(wrapper, '_run_git', return_value="mock"):
            wrapper.push("codeforge/task-123")

            flag[0] = True
            with pytest.raises(OwnershipLostError):
                wrapper.push("codeforge/task-123")
    finally:
        _ownership_verifier.reset(token)

def test_stale_context_cleaned_up():
    import pytest

    from app.execution.ownership import (
        OwnershipLostError,
        reset_ownership_verifier,
        set_ownership_verifier,
        verify_ownership,
    )

    def bad_verifier():
        raise OwnershipLostError("stale")

    token = set_ownership_verifier(bad_verifier)
    with pytest.raises(OwnershipLostError):
        verify_ownership()

    reset_ownership_verifier(token)

    # Should not raise now
    verify_ownership()


@pytest.mark.asyncio
async def test_stale_mcp_tool_boundary_rejected():
    import tempfile
    from pathlib import Path

    import app.mcp.tools.repository  # noqa: F401
    from app.mcp.permissions import ToolPermission
    from app.mcp.registry import registry
    from app.workspace.manager import WorkspaceManager

    with tempfile.TemporaryDirectory() as d:
        wm = WorkspaceManager(Path(d))

        flag = [False]
        def verifier():
            if flag[0]:
                raise OwnershipLostError("stale")

        from app.execution.ownership import _ownership_verifier
        token = _ownership_verifier.set(verifier)

        try:
            # First, check valid
            await registry.execute_tool(
                name="repository.create_file",
                arguments={"path": "test.txt", "content": "hello"},
                caller_permission=ToolPermission.WRITE,
                workspace=wm
            )
            assert (Path(d) / "test.txt").exists()

            # Now lose ownership
            flag[0] = True

            # Use execute_tool again; it should either raise OwnershipLostError OR return the error as TextContent.
            # execute_tool catches MCPError, but OwnershipLostError is not an MCPError unless it's wrapped.
            # Let's see what happens.
            with pytest.raises(OwnershipLostError):
                await registry.execute_tool(
                    name="repository.create_file",
                    arguments={"path": "stale.txt", "content": "hello"},
                    caller_permission=ToolPermission.WRITE,
                    workspace=wm
                )

            assert not (Path(d) / "stale.txt").exists()

        finally:
            _ownership_verifier.reset(token)


@pytest.mark.asyncio
async def test_authoritative_db_fencing_workspace(setup_db):
    import tempfile
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

    import app.mcp.tools.repository  # noqa: F401
    from app.db.models import Job, JobStatusEnum
    from app.db.repositories.job_repository import JobRepository
    from app.mcp.permissions import ToolPermission
    from app.mcp.registry import registry
    from app.workspace.manager import WorkspaceManager

    session = AsyncSession(setup_db)
    repo = JobRepository(session)
    from app.db.models import Task, TaskStatusEnum
    from app.db.repositories.task_repository import TaskRepository
    task_repo = TaskRepository(session)
    new_task = Task(task_id="task_123", status=TaskStatusEnum.PENDING.value, requested_task="test", repository="owner/repo", execution_target="WORKSPACE")
    session.add(new_task)
    await session.commit()

    new_job = Job(task_id="task_123", worker_id=None, status=JobStatusEnum.PENDING.value)
    job = await repo.create_job(new_job)
    job = await repo.claim_job(job.id, "worker_A")
    assert job is not None
    job_id = job.id
    assert job.lease_version == 1

    # Setup context
    flag = [False]
    def sync_verifier():
        if flag[0]:
            raise OwnershipLostError("stale locally")

    async def async_verifier():
        if flag[0]:
            raise OwnershipLostError("stale locally")
        # Use job_id instead of job.id to avoid lazy-loading an expired object
        current_job = await repo.get_job(job_id)
        if not current_job or current_job.worker_id != "worker_A" or current_job.lease_version != 1:
            raise OwnershipLostError("stale in DB")

    from app.execution.ownership import (
        OwnershipLostError,
        reset_async_ownership_verifier,
        reset_ownership_verifier,
        set_async_ownership_verifier,
        set_ownership_verifier,
    )
    t1 = set_ownership_verifier(sync_verifier)
    t2 = set_async_ownership_verifier(async_verifier)

    try:
        with tempfile.TemporaryDirectory() as d:
            wm = WorkspaceManager(Path(d))

            # 1. Valid execution
            await registry.execute_tool(
                name="repository.create_file",
                arguments={"path": "test.txt", "content": "hello"},
                caller_permission=ToolPermission.WRITE,
                workspace=wm
            )
            assert (Path(d) / "test.txt").exists()

            # 2. Simulate DB ownership change (e.g. Reaper or claim)
            # Worker B reclaims it!
            from sqlalchemy import update
            await session.execute(
                update(Job).where(Job.id == job_id).values(
                    worker_id="worker_B",
                    lease_version=2
                )
            )
            await session.commit()

            # Local flag is still False!
            assert flag[0] is False

            # 3. Worker A attempts mutation through orchestration boundary
            with pytest.raises(OwnershipLostError, match="stale in DB"):
                await registry.execute_tool(
                    name="repository.create_file",
                    arguments={"path": "stale.txt", "content": "hello"},
                    caller_permission=ToolPermission.WRITE,
                    workspace=wm
                )
            assert not (Path(d) / "stale.txt").exists()

    finally:
        await session.close()
        reset_ownership_verifier(t1)
        reset_async_ownership_verifier(t2)


@pytest.mark.asyncio
async def test_authoritative_db_fencing_git(setup_db, monkeypatch):

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import Job, JobStatusEnum, Task, TaskStatusEnum
    from app.db.repositories.job_repository import JobRepository
    from app.db.repositories.task_repository import TaskRepository
    from app.execution.github_execution import GitHubExecutionService
    from app.github.git import SafeGitWrapper

    session = AsyncSession(setup_db)
    repo = JobRepository(session)
    task_repo = TaskRepository(session)

    new_task = Task(task_id="task_git", status=TaskStatusEnum.PENDING.value, requested_task="test", repository="owner/repo", execution_target="GITHUB_PR")
    session.add(new_task)
    await session.commit()

    new_job = Job(task_id="task_git", worker_id=None, status=JobStatusEnum.PENDING.value)
    job = await repo.create_job(new_job)
    job = await repo.claim_job(job.id, "worker_A")
    job_id = job.id

    flag = [False]
    def sync_verifier():
        if flag[0]:
            raise OwnershipLostError("stale locally")

    async def async_verifier():
        if flag[0]:
            raise OwnershipLostError("stale locally")
        current_job = await repo.get_job(job_id)
        if not current_job or current_job.worker_id != "worker_A" or current_job.lease_version != 1:
            raise OwnershipLostError("stale in DB")

    from app.execution.ownership import (
        reset_async_ownership_verifier,
        reset_ownership_verifier,
        set_async_ownership_verifier,
        set_ownership_verifier,
    )
    t1 = set_ownership_verifier(sync_verifier)
    t2 = set_async_ownership_verifier(async_verifier)

    push_called = False
    def mock_push(*args, **kwargs):
        nonlocal push_called
        push_called = True

    monkeypatch.setattr(SafeGitWrapper, "push", mock_push)

    # Simulate worker B claiming it!
    from sqlalchemy import update
    await session.execute(
        update(Job).where(Job.id == job_id).values(
            worker_id="worker_B",
            lease_version=2
        )
    )
    await session.commit()

    try:
        from app.schemas.task import TaskRequest

        request = TaskRequest(execution_target="github", github_repository="owner/repo", description="test description task")
        task_id = "task_git"



        # Patch out everything before push just to test push
        monkeypatch.setattr(SafeGitWrapper, "clone", lambda *args, **kwargs: None)
        monkeypatch.setattr(SafeGitWrapper, "checkout_new_branch", lambda *args, **kwargs: None)
        monkeypatch.setattr(SafeGitWrapper, "has_changes", lambda *args, **kwargs: True)
        monkeypatch.setattr(SafeGitWrapper, "commit_files", lambda *args, **kwargs: "abc")

        # Monkey patch GitHub client
        class FakeClient:
            token = "fake_token"
            async def get_repository(self, *args, **kwargs): return type('obj', (object,), {'default_branch': 'main'})()
            async def get_branch(self, *args, **kwargs): return type('obj', (object,), {'sha': 'abc'})()
            async def get_default_branch(self, *args, **kwargs): return "main"
            async def get_branch_sha(self, *args, **kwargs): return type('obj', (object,), {'sha': 'abc'})()
            async def create_branch(self, *args, **kwargs): return None
            async def create_pull_request(self, *args, **kwargs): return type('obj', (object,), {'html_url': 'http'})()
        import app.execution.github_execution
        monkeypatch.setattr(app.execution.github_execution, "GitHubClient", lambda *args, **kwargs: FakeClient())
        gh_service = GitHubExecutionService()

        from app.workspace.manager import WorkspaceManager
        monkeypatch.setattr(WorkspaceManager, "get_modified_paths", lambda self: ['test.txt'])

        # Mock orchestrator
        class FakeOrchestrator:
            async def run(self, *args, **kwargs):
                from app.orchestration.models import FinalTaskResult, WorkflowStatus
                return FinalTaskResult(task_id=task_id, task_description="test", workflow_status=WorkflowStatus.COMPLETED, final_message="ok", token_usage={}, metadata={})
        gh_service._orchestrator = FakeOrchestrator()

        with pytest.raises(OwnershipLostError, match="stale in DB"):
            await gh_service.execute(request, task_id)

        assert not push_called

    finally:
        reset_ownership_verifier(t1)
        reset_async_ownership_verifier(t2)
        await session.close()
