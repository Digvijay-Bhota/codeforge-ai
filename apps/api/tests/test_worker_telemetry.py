"""Telemetry recorded while a job runs is committed with the job.

record_event() without an explicit session writes into the observability
context's session.  process_job used to put its claim session there, which is
closed by the time the job runs: the SQLAlchemy session silently began a new
transaction that nothing committed, so those events (every GitHub token mint,
for example) were lost and a pooled connection stayed checked out, idle in
transaction, until garbage collection.  The first real GitHub run logged two
GITHUB_OPERATION_COMPLETED events and stored none.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ObservabilityEvent, TaskStatusEnum
from app.observability.events import EventType
from app.observability.tracing import record_event
from tests.security.test_github_plan_gate import (  # noqa: F401
    comment_task,
    maker,
    plan_result,
    run_jobs,
    snapshot,
)


@pytest.mark.asyncio
async def test_events_recorded_during_a_job_are_committed(session: AsyncSession, maker: Any) -> None:  # noqa: F811
    task_id = await comment_task(session)

    async def execute(request: Any, tid: str, **kwargs: Any) -> Any:
        # As the token service does: no session argument, the context's session.
        await record_event(event_type=EventType.GITHUB_OPERATION_COMPLETED, component="github")
        return plan_result(tid)

    with patch("app.services.task_service.TaskService.execute_task", new=AsyncMock(side_effect=execute)):
        await run_jobs(maker, task_id)

    assert (await snapshot(maker, task_id))["status"] == TaskStatusEnum.WAITING_APPROVAL.value
    async with maker() as s:
        stored = (await s.execute(select(func.count()).select_from(ObservabilityEvent).where(
            ObservabilityEvent.task_id == task_id,
            ObservabilityEvent.event_type == EventType.GITHUB_OPERATION_COMPLETED.value,
        ))).scalar_one()  # fmt: skip
        assert stored == 1
        # And no connection was left holding an open transaction.
        idle = (await s.execute(text(
            "select count(*) from pg_stat_activity where datname = current_database() "
            "and state = 'idle in transaction' and pid <> pg_backend_pid()"
        ))).scalar_one()  # fmt: skip
    assert idle == 0
