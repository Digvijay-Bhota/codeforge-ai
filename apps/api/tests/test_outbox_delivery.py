"""Outbox delivery state through failures, interruptions and operator recovery.

The real dispatcher and repository against PostgreSQL; the job queue is a
stand-in that can fail like Redis does.  "Time passing" moves an event's
retry time (or the start of its retry window) instead of sleeping.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db.models import AuditEvent, OutboxEvent
from app.db.repositories.outbox_repository import OutboxRepository
from app.github.exceptions import GitHubRateLimitError
from app.outbox_admin import list_events, queue_status, requeue
from app.outbox_dispatcher import dispatch_batch
from app.outbox_policy import Failure, FailureCategory


class Queue:
    """The job queue; *down* makes enqueue fail as Redis does when unreachable."""

    def __init__(self) -> None:
        self.down = False
        self.enqueued: list[int] = []

    async def enqueue(self, job_id: int) -> None:
        if self.down:
            raise RedisConnectionError("Error 111 connecting to redis:6379. Connection refused.")
        self.enqueued.append(job_id)


@pytest.fixture
def maker(session: AsyncSession) -> Any:
    return async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)


async def add(maker: Any, *events: OutboxEvent) -> list[int]:
    async with maker() as s:
        s.add_all(events)
        await s.commit()
        return [e.id for e in events]


def job(job_id: int) -> OutboxEvent:
    return OutboxEvent(event_type="JOB_CREATED", aggregate_id=str(job_id), payload={"job_id": job_id})


async def dispatch(maker: Any, queue: Queue) -> int:
    async with maker() as s:
        return await dispatch_batch(session=s, queue=queue)  # type: ignore[arg-type]


async def state(maker: Any, event_id: int) -> OutboxEvent:
    async with maker() as s:
        event = await s.get(OutboxEvent, event_id)
        assert event is not None
        return event


async def time_passes(maker: Any, *, window: bool = False) -> None:
    """Scheduled retries become due; with *window*, the retry window since the first failure has passed."""
    values: dict[str, Any] = {"next_attempt_at": func.now()}
    if window:
        values["first_failed_at"] = func.now() - timedelta(seconds=settings.outbox_retry_max_age_seconds + 1)
    async with maker() as s:
        await s.execute(update(OutboxEvent).where(OutboxEvent.next_attempt_at.is_not(None)).values(**values))
        await s.commit()


@pytest.mark.asyncio
async def test_an_unknown_event_type_fails_visibly_instead_of_being_dropped(maker: Any) -> None:
    [event_id] = await add(maker, OutboxEvent(event_type="NO_SUCH_EVENT", aggregate_id="x", payload={}))
    await dispatch(maker, Queue())
    event = await state(maker, event_id)
    assert event.published_at is None and event.failed_at is not None  # used to be "published", silently
    assert event.last_error_category == "permanent" and "NO_SUCH_EVENT" in (event.last_error or "")

    await time_passes(maker)
    await dispatch(maker, Queue())
    assert (await state(maker, event_id)).attempts == 1  # never retried by itself


@pytest.mark.asyncio
async def test_a_redis_outage_is_retried_with_growing_delays_then_delivered(maker: Any) -> None:
    queue = Queue()
    queue.down = True
    [event_id] = await add(maker, job(7))
    delays = []
    for attempt in (1, 2, 3, 4):
        await dispatch(maker, queue)
        event = await state(maker, event_id)
        assert event.attempts == attempt and event.last_error_category == "transient"
        assert event.next_attempt_at is not None and event.last_failed_at is not None
        delays.append((event.next_attempt_at - event.last_failed_at).total_seconds())
        assert "Connection refused" in (event.last_error or "")
        await time_passes(maker)
    initial = settings.outbox_retry_initial_delay_seconds
    for n, delay in enumerate(delays):  # [initial/2, initial] * 2**n, with jitter
        assert initial * 2**n * 0.5 <= delay <= initial * 2**n + 0.001

    queue.down = False
    await dispatch(maker, queue)
    event = await state(maker, event_id)
    assert queue.enqueued == [7] and event.published_at is not None and event.next_attempt_at is None


@pytest.mark.asyncio
async def test_retries_run_out_then_an_operator_requeue_delivers_the_event(maker: Any) -> None:
    queue = Queue()
    queue.down = True
    [event_id] = await add(maker, job(8))
    await dispatch(maker, queue)
    await time_passes(maker, window=True)
    await dispatch(maker, queue)
    event = await state(maker, event_id)
    assert event.failed_at is not None and event.published_at is None and event.attempts == 2

    queue.down = False  # Redis is back; nothing happens until an operator acts
    await time_passes(maker)
    await dispatch(maker, queue)
    assert queue.enqueued == []

    async with maker() as s:
        assert await requeue(s, [event_id], by="ops-alice", reason="Redis restored") == {event_id: True}
    event = await state(maker, event_id)
    assert event.failed_at is None and event.attempts == 0 and event.first_failed_at is None
    assert event.last_error is not None  # kept as history until a new attempt replaces it
    await dispatch(maker, queue)
    assert queue.enqueued == [8] and (await state(maker, event_id)).published_at is not None

    async with maker() as s:
        audit = (
            await s.execute(select(AuditEvent).where(AuditEvent.event_type == "OUTBOX_EVENT_REQUEUED",
                                                     AuditEvent.resource_id == str(event_id)))  # fmt: skip
        ).scalar_one()
        assert audit.actor_type == "OPERATOR" and audit.actor_id == "ops-alice"
        failed_audit = (
            await s.execute(select(AuditEvent).where(AuditEvent.event_type == "OUTBOX_EVENT_FAILED",
                                                     AuditEvent.resource_id == str(event_id)))  # fmt: skip
        ).scalar_one()
        assert failed_audit.result == "FAILED"


@pytest.mark.asyncio
async def test_requeue_refuses_events_that_did_not_fail(maker: Any) -> None:
    queue = Queue()
    delivered, pending = await add(maker, job(1), OutboxEvent(event_type="JOB_CREATED", aggregate_id="2",
                                                               payload={"job_id": 2}))  # fmt: skip
    await dispatch(maker, queue)  # both delivered
    [waiting] = await add(maker, job(3))
    async with maker() as s:
        result = await requeue(s, [delivered, waiting, 999_999], by="ops", reason="test")
    assert result == {delivered: False, waiting: False, 999_999: False}
    assert (await state(maker, delivered)).published_at is not None


@pytest.mark.asyncio
async def test_a_dispatcher_that_dies_after_claiming_loses_nothing(maker: Any) -> None:
    [event_id] = await add(maker, job(11))
    async with maker() as dying:
        assert await OutboxRepository(dying).claim_unpublished(event_id) is not None
        # The process dies here: its transaction is rolled back with the connection.
        await dying.rollback()
    event = await state(maker, event_id)
    assert event.published_at is None and event.attempts == 0  # no failure recorded, still due

    queue = Queue()
    await dispatch(maker, queue)
    assert queue.enqueued == [11]


@pytest.mark.asyncio
async def test_a_claimed_event_is_skipped_by_another_dispatcher(maker: Any) -> None:
    [event_id] = await add(maker, job(12))
    queue = Queue()
    async with maker() as holder:
        assert await OutboxRepository(holder).claim_unpublished(event_id) is not None
        assert await dispatch(maker, queue) == 0  # locked: not handled twice
        await holder.rollback()
    assert queue.enqueued == []
    await dispatch(maker, queue)
    assert queue.enqueued == [12]


@pytest.mark.asyncio
async def test_an_event_waiting_for_its_retry_does_not_hold_up_later_ones(maker: Any) -> None:
    queue = Queue()
    queue.down = True
    [stuck] = await add(maker, job(21))
    await dispatch(maker, queue)  # scheduled for later
    queue.down = False
    later = await add(maker, job(22), job(23))
    await dispatch(maker, queue)
    assert queue.enqueued == [22, 23]
    assert all([(await state(maker, e)).published_at is not None for e in later])
    assert (await state(maker, stuck)).published_at is None


@pytest.mark.asyncio
async def test_a_rate_limit_is_not_retried_before_github_allows(maker: Any) -> None:
    [event_id] = await add(maker, job(31))
    async with maker() as s:
        repo = OutboxRepository(s)
        await repo.claim_unpublished(event_id)
        failure = Failure(FailureCategory.RATE_LIMITED, "GitHubRateLimitError: limit", retry_after=900)
        decision = await repo.record_failure(event_id, failure)
        await s.commit()
    assert decision is not None and not decision.give_up and decision.delay == 900
    event = await state(maker, event_id)
    assert event.next_attempt_at is not None and event.last_failed_at is not None
    assert (event.next_attempt_at - event.last_failed_at).total_seconds() == pytest.approx(900)
    assert isinstance(GitHubRateLimitError("x", retry_after=900).retry_after, int)


@pytest.mark.asyncio
async def test_a_failure_is_not_recorded_on_an_event_delivered_meanwhile(maker: Any) -> None:
    [event_id] = await add(maker, job(41))
    async with maker() as s:
        await OutboxRepository(s).mark_published(event_id)
        await s.commit()
    async with maker() as s:
        assert await OutboxRepository(s).record_failure(event_id, Failure(FailureCategory.TRANSIENT, "late")) is None
        await s.commit()
    event = await state(maker, event_id)
    assert event.attempts == 0 and event.failed_at is None and event.last_error is None


@pytest.mark.asyncio
async def test_the_operator_view_counts_the_queue_and_never_shows_payloads(maker: Any) -> None:
    secret = "ghs_" + "x" * 36
    queue = Queue()
    queue.down = True
    failing, scheduled = await add(
        maker,
        OutboxEvent(event_type="NO_SUCH_EVENT", aggregate_id="a", payload={"note": secret}),
        OutboxEvent(event_type="JOB_CREATED", aggregate_id="9", payload={"job_id": 9, "note": secret}),
    )
    await dispatch(maker, queue)
    async with maker() as s:
        status = await queue_status(s)
        failed = await list_events(s, failed=True)
        waiting = await list_events(s, failed=False)
    assert status["failed"] == 1 and status["scheduled_for_retry"] == 1 and status["due_now"] == 0
    assert status["oldest_pending_age_seconds"] is not None and status["next_retry_at"] is not None
    assert {"event_type": "NO_SUCH_EVENT", "category": "permanent", "count": 1} in status["failed_by_type"]
    assert [e["id"] for e in failed] == [failing] and [e["id"] for e in waiting] == [scheduled]
    assert waiting[0]["category"] == "transient" and waiting[0]["attempts"] == 1
    assert secret not in json.dumps([status, failed, waiting])
