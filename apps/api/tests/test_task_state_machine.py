import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Task, TaskStatusEnum
from app.db.repositories.task_repository import TaskRepository
from app.services.task_state_machine import InvalidTaskTransitionError, TaskStateMachine


@pytest.mark.asyncio
async def test_state_machine_valid_transitions(session: AsyncSession):
    task_id = str(uuid.uuid4())
    task = Task(task_id=task_id, status=TaskStatusEnum.PENDING.value, execution_target="local", requested_task="state test")
    session.add(task)
    await session.commit()

    repo = TaskRepository(session)
    sm = TaskStateMachine(repo)

    # PENDING -> QUEUED
    task = await sm.transition(task, TaskStatusEnum.QUEUED)
    assert task.status == TaskStatusEnum.QUEUED.value

    # QUEUED -> ANALYZING
    task = await sm.transition(task, TaskStatusEnum.ANALYZING)
    await session.refresh(task)
    assert task.status == TaskStatusEnum.ANALYZING.value
    assert task.started_at is not None

    # ANALYZING -> PLANNING
    task = await sm.transition(task, TaskStatusEnum.PLANNING)
    assert task.status == TaskStatusEnum.PLANNING.value

    # PLANNING -> CODING
    task = await sm.transition(task, TaskStatusEnum.CODING)
    assert task.status == TaskStatusEnum.CODING.value

    # CODING -> TESTING
    task = await sm.transition(task, TaskStatusEnum.TESTING)
    assert task.status == TaskStatusEnum.TESTING.value

    # TESTING -> COMPLETED
    task = await sm.transition(task, TaskStatusEnum.COMPLETED)
    await session.refresh(task)
    assert task.status == TaskStatusEnum.COMPLETED.value
    assert task.completed_at is not None

    # Verify events
    events = await repo.get_task_events(task_id)
    event_types = [e.event_type for e in events]
    assert "TASK_QUEUED" in event_types
    assert "TASK_ANALYZING" in event_types
    assert "TASK_PLANNING" in event_types
    assert "TASK_CODING" in event_types
    assert "TASK_TESTING" in event_types
    assert "TASK_COMPLETED" in event_types

@pytest.mark.asyncio
async def test_state_machine_invalid_transition(session: AsyncSession):
    task_id = str(uuid.uuid4())
    task = Task(task_id=task_id, status=TaskStatusEnum.PENDING.value, execution_target="local", requested_task="state test")
    session.add(task)
    await session.commit()

    repo = TaskRepository(session)
    sm = TaskStateMachine(repo)

    with pytest.raises(InvalidTaskTransitionError):
        # Cannot go PENDING -> CODING directly
        await sm.transition(task, TaskStatusEnum.CODING)
