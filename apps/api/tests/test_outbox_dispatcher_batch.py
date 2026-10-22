"""The outbox dispatcher handles each event of a batch on its own, exactly once.

dispatch_batch fetches a batch with FOR UPDATE SKIP LOCKED, then commits (or
rolls back) after every event.  Two defects followed:

* After a failing event's rollback, the next event's attributes were expired,
  and reading them in async code raised MissingGreenlet: the whole batch was
  abandoned, and every event queued behind a failing one waited for it to
  exhaust its retries (found by tests/e2e/test_github_failures.py, where a
  GitHub outage failed status updates).
* The first commit released the batch's row locks, so a second dispatcher
  could take an event this one was still going to handle, and both did.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import OutboxEvent
from app.outbox_dispatcher import dispatch_batch


class RecordingQueue:
    def __init__(self, name: str, on_enqueue: Any = None) -> None:
        self.name = name
        self.enqueued: list[int] = []
        self.on_enqueue = on_enqueue

    async def enqueue(self, job_id: int) -> None:
        if self.on_enqueue is not None:
            await self.on_enqueue(job_id)
        self.enqueued.append(job_id)


async def add_events(session: AsyncSession, *events: OutboxEvent) -> list[int]:
    for event in events:
        session.add(event)
        await session.flush()
    await session.commit()
    return [e.id for e in events]


def job_created(job_id: int) -> OutboxEvent:
    return OutboxEvent(event_type="JOB_CREATED", aggregate_id=str(job_id), payload={"job_id": job_id})


async def published(maker: Any) -> dict[int, bool]:
    async with maker() as s:
        return {e.id: e.published_at is not None for e in (await s.execute(select(OutboxEvent))).scalars()}


@pytest.mark.asyncio
async def test_a_failing_event_does_not_stop_the_rest_of_its_batch(session: AsyncSession) -> None:
    maker = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
    status = OutboxEvent(event_type="GITHUB_STATUS_UPDATE", aggregate_id="t-1", payload={"task_id": "t-1"})
    ids = await add_events(session, status, job_created(101), job_created(102))

    async def fails(self: Any, event: OutboxEvent) -> None:
        raise RuntimeError("GitHub is down")

    queue = RecordingQueue("a")
    with patch("app.github.status_consumer.GitHubStatusConsumer.process_event", new=fails):
        async with maker() as s:
            processed = await dispatch_batch(session=s, queue=queue)  # type: ignore[arg-type]

    assert processed == 2
    assert queue.enqueued == [101, 102]
    state = await published(maker)
    assert state == {ids[0]: False, ids[1]: True, ids[2]: True}  # the failing one waits for its retry
    async with maker() as s:
        failed = await s.get(OutboxEvent, ids[0])
        assert failed is not None and failed.payload["attempts"] == 1 and failed.payload["last_error"] == "GitHub is down"


@pytest.mark.asyncio
async def test_two_dispatchers_never_handle_the_same_event(session: AsyncSession) -> None:
    maker = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
    await add_events(session, job_created(201), job_created(202))
    second = RecordingQueue("b")
    ran_second = False

    async def run_second_dispatcher(job_id: int) -> None:
        # Dispatcher A has committed event 201 and is about to hand 202 to the
        # queue: the moment another dispatcher would find 202 unlocked.
        nonlocal ran_second
        if job_id == 202 and not ran_second:
            ran_second = True
            async with maker() as s:
                await dispatch_batch(session=s, queue=second)  # type: ignore[arg-type]

    first = RecordingQueue("a", on_enqueue=run_second_dispatcher)
    async with maker() as s:
        await dispatch_batch(session=s, queue=first)  # type: ignore[arg-type]

    assert ran_second
    assert first.enqueued == [201, 202]
    assert second.enqueued == []  # 202 was A's; B skipped it
    assert all((await published(maker)).values())
