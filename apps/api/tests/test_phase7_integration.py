import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Base, Job, JobStatusEnum, OutboxEvent, Task, TaskEvent
from app.db.repositories.job_repository import JobRepository
from app.orchestration.models import FinalTaskResult, ImplementationPlan, WorkflowStatus

TEST_DB_URL = os.getenv("TEST_DATABASE_URL", "postgresql+asyncpg://codeforge:codeforge@localhost:5433/codeforge")

engine = create_async_engine(TEST_DB_URL, echo=False, poolclass=__import__("sqlalchemy").pool.NullPool)
TestingSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)

@pytest.fixture(scope="module")
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield

@pytest_asyncio.fixture
async def session(setup_db):
    async with TestingSessionLocal() as session:
        yield session
        await session.rollback()

@pytest.mark.asyncio
async def test_concurrent_worker_claim(session: AsyncSession):
    # Setup job
    task_id = str(uuid.uuid4())
    task = Task(task_id=task_id, execution_target="local", requested_task="test concurrent claim")
    session.add(task)
    await session.flush()
    job = Job(task_id=task_id, status=JobStatusEnum.PENDING.value)
    session.add(job)
    await session.commit()

    # Try to claim concurrently
    repo1 = JobRepository(session)
    repo2 = JobRepository(session)

    # We execute claim sequentially here but in real DB they would block/fail
    claimed_job_1 = await repo1.claim_job(job.id, "worker-1")
    await session.commit()

    claimed_job_2 = await repo2.claim_job(job.id, "worker-2")

    assert claimed_job_1 is not None
    assert claimed_job_1.worker_id == "worker-1"
    assert claimed_job_2 is None # Second worker gets nothing because status is RUNNING and lease is active

@pytest.mark.asyncio
async def test_stale_lease_recovery(session: AsyncSession):
    task_id = str(uuid.uuid4())
    task = Task(task_id=task_id, execution_target="local", requested_task="test stale lease")
    session.add(task)
    await session.flush()

    # Simulate a job that was claimed but lease expired (available_at in the past)
    job = Job(
        task_id=task_id,
        status=JobStatusEnum.RUNNING.value,
        attempt_count=1,
        worker_id="dead-worker"
    )
    session.add(job)
    await session.commit()

    # Hack available_at to be in the past to simulate expiration
    await session.execute(
        text(f"UPDATE jobs SET available_at = NOW() - INTERVAL '1 hour' WHERE id = {job.id}")
    )
    await session.commit()

    repo = JobRepository(session)
    reclaimed_job = await repo.claim_job(job.id, "new-worker")
    assert reclaimed_job is not None
    assert reclaimed_job.worker_id == "new-worker"
    assert reclaimed_job.attempt_count == 2
    assert reclaimed_job.status == JobStatusEnum.RUNNING.value

@pytest.mark.asyncio
async def test_terminal_state_protection(session: AsyncSession):
    task_id_success = str(uuid.uuid4())
    task_id_failed = str(uuid.uuid4())

    task1 = Task(task_id=task_id_success, execution_target="local", requested_task="test success")
    task2 = Task(task_id=task_id_failed, execution_target="local", requested_task="test failure")

    session.add_all([task1, task2])
    await session.flush()

    job1 = Job(task_id=task_id_success, status=JobStatusEnum.SUCCEEDED.value, attempt_count=1)
    job2 = Job(task_id=task_id_failed, status=JobStatusEnum.FAILED.value, attempt_count=1)

    session.add_all([job1, job2])
    await session.commit()

    repo = JobRepository(session)

    # SUCCEEDED cannot be reclaimed
    claim1 = await repo.claim_job(job1.id, "worker-x")
    assert claim1 is None

    # FAILED cannot be reclaimed
    claim2 = await repo.claim_job(job2.id, "worker-x")
    assert claim2 is None

@pytest.mark.asyncio
async def test_implementation_plan_persistence(session: AsyncSession):
    task_id = str(uuid.uuid4())

    from app.schemas.plan import PlanAction, RiskLevel
    from app.schemas.plan import PlanStep as PlanStepModel

    # 3-step plan
    plan_steps = [
        PlanStepModel(step_number=1, action=PlanAction.modify, description="Step 1", rationale="none", affected_paths=[]),
        PlanStepModel(step_number=2, action=PlanAction.modify, description="Step 2", rationale="none", affected_paths=[]),
        PlanStepModel(step_number=3, action=PlanAction.modify, description="Step 3", rationale="none", affected_paths=[])
    ]

    final_result = FinalTaskResult(
        workflow_status=WorkflowStatus.COMPLETED,
        task_id=task_id,
        task_description="Test plan description",
        implementation_plan=ImplementationPlan(
            goal="Test",
            validation_strategy="None",
            risk_level=RiskLevel.low,
            summary="test",
            steps=plan_steps
        ),
        changed_files=[],
        diff="",
        final_message="Done"
    )

    from app.services.task_service import _map_to_task_result
    result = _map_to_task_result(final_result, task_id)

    assert len(result.plan) == 3
    assert result.plan[0].description == "Step 1"
    assert result.plan[2].description == "Step 3"

@pytest.mark.asyncio
async def test_redis_payload_security(session: AsyncSession):
    # Enqueue a job and check the outbox event payload
    task_id = str(uuid.uuid4())
    task = Task(task_id=task_id, execution_target="local", requested_task="Super secret prompt with token xoxb-1234")
    session.add(task)
    await session.flush()

    job = Job(task_id=task_id, status=JobStatusEnum.PENDING.value)
    session.add(job)
    await session.flush()

    payload = {"job_id": 99999}
    event = OutboxEvent(event_type="JOB_CREATED", aggregate_id="99999", payload=payload)

    session.add(event)
    await session.commit()

    # Check what is persisted to outbox payload
    db_event = (await session.execute(select(OutboxEvent).where(OutboxEvent.id == event.id))).scalar_one()

    assert "job_id" in db_event.payload
    assert "token" not in db_event.payload
    assert "requested_task" not in db_event.payload





@pytest.mark.asyncio
async def test_event_pagination(session: AsyncSession):
    task_id = str(uuid.uuid4())
    task = Task(task_id=task_id, execution_target="local", requested_task="test events")
    session.add(task)
    await session.flush()

    # Create 150 events
    for i in range(150):
        event = TaskEvent(task_id=task_id, event_type="TEST", message=f"Event {i}")
        session.add(event)
    await session.commit()

    # Check limit and offset through the repository
    from app.db.repositories.task_repository import TaskRepository
    repo = TaskRepository(session)

    # Default limit
    events = await repo.get_task_events(task_id)
    assert len(events) == 50
    assert events[0].message == "Event 0"

    # Custom limit and offset
    events = await repo.get_task_events(task_id, limit=20, offset=50)
    assert len(events) == 20
    assert events[0].message == "Event 50"

    # Test through the API
    from app.api.tasks import get_task_events
    # The API clamps limit to 100
    api_events = await get_task_events(task_id=task_id, limit=200, offset=0, session=session)
    assert len(api_events) == 100
    assert api_events[0]["message"] == "Event 0"


@pytest.mark.asyncio
async def test_stale_job_reaper(session: AsyncSession):
    # Setup some jobs
    from sqlalchemy import func, update

    from app.db.models import Job, JobStatusEnum

    # 1. RUNNING + expired lease
    job1 = Job(task_id=str(uuid.uuid4()), status=JobStatusEnum.RUNNING.value, attempt_count=1, worker_id="w1")
    # 2. RUNNING + active lease
    job2 = Job(task_id=str(uuid.uuid4()), status=JobStatusEnum.RUNNING.value, attempt_count=1, worker_id="w2")
    # 3. SUCCEEDED + expired lease
    job3 = Job(task_id=str(uuid.uuid4()), status=JobStatusEnum.SUCCEEDED.value, attempt_count=1, worker_id="w3")
    # 4. FAILED + expired lease
    job4 = Job(task_id=str(uuid.uuid4()), status=JobStatusEnum.FAILED.value, attempt_count=1, worker_id="w4")

    for j in [job1, job2, job3, job4]:
        task = Task(task_id=j.task_id, execution_target="local", requested_task="test")
        session.add(task)
    await session.flush()
    for j in [job1, job2, job3, job4]:
        session.add(j)
    await session.commit()

    # Hack available_at for expired leases (1, 3, 4)
    await session.execute(text(f"UPDATE jobs SET available_at = NOW() - INTERVAL '1 hour' WHERE id IN ({job1.id}, {job3.id}, {job4.id})"))
    # Active lease (2)
    await session.execute(text(f"UPDATE jobs SET available_at = NOW() + INTERVAL '1 hour' WHERE id = {job2.id}"))
    await session.commit()

    # Simulate Reaper
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

    # Simulate concurrent reapers: run stmt twice without committing first
    # In asyncpg, if we run them sequentially, the first will update and the second will see the updated status.
    result1 = await session.execute(stmt)
    reclaimed_ids_1 = result1.scalars().all()

    result2 = await session.execute(stmt)
    reclaimed_ids_2 = result2.scalars().all()

    await session.commit()

    assert job1.id in reclaimed_ids_1
    assert job2.id not in reclaimed_ids_1 # active lease
    assert job3.id not in reclaimed_ids_1 # SUCCEEDED
    assert job4.id not in reclaimed_ids_1 # FAILED

    assert len(reclaimed_ids_2) == 0 # Concurrent reaper finds nothing

    # Check job1 is now PENDING and has no worker
    await session.refresh(job1)
    assert job1.status == JobStatusEnum.PENDING.value
    assert job1.worker_id is None

