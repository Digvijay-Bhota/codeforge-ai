"""Phase 10A tests: Product API, Plan Approval, Rejection, Retry, Diff, and Pause/Resume."""

import asyncio
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_current_user
from app.db.models import (
    ApprovalStatusEnum,
    Job,
    JobStatusEnum,
    OutboxEvent,
    Task,
    TaskApproval,
    TaskStatusEnum,
    User,
)
from app.db.repositories.job_repository import JobRepository
from app.db.repositories.task_repository import TaskRepository
from app.db.session import get_db_session
from app.main import create_app
from app.orchestration.models import ImplementationPlan
from app.schemas.plan import PlanAction, RiskLevel
from app.schemas.plan import PlanStep as PlanStageStep
from app.schemas.task import (
    ChangedFile,
    TaskRequest,
    TaskStatus,
    TestResult,
)
from app.schemas.task import (
    PlanStep as TaskPlanStep,
)
from app.schemas.task import (
    TaskResult as SchemaTaskResult,
)
from app.services.diff_service import parse_unified_diff
from app.services.task_service import TaskService, parse_plan_dict
from app.services.task_state_machine import InvalidTaskTransitionError, TaskStateMachine
from app.worker import process_job


@pytest_asyncio.fixture
async def async_client(session: AsyncSession):
    app = create_app()

    async def override_get_db():
        yield session

    user = await session.get(User, "test-phase10a-user")
    if not user:
        user = User(
            id="test-phase10a-user",
            display_name="Phase10A Tester",
            email="tester@example.com",
            is_active=True,
        )
        session.add(user)
        await session.commit()

    async def override_get_current_user():
        return user

    app.dependency_overrides[get_db_session] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


def _sample_plan() -> ImplementationPlan:
    return ImplementationPlan(
        goal="Fix calculation bug in calculator.py",
        assumptions=["Python 3.12+"],
        steps=[
            PlanStageStep(
                step_number=1,
                action=PlanAction.modify,
                description="Fix divide by zero check",
                affected_paths=["calculator.py"],
                rationale="Prevent crashing when dividing by zero",
                dependencies=[],
            )
        ],
        affected_files=["calculator.py"],
        tests_needed=["test_divide"],
        validation_strategy="Run pytest",
        risks=["Minimal"],
        risk_level=RiskLevel.low,
        summary="Add check for zero divisor in calculator.py",
    )


# ── 1. Structured Diff Unit Tests ─────────────────────────────────────────────


def test_parse_unified_diff_empty():
    res = parse_unified_diff("t1", "")
    assert res.files_changed_count == 0
    assert res.additions == 0
    assert res.deletions == 0
    assert res.files == []

    res2 = parse_unified_diff("t1", None)
    assert res2.files_changed_count == 0


def test_parse_unified_diff_modified_file():
    raw = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,4 +1,5 @@
 import os
+import sys
-old_var = 1
+new_var = 2
"""
    res = parse_unified_diff("t1", raw)
    assert res.files_changed_count == 1
    assert res.additions == 2
    assert res.deletions == 1
    assert len(res.files) == 1
    f = res.files[0]
    assert f.path == "app.py"
    assert f.status == "modified"
    assert f.additions == 2
    assert f.deletions == 1


def test_parse_unified_diff_added_file():
    raw = """--- /dev/null
+++ b/new_module.py
@@ -0,0 +1,3 @@
+def foo():
+    return True
"""
    res = parse_unified_diff("t2", raw)
    assert res.files_changed_count == 1
    assert res.additions == 2
    assert res.deletions == 0
    f = res.files[0]
    assert f.path == "new_module.py"
    assert f.status == "added"


def test_parse_unified_diff_deleted_file():
    raw = """--- a/deprecated.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def old():
-    pass
"""
    res = parse_unified_diff("t3", raw)
    assert res.files_changed_count == 1
    assert res.additions == 0
    assert res.deletions == 2
    f = res.files[0]
    assert f.path == "deprecated.py"
    assert f.status == "deleted"


def test_parse_unified_diff_truncation_bound():
    many_lines = "\n".join([f"+line {i}" for i in range(50)])
    raw = f"""--- a/big.py\n+++ b/big.py\n@@ -1 +1,50 @@\n{many_lines}"""
    res = parse_unified_diff("t4", raw, max_patch_lines=10)
    assert res.files_changed_count == 1
    assert res.additions == 50
    assert "truncated" in res.files[0].patch


# ── 2. State Machine Unit Tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_state_machine_approval_transitions(session: AsyncSession):
    repo = TaskRepository(session)
    sm = TaskStateMachine(repo)

    task = Task(
        task_id=str(uuid.uuid4()),
        status=TaskStatusEnum.PENDING.value,
        execution_target="local",
        requested_task="test",
    )
    await repo.create_task(task)

    task = await sm.transition(task, TaskStatusEnum.QUEUED)
    task = await sm.transition(task, TaskStatusEnum.ANALYZING)
    task = await sm.transition(task, TaskStatusEnum.PLANNING)
    task = await sm.transition(task, TaskStatusEnum.WAITING_APPROVAL)
    assert task.status == TaskStatusEnum.WAITING_APPROVAL.value

    # Resume on approval: WAITING_APPROVAL -> QUEUED -> CODING
    task = await sm.transition(task, TaskStatusEnum.QUEUED)
    assert task.status == TaskStatusEnum.QUEUED.value

    task = await sm.transition(task, TaskStatusEnum.CODING)
    assert task.status == TaskStatusEnum.CODING.value

    # Rejection transition
    task2 = Task(
        task_id=str(uuid.uuid4()),
        status=TaskStatusEnum.WAITING_APPROVAL.value,
        execution_target="local",
        requested_task="test2",
    )
    await repo.create_task(task2)
    task2 = await sm.transition(task2, TaskStatusEnum.CANCELLED)
    assert task2.status == TaskStatusEnum.CANCELLED.value

    # Invalid transitions
    with pytest.raises(InvalidTaskTransitionError):
        await sm.transition(task2, TaskStatusEnum.CODING)  # Cannot transition from CANCELLED


# ── 3. Plan Dict Reconstitution Unit Tests ────────────────────────────────────


def test_parse_plan_dict():
    plan = _sample_plan()
    dumped = plan.model_dump()
    parsed = parse_plan_dict(dumped)
    assert parsed is not None
    assert parsed.goal == plan.goal
    assert len(parsed.steps) == 1
    assert parsed.steps[0].description == "Fix divide by zero check"

    # Simplified steps dict
    simplified = {"steps": [{"step": 1, "description": "Step one"}]}
    parsed2 = parse_plan_dict(simplified)
    assert parsed2 is not None
    assert len(parsed2.steps) == 1
    assert parsed2.steps[0].description == "Step one"

    assert parse_plan_dict(None) is None
    assert parse_plan_dict({}) is None


# ── 4. Task Service: Approval, Rejection, Retry Integration Tests ─────────────


@pytest.mark.asyncio
async def test_task_creation_with_approval_config(session: AsyncSession):
    service = TaskService(session)
    req = TaskRequest(
        workspace_path="/tmp/test",
        description="Write a calculator function with approval",
        require_plan_approval=True,
        approval_config={"timeout_hours": 48},
    )
    task = await service.create_task(req)
    await session.commit()

    assert task.task_id is not None
    assert task.approval_config is not None
    assert task.approval_config["require_plan_approval"] is True
    assert task.approval_config["timeout_hours"] == 48


@pytest.mark.asyncio
async def test_approve_task_success_and_idempotency(session: AsyncSession):
    service = TaskService(session)
    task_id = str(uuid.uuid4())

    # Create task parked at WAITING_APPROVAL
    task = Task(
        task_id=task_id,
        status=TaskStatusEnum.WAITING_APPROVAL.value,
        execution_target="local",
        requested_task="Approve me",
        implementation_plan=_sample_plan().model_dump(),
    )
    await service.task_repo.create_task(task)

    approval = TaskApproval(
        task_id=task_id,
        approval_type="plan",
        status=ApprovalStatusEnum.PENDING.value,
        requested_by="worker",
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    await service.task_repo.create_approval(approval)
    await session.commit()

    # Call approve
    approved_task = await service.approve_task(task_id, approver="digvijay", comment="LGTM!")
    await session.commit()

    assert approved_task.status == TaskStatusEnum.QUEUED.value

    # Verify TaskApproval updated
    updated_app = await service.task_repo.get_latest_approval(task_id)
    assert updated_app is not None
    assert updated_app.status == ApprovalStatusEnum.APPROVED.value
    assert updated_app.approved_by == "digvijay"
    assert updated_app.comment == "LGTM!"
    assert updated_app.responded_at is not None

    # Verify new Job created
    jobs = (await session.execute(select(Job).where(Job.task_id == task_id))).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].status == JobStatusEnum.PENDING.value

    # Verify OutboxEvent created
    events = (
        (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == str(jobs[0].id))
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].event_type == "JOB_CREATED"

    # Idempotent call
    approved_again = await service.approve_task(task_id, approver="digvijay")
    assert approved_again.status == TaskStatusEnum.QUEUED.value
    # No second job created
    jobs_after = (await session.execute(select(Job).where(Job.task_id == task_id))).scalars().all()
    assert len(jobs_after) == 1


@pytest.mark.asyncio
async def test_approve_task_expired(session: AsyncSession):
    from fastapi import HTTPException

    service = TaskService(session)
    task_id = str(uuid.uuid4())

    task = Task(
        task_id=task_id,
        status=TaskStatusEnum.WAITING_APPROVAL.value,
        execution_target="local",
        requested_task="Expired plan",
    )
    await service.task_repo.create_task(task)

    approval = TaskApproval(
        task_id=task_id,
        approval_type="plan",
        status=ApprovalStatusEnum.PENDING.value,
        requested_by="worker",
        expires_at=datetime.now(UTC) - timedelta(hours=1),  # already expired
    )
    await service.task_repo.create_approval(approval)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await service.approve_task(task_id)
    assert exc.value.status_code == 409
    assert "expired" in exc.value.detail.lower()

    # Verify task cancelled and approval expired
    updated_task = await service.task_repo.get_task(task_id)
    assert updated_task is not None
    assert updated_task.status == TaskStatusEnum.CANCELLED.value
    assert "expired" in (updated_task.failure_reason or "").lower()

    updated_app = await service.task_repo.get_latest_approval(task_id)
    assert updated_app is not None
    assert updated_app.status == ApprovalStatusEnum.EXPIRED.value


@pytest.mark.asyncio
async def test_reject_task_success_and_idempotency(session: AsyncSession):
    service = TaskService(session)
    task_id = str(uuid.uuid4())

    task = Task(
        task_id=task_id,
        status=TaskStatusEnum.WAITING_APPROVAL.value,
        execution_target="local",
        requested_task="Reject me",
    )
    await service.task_repo.create_task(task)

    approval = TaskApproval(
        task_id=task_id,
        approval_type="plan",
        status=ApprovalStatusEnum.PENDING.value,
        requested_by="worker",
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    await service.task_repo.create_approval(approval)
    await session.commit()

    # Reject
    rejected_task = await service.reject_task(
        task_id, rejecter="reviewer", reason="Architecture mismatch"
    )
    await session.commit()

    assert rejected_task.status == TaskStatusEnum.CANCELLED.value
    assert rejected_task.failure_reason == "Architecture mismatch"

    updated_app = await service.task_repo.get_latest_approval(task_id)
    assert updated_app is not None
    assert updated_app.status == ApprovalStatusEnum.REJECTED.value
    assert updated_app.approved_by == "reviewer"
    assert updated_app.comment == "Architecture mismatch"

    # Idempotent call
    rejected_again = await service.reject_task(task_id, rejecter="reviewer")
    assert rejected_again.status == TaskStatusEnum.CANCELLED.value


@pytest.mark.asyncio
async def test_retry_task_lifecycle(session: AsyncSession):
    from fastapi import HTTPException

    service = TaskService(session)
    task_id = str(uuid.uuid4())

    with tempfile.TemporaryDirectory() as tmp_ws:
        # 1. Attempt retry on active task -> 409
        active_task = Task(
            task_id=task_id,
            status=TaskStatusEnum.CODING.value,
            requested_task="In flight task",
            execution_target="local",
            workspace_path=tmp_ws,
        )
        await service.task_repo.create_task(active_task)
        await session.commit()

        with pytest.raises(HTTPException) as exc:
            await service.retry_task(task_id)
        assert exc.value.status_code == 409

        # 2. Mark task FAILED and retry -> success
        active_task.status = TaskStatusEnum.FAILED.value
        await service.task_repo.update_task(active_task)
        await session.commit()

        child = await service.retry_task(
            task_id, additional_instructions="Make sure tests pass this time"
        )
        await session.commit()

        assert child.parent_task_id == task_id
        assert child.status == TaskStatusEnum.PENDING.value
        assert "In flight task" in child.requested_task
        assert "Make sure tests pass this time" in child.requested_task

        # Check child has its own Job
        child_jobs = (
            (await session.execute(select(Job).where(Job.task_id == child.task_id))).scalars().all()
        )
        assert len(child_jobs) == 1
        assert child_jobs[0].status == JobStatusEnum.PENDING.value


@pytest.mark.asyncio
async def test_list_tasks_and_diff_retrieval(session: AsyncSession):
    service = TaskService(session)
    repo = "Digvijay-Bhota/codeforge-ai"

    # Seed tasks
    t1 = Task(
        task_id=str(uuid.uuid4()),
        status=TaskStatusEnum.COMPLETED.value,
        repository=repo,
        execution_target="github",
        requested_task="Implement task listing",
        implementation_plan={"steps": [{"step": 1, "description": "Step 1"}]},
        task_result={
            "diff": "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1,2 @@\n line1\n+line2\n",
            "changed_files": [{"path": "foo.py", "action": "modified"}],
        },
    )
    t2 = Task(
        task_id=str(uuid.uuid4()),
        status=TaskStatusEnum.WAITING_APPROVAL.value,
        repository=repo,
        execution_target="github",
        requested_task="Plan approval pending",
    )
    await service.task_repo.create_task(t1)
    await service.task_repo.create_task(t2)
    await session.commit()

    # List all
    tasks, total = await service.list_tasks(repository=repo)
    assert total >= 2
    task_ids = [t.task_id for t in tasks]
    assert t1.task_id in task_ids
    assert t2.task_id in task_ids

    # Filter by status
    waiting_tasks, count_w = await service.list_tasks(status=TaskStatusEnum.WAITING_APPROVAL.value)
    assert any(t.task_id == t2.task_id for t in waiting_tasks)
    assert not any(t.task_id == t1.task_id for t in waiting_tasks)

    # Diff retrieval
    diff_resp = await service.get_task_diff(t1.task_id)
    assert diff_resp.files_changed_count == 1
    assert diff_resp.additions == 1
    assert diff_resp.deletions == 0
    assert diff_resp.files[0].path == "foo.py"

    # Diff retrieval on task without diff
    diff_resp2 = await service.get_task_diff(t2.task_id)
    assert diff_resp2.files_changed_count == 0


# ── 5. FastAPI Endpoints Integration Tests ────────────────────────────────────


@pytest.mark.asyncio
async def test_api_list_tasks(async_client: AsyncClient):
    response = await async_client.get("/api/v1/tasks?limit=10")
    assert response.status_code == 200
    data = response.json()
    assert "total" in data
    assert "tasks" in data
    assert isinstance(data["tasks"], list)


@pytest.mark.asyncio
async def test_api_get_task_detail(async_client: AsyncClient):
    # Non-existent
    res_404 = await async_client.get(f"/api/v1/tasks/{uuid.uuid4()}")
    assert res_404.status_code == 404

    # Create task
    create_res = await async_client.post(
        "/api/v1/tasks",
        json={"workspace_path": "/tmp/test", "description": "Get task details test case"},
    )
    assert create_res.status_code == 200
    task_id = create_res.json()["task_id"]

    res = await async_client.get(f"/api/v1/tasks/{task_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["task_id"] == task_id
    assert body["status"] == "PENDING"
    assert "requested_task" in body


@pytest.mark.asyncio
async def test_api_approve_and_reject_endpoints(async_client: AsyncClient):
    # Non-existent approve
    res_404 = await async_client.post(
        f"/api/v1/tasks/{uuid.uuid4()}/approve", json={"comment": "approve"}
    )
    assert res_404.status_code == 404

    # Create a task in PENDING status -> trying to approve gives 409
    create_res = await async_client.post(
        "/api/v1/tasks",
        json={"workspace_path": "/tmp/test", "description": "Test approve active conflict"},
    )
    task_id = create_res.json()["task_id"]
    res_409 = await async_client.post(
        f"/api/v1/tasks/{task_id}/approve", json={"comment": "approve"}
    )
    assert res_409.status_code == 409


@pytest.mark.asyncio
async def test_api_diff_endpoint(async_client: AsyncClient):
    res_404 = await async_client.get(f"/api/v1/tasks/{uuid.uuid4()}/diff")
    assert res_404.status_code == 404

    create_res = await async_client.post(
        "/api/v1/tasks",
        json={"workspace_path": "/tmp/test", "description": "Test diff retrieval endpoint"},
    )
    task_id = create_res.json()["task_id"]
    diff_res = await async_client.get(f"/api/v1/tasks/{task_id}/diff")
    assert diff_res.status_code == 200
    data = diff_res.json()
    assert data["files_changed_count"] == 0


# ── 6. Worker Two-Stage Pause/Resume End-to-End Test ──────────────────────────


@pytest.mark.asyncio
async def test_worker_two_stage_pause_and_resumption(session: AsyncSession):
    """Verifies that:
    1. Stage A: Worker pauses at WAITING_APPROVAL, creates TaskApproval, completes Job, yields lease.
    2. API: approve_task moves task to QUEUED and creates new Job.
    3. Stage B: Worker resumes from QUEUED, skips planning, runs to COMPLETED.
    """
    bind_engine = session.bind
    test_session_maker = async_sessionmaker(
        bind_engine, class_=AsyncSession, expire_on_commit=False
    )

    service = TaskService(session)
    sample_plan = _sample_plan()

    # Step 1: Submit task requiring approval
    req = TaskRequest(
        workspace_path="/tmp/mock_repo",
        description="Implement bounded diff with approval pause",
        require_plan_approval=True,
        approval_config={"timeout_hours": 12},
    )
    task = await service.create_task(req)
    await session.commit()

    initial_job = (
        await session.execute(select(Job).where(Job.task_id == task.task_id))
    ).scalar_one()

    # Step 2: Worker processes Stage A (Plan only)
    stage_a_res = SchemaTaskResult(
        task_id=task.task_id,
        status=TaskStatus.success,
        description=req.description,
        plan=[
            TaskPlanStep(step=s.step_number, description=s.description) for s in sample_plan.steps
        ],
        changed_files=[],
        diff="",
        test_result=None,
        agent_output="Plan generated. Awaiting approval.",
        full_plan=sample_plan.model_dump(),
    )

    with (
        patch("app.worker.async_session_maker", test_session_maker),
        patch(
            "app.services.task_service.TaskService.execute_task",
            new=AsyncMock(return_value=stage_a_res),
        ),
    ):
        await process_job(initial_job.id)

    # Verify Stage A outcome:
    async with test_session_maker() as s2:
        t_repo = TaskRepository(s2)
        j_repo = JobRepository(s2)

        stage_a_task = await t_repo.get_task(task.task_id)
        assert stage_a_task is not None
        assert stage_a_task.status == TaskStatusEnum.WAITING_APPROVAL.value
        assert stage_a_task.implementation_plan is not None

        stage_a_job = await j_repo.get_job(initial_job.id)
        assert stage_a_job is not None
        assert stage_a_job.status == JobStatusEnum.SUCCEEDED.value  # Lease yielded cleanly!

        approval_rec = await t_repo.get_latest_approval(task.task_id)
        assert approval_rec is not None
        assert approval_rec.status == ApprovalStatusEnum.PENDING.value

    # Step 3: Human approves task
    async with test_session_maker() as s3:
        svc3 = TaskService(s3)
        approved_task = await svc3.approve_task(
            task.task_id, approver="tech-lead", comment="Plan looks good!"
        )
        await s3.commit()

        assert approved_task.status == TaskStatusEnum.QUEUED.value

        # Fetch resumed job
        all_jobs = (
            (
                await s3.execute(
                    select(Job).where(Job.task_id == task.task_id).order_by(Job.id.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(all_jobs) == 2
        resumed_job = all_jobs[1]
        assert resumed_job.status == JobStatusEnum.PENDING.value

    # Step 4: Worker processes Stage B (Resumed execution)
    stage_b_res = SchemaTaskResult(
        task_id=task.task_id,
        status=TaskStatus.success,
        description=req.description,
        plan=[
            TaskPlanStep(step=s.step_number, description=s.description) for s in sample_plan.steps
        ],
        changed_files=[ChangedFile(path="calculator.py", action="modified")],
        diff="--- a/calculator.py\n+++ b/calculator.py\n@@ -1 +1,2 @@\n+# Fix\n",
        test_result=TestResult(
            passed=True, exit_code=0, stdout="OK", stderr="", duration_seconds=1.2
        ),
        agent_output="Successfully implemented and tested changes.",
        full_plan=sample_plan.model_dump(),
    )

    with (
        patch("app.worker.async_session_maker", test_session_maker),
        patch(
            "app.services.task_service.TaskService.execute_task",
            new=AsyncMock(return_value=stage_b_res),
        ) as mock_exec,
    ):
        await process_job(resumed_job.id)

        # Ensure execute_task was called with initial_plan!
        call_kwargs = mock_exec.call_args.kwargs
        assert call_kwargs.get("initial_plan") is not None
        assert call_kwargs.get("stop_after_plan") is False

    # Verify Stage B outcome:
    async with test_session_maker() as s4:
        t_repo4 = TaskRepository(s4)
        j_repo4 = JobRepository(s4)

        final_task = await t_repo4.get_task(task.task_id)
        assert final_task is not None
        assert final_task.status == TaskStatusEnum.COMPLETED.value
        assert final_task.task_result is not None
        assert final_task.task_result["diff"] != ""

        final_job = await j_repo4.get_job(resumed_job.id)
        assert final_job is not None
        assert final_job.status == JobStatusEnum.SUCCEEDED.value


# ── 7. Hardening Regression Tests: Isolation, Concurrency & Expiration ───────


@pytest.mark.asyncio
async def test_retry_workspace_isolation(session: AsyncSession):
    """Prove that a retried local task receives an isolated directory without secrets, git, or caches."""
    with tempfile.TemporaryDirectory() as orig_dir:
        orig_path = Path(orig_dir)
        # Create normal files
        (orig_path / "calculator.py").write_text("def add(a, b): return a + b\n")
        (orig_path / "README.md").write_text("# Calculator\n")

        # Create sensitive files that must NOT leak into retries
        (orig_path / ".env").write_text("SECRET_KEY=supersecret123\n")
        (orig_path / "secret.key").write_text("PRIVATE_KEY_DATA\n")
        (orig_path / "id_rsa").write_text("SSH_KEY_DATA\n")

        # Create git and cache directories that must NOT leak into retries
        git_dir = orig_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")

        pycache_dir = orig_path / "__pycache__"
        pycache_dir.mkdir()
        (pycache_dir / "calculator.cpython-312.pyc").write_bytes(b"\x00\x01\x02")

        # Create completed original task
        service = TaskService(session)
        req = TaskRequest(
            workspace_path=str(orig_path),
            description="Initial local task",
        )
        orig_task = await service.create_task(req)
        sm = TaskStateMachine(TaskRepository(session))
        orig_task = await sm.transition(orig_task, TaskStatusEnum.QUEUED)
        orig_task = await sm.transition(orig_task, TaskStatusEnum.CODING)
        orig_task = await sm.transition(orig_task, TaskStatusEnum.TESTING)
        orig_task = await sm.transition(orig_task, TaskStatusEnum.COMPLETED)
        await session.commit()

        # Retry task
        child_task = await service.retry_task(
            orig_task.task_id, additional_instructions="Add multiplication"
        )
        await session.commit()

        assert child_task.workspace_path is not None
        assert child_task.workspace_path != str(orig_path)

        child_path = Path(child_task.workspace_path)
        assert child_path.exists()
        assert child_path.is_dir()

        # Verify source code is copied
        assert (child_path / "calculator.py").exists()
        assert (child_path / "calculator.py").read_text() == "def add(a, b): return a + b\n"
        assert (child_path / "README.md").exists()

        # Verify sensitive files and caches are excluded
        assert not (child_path / ".env").exists()
        assert not (child_path / "secret.key").exists()
        assert not (child_path / "id_rsa").exists()
        assert not (child_path / ".git").exists()
        assert not (child_path / "__pycache__").exists()

        # Verify file changes in child workspace do not mutate original workspace
        (child_path / "calculator.py").write_text("def add(a, b): return a + b + 1\n")
        assert (orig_path / "calculator.py").read_text() == "def add(a, b): return a + b\n"


@pytest.mark.asyncio
async def test_retry_workspace_missing_original(session: AsyncSession):
    """Prove that retrying a task whose original workspace was deleted fails cleanly with 400."""
    service = TaskService(session)
    orig_task = Task(
        task_id=str(uuid.uuid4()),
        status=TaskStatusEnum.COMPLETED.value,
        execution_target="local",
        workspace_path="/tmp/nonexistent_workspace_path_12345",
        requested_task="Task with missing workspace",
    )
    await service.task_repo.create_task(orig_task)
    await session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await service.retry_task(orig_task.task_id)
    assert exc_info.value.status_code == 400
    assert "no longer exists or is not a directory" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_retry_workspace_override_path_escape(session: AsyncSession):
    """Prove that providing a workspace override outside settings.workspace_root is rejected."""
    with tempfile.TemporaryDirectory() as root_dir:
        enforced_root = Path(root_dir).resolve()
        with patch("app.services.task_service.settings.workspace_root", str(enforced_root)):
            allowed_dir = enforced_root / "allowed"
            allowed_dir.mkdir()
            outside_dir = Path(tempfile.mkdtemp())

            service = TaskService(session)
            orig_task = Task(
                task_id=str(uuid.uuid4()),
                status=TaskStatusEnum.COMPLETED.value,
                execution_target="local",
                workspace_path=str(allowed_dir),
                requested_task="Task inside workspace_root",
            )
            await service.task_repo.create_task(orig_task)
            await session.commit()

            with pytest.raises(HTTPException) as exc_info:
                await service.retry_task(
                    orig_task.task_id, workspace_path_override=str(outside_dir)
                )
            assert exc_info.value.status_code == 400
            assert "outside enforced workspace root" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_concurrent_approvals_real_transactions(session: AsyncSession):
    """Prove that concurrent approval requests are locked and produce exactly one resumed job."""
    bind_engine = session.bind
    test_session_maker = async_sessionmaker(
        bind_engine, class_=AsyncSession, expire_on_commit=False
    )

    service = TaskService(session)
    req = TaskRequest(
        workspace_path="/tmp/mock_repo",
        description="Concurrent approval test",
        require_plan_approval=True,
    )
    task = await service.create_task(req)
    sm = TaskStateMachine(TaskRepository(session))
    task = await sm.transition(task, TaskStatusEnum.QUEUED)
    task = await sm.transition(task, TaskStatusEnum.ANALYZING)
    task = await sm.transition(task, TaskStatusEnum.PLANNING)
    task = await sm.transition(task, TaskStatusEnum.WAITING_APPROVAL)

    approval = TaskApproval(
        task_id=task.task_id,
        approval_type="plan",
        status=ApprovalStatusEnum.PENDING.value,
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    await TaskRepository(session).create_approval(approval)
    await session.commit()

    async def do_approve(approver_name: str):
        async with test_session_maker() as s:
            svc = TaskService(s)
            res = await svc.approve_task(task.task_id, approver=approver_name)
            await s.commit()
            return res

    results = await asyncio.gather(
        do_approve("approver-1"),
        do_approve("approver-2"),
        return_exceptions=True,
    )

    for r in results:
        assert not isinstance(r, Exception), f"Unexpected exception: {r}"

    async with test_session_maker() as s2:
        jobs = (await s2.execute(select(Job).where(Job.task_id == task.task_id))).scalars().all()
        assert len(jobs) == 2

        outbox_events = (
            (
                await s2.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "JOB_CREATED",
                        OutboxEvent.aggregate_id == str(jobs[1].id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(outbox_events) == 1


@pytest.mark.asyncio
async def test_concurrent_approve_and_reject_real_transactions(session: AsyncSession):
    """Prove that concurrent approve and reject resolve deterministically to exactly one outcome."""
    bind_engine = session.bind
    test_session_maker = async_sessionmaker(
        bind_engine, class_=AsyncSession, expire_on_commit=False
    )

    service = TaskService(session)
    req = TaskRequest(
        workspace_path="/tmp/mock_repo",
        description="Concurrent approve/reject race test",
        require_plan_approval=True,
    )
    task = await service.create_task(req)
    sm = TaskStateMachine(TaskRepository(session))
    task = await sm.transition(task, TaskStatusEnum.QUEUED)
    task = await sm.transition(task, TaskStatusEnum.ANALYZING)
    task = await sm.transition(task, TaskStatusEnum.PLANNING)
    task = await sm.transition(task, TaskStatusEnum.WAITING_APPROVAL)

    approval = TaskApproval(
        task_id=task.task_id,
        approval_type="plan",
        status=ApprovalStatusEnum.PENDING.value,
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    await TaskRepository(session).create_approval(approval)
    await session.commit()

    async def call_approve():
        async with test_session_maker() as s:
            svc = TaskService(s)
            res = await svc.approve_task(task.task_id, approver="alice")
            await s.commit()
            return res

    async def call_reject():
        async with test_session_maker() as s:
            svc = TaskService(s)
            res = await svc.reject_task(task.task_id, rejecter="bob", reason="rejected in race")
            await s.commit()
            return res

    results = await asyncio.gather(call_approve(), call_reject(), return_exceptions=True)

    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, HTTPException)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].status_code == 409

    async with test_session_maker() as s2:
        t_repo = TaskRepository(s2)
        final_t = await t_repo.get_task(task.task_id)
        assert final_t is not None
        assert final_t.status in (TaskStatusEnum.QUEUED.value, TaskStatusEnum.CANCELLED.value)
        final_app = await t_repo.get_latest_approval(task.task_id)
        assert final_app is not None
        if final_t.status == TaskStatusEnum.QUEUED.value:
            assert final_app.status == ApprovalStatusEnum.APPROVED.value
        else:
            assert final_app.status == ApprovalStatusEnum.REJECTED.value


@pytest.mark.asyncio
async def test_multiple_jobs_per_task_worker_claim_and_reaper_isolation(session: AsyncSession):
    """Prove worker claim isolation and reaper immunity across multiple jobs of the same task."""
    j_repo = JobRepository(session)
    t_repo = TaskRepository(session)

    task = Task(
        task_id=str(uuid.uuid4()),
        status=TaskStatusEnum.WAITING_APPROVAL.value,
        execution_target="local",
        requested_task="Multi-job isolation test",
    )
    await t_repo.create_task(task)

    job1 = Job(
        task_id=task.task_id,
        status=JobStatusEnum.SUCCEEDED.value,
        worker_id="worker-1",
        started_at=datetime.now(UTC) - timedelta(minutes=5),
        completed_at=datetime.now(UTC) - timedelta(minutes=4),
    )
    await j_repo.create_job(job1)

    job2 = Job(
        task_id=task.task_id,
        status=JobStatusEnum.PENDING.value,
    )
    await j_repo.create_job(job2)
    await session.commit()

    # 1. Verify get_job_by_task returns latest job (Job 2)
    latest = await j_repo.get_job_by_task(task.task_id)
    assert latest is not None
    assert latest.id == job2.id

    # 2. Worker claims Job 2
    claimed_job2 = await j_repo.claim_job(job2.id, worker_id="worker-2")
    assert claimed_job2 is not None
    assert claimed_job2.status == JobStatusEnum.RUNNING.value
    assert claimed_job2.worker_id == "worker-2"

    # 3. Attempting to claim Job 1 must fail because Job 1 is SUCCEEDED
    claimed_job1 = await j_repo.claim_job(job1.id, worker_id="worker-2")
    assert claimed_job1 is None

    # 4. Prove Job 1 remains SUCCEEDED
    job1_fresh = await j_repo.get_job(job1.id)
    assert job1_fresh is not None
    assert job1_fresh.status == JobStatusEnum.SUCCEEDED.value


@pytest.mark.asyncio
async def test_get_task_lazy_approval_expiration(async_client: AsyncClient, session: AsyncSession):
    """Prove GET /tasks/{task_id} lazily marks expired approvals and returns non-actionable status."""
    service = TaskService(session)
    req = TaskRequest(
        workspace_path="/tmp/mock_repo",
        description="Approval expiration test",
        require_plan_approval=True,
    )
    task = await service.create_task(req)
    sm = TaskStateMachine(TaskRepository(session))
    task = await sm.transition(task, TaskStatusEnum.QUEUED)
    task = await sm.transition(task, TaskStatusEnum.ANALYZING)
    task = await sm.transition(task, TaskStatusEnum.PLANNING)
    task = await sm.transition(task, TaskStatusEnum.WAITING_APPROVAL)

    approval = TaskApproval(
        task_id=task.task_id,
        approval_type="plan",
        status=ApprovalStatusEnum.PENDING.value,
        expires_at=datetime.now(UTC) - timedelta(hours=2),
    )
    await TaskRepository(session).create_approval(approval)
    await session.commit()

    resp = await async_client.get(f"/api/v1/tasks/{task.task_id}")
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "CANCELLED"
    assert data["failure_reason"] == "Approval request expired"
    assert data["latest_approval"] is not None
    assert data["latest_approval"]["status"] == "EXPIRED"


@pytest.mark.asyncio
async def test_concurrent_reject_and_reject_real_transactions(session: AsyncSession):
    """Case C: Prove concurrent reject requests resolve idempotently with no duplicate side effects."""
    bind_engine = session.bind
    test_session_maker = async_sessionmaker(
        bind_engine, class_=AsyncSession, expire_on_commit=False
    )

    service = TaskService(session)
    req = TaskRequest(
        workspace_path="/tmp/mock_repo",
        description="Concurrent reject/reject race test",
        require_plan_approval=True,
    )
    task = await service.create_task(req)
    sm = TaskStateMachine(TaskRepository(session))
    task = await sm.transition(task, TaskStatusEnum.QUEUED)
    task = await sm.transition(task, TaskStatusEnum.ANALYZING)
    task = await sm.transition(task, TaskStatusEnum.PLANNING)
    task = await sm.transition(task, TaskStatusEnum.WAITING_APPROVAL)

    approval = TaskApproval(
        task_id=task.task_id,
        approval_type="plan",
        status=ApprovalStatusEnum.PENDING.value,
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    await TaskRepository(session).create_approval(approval)
    await session.commit()

    async def do_reject(rejecter_name: str, reason: str):
        async with test_session_maker() as s:
            svc = TaskService(s)
            res = await svc.reject_task(task.task_id, rejecter=rejecter_name, reason=reason)
            await s.commit()
            return res

    results = await asyncio.gather(
        do_reject("rejecter-1", "First reject"),
        do_reject("rejecter-2", "Second reject"),
        return_exceptions=True,
    )

    for r in results:
        assert not isinstance(r, Exception), f"Unexpected exception in concurrent reject: {r}"

    async with test_session_maker() as s2:
        t_repo = TaskRepository(s2)
        final_t = await t_repo.get_task(task.task_id)
        assert final_t is not None
        assert final_t.status == TaskStatusEnum.CANCELLED.value

        final_app = await t_repo.get_latest_approval(task.task_id)
        assert final_app is not None
        assert final_app.status == ApprovalStatusEnum.REJECTED.value

        # Zero resumed jobs created (only the 1 initial job)
        jobs = (await s2.execute(select(Job).where(Job.task_id == task.task_id))).scalars().all()
        assert len(jobs) == 1

        # Zero new JOB_CREATED outbox events
        outbox_events = (
            (
                await s2.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "JOB_CREATED",
                        OutboxEvent.aggregate_id != str(jobs[0].id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(outbox_events) == 0


@pytest.mark.asyncio
async def test_approve_task_expired_real_transaction(session: AsyncSession):
    """Case D: Prove approve_task on expired approval cannot resume and transitions to CANCELLED."""
    service = TaskService(session)
    req = TaskRequest(
        workspace_path="/tmp/mock_repo",
        description="Approve expired test",
        require_plan_approval=True,
    )
    task = await service.create_task(req)
    sm = TaskStateMachine(TaskRepository(session))
    task = await sm.transition(task, TaskStatusEnum.QUEUED)
    task = await sm.transition(task, TaskStatusEnum.ANALYZING)
    task = await sm.transition(task, TaskStatusEnum.PLANNING)
    task = await sm.transition(task, TaskStatusEnum.WAITING_APPROVAL)

    approval = TaskApproval(
        task_id=task.task_id,
        approval_type="plan",
        status=ApprovalStatusEnum.PENDING.value,
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    await TaskRepository(session).create_approval(approval)
    await session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await service.approve_task(task.task_id, approver="late-lead")
    assert exc_info.value.status_code == 409
    assert "expired" in str(exc_info.value.detail).lower()

    # Verify task is CANCELLED and approval is EXPIRED
    await session.refresh(task)
    assert task.status == TaskStatusEnum.CANCELLED.value
    assert task.failure_reason == "Approval request expired"

    latest_app = await TaskRepository(session).get_latest_approval(task.task_id)
    assert latest_app is not None
    assert latest_app.status == ApprovalStatusEnum.EXPIRED.value

    # Zero resumed jobs created (only the 1 initial job)
    jobs = (await session.execute(select(Job).where(Job.task_id == task.task_id))).scalars().all()
    assert len(jobs) == 1

    # Zero resumed outbox events
    outbox_events = (
        (
            await session.execute(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == "JOB_CREATED",
                    OutboxEvent.aggregate_id != str(jobs[0].id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(outbox_events) == 0


@pytest.mark.asyncio
async def test_retry_workspace_independent_isolation(session: AsyncSession):
    """Prove that multiple retries of a task receive distinct directories and cannot mutate each other."""
    with tempfile.TemporaryDirectory() as root_dir:
        enforced_root = Path(root_dir).resolve()
        with patch("app.services.task_service.settings.workspace_root", str(enforced_root)):
            orig_dir = enforced_root / "orig_task_ws"
            orig_dir.mkdir()
            (orig_dir / "app.py").write_text("v = 1\n")

            service = TaskService(session)
            req = TaskRequest(
                workspace_path=str(orig_dir),
                description="Parent task",
            )
            orig_task = await service.create_task(req)
            sm = TaskStateMachine(TaskRepository(session))
            orig_task = await sm.transition(orig_task, TaskStatusEnum.QUEUED)
            orig_task = await sm.transition(orig_task, TaskStatusEnum.CODING)
            orig_task = await sm.transition(orig_task, TaskStatusEnum.TESTING)
            orig_task = await sm.transition(orig_task, TaskStatusEnum.COMPLETED)
            await session.commit()

            # Launch two retries
            retry1 = await service.retry_task(orig_task.task_id, additional_instructions="Retry 1")
            retry2 = await service.retry_task(orig_task.task_id, additional_instructions="Retry 2")
            await session.commit()

            # Prove workspaces are distinct
            assert retry1.workspace_path is not None
            assert retry2.workspace_path is not None
            assert retry1.workspace_path != retry2.workspace_path
            assert retry1.workspace_path != str(orig_dir)
            assert retry2.workspace_path != str(orig_dir)

            path1 = Path(retry1.workspace_path)
            path2 = Path(retry2.workspace_path)

            # Both directories exist and are inside enforced_root
            assert path1.is_dir()
            assert path2.is_dir()
            assert path1.relative_to(enforced_root)
            assert path2.relative_to(enforced_root)

            # File mutations in retry1 do not affect retry2 or orig_dir
            (path1 / "app.py").write_text("v = 2\n")
            (path2 / "app.py").write_text("v = 3\n")

            assert (path1 / "app.py").read_text() == "v = 2\n"
            assert (path2 / "app.py").read_text() == "v = 3\n"
            assert (orig_dir / "app.py").read_text() == "v = 1\n"
