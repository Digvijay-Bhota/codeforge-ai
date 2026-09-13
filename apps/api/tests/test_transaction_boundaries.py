import os
import uuid
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Base, Job, Task
from app.schemas.task import ExecutionTarget, TaskRequest
from app.services.task_service import TaskService

TEST_DB_URL = os.getenv("TEST_DATABASE_URL", os.getenv("DATABASE_URL", "postgresql+asyncpg://codeforge:codeforge@localhost:5433/codeforge"))

engine = create_async_engine(TEST_DB_URL, echo=False)
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
async def test_task_creation_rollback(session: AsyncSession):
    service = TaskService(session)
    req = TaskRequest(description="Test description", workspace_path="/tmp", execution_target=ExecutionTarget.local)

    test_task_id = str(uuid.uuid4())

    async def failing_create_event(*args, **kwargs):
        raise ValueError("Simulated DB failure during outbox creation")

    service.outbox_repo.create_event = failing_create_event

    with patch("app.services.task_service.uuid.uuid4", return_value=uuid.UUID(test_task_id)):
        try:
            await service.create_task(req)
            await session.commit()
            pytest.fail("Should have raised ValueError")
        except ValueError:
            await session.rollback()

    # Verify neither Task nor Job was persisted
    task = (await session.execute(select(Task).where(Task.task_id == test_task_id))).scalar_one_or_none()
    assert task is None, "Task should not exist"

    job = (await session.execute(select(Job).where(Job.task_id == test_task_id))).scalar_one_or_none()
    assert job is None, "Job should not exist"

