"""Outbox persistence and delivery state; the retry policy is app/outbox_policy.py."""

import logging
from datetime import timedelta

from sqlalchemy import ColumnElement, and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.db.models import OutboxEvent
from app.observability.metrics import inc_counter
from app.outbox_policy import Decision, Failure, FailureCategory, decide

logger = logging.getLogger("outbox")


def _pending() -> ColumnElement[bool]:
    return and_(OutboxEvent.published_at.is_(None), OutboxEvent.failed_at.is_(None))


def _due() -> ColumnElement[bool]:
    """Pending, and its next attempt (if scheduled) is not in the future (database time)."""
    return and_(
        _pending(),
        or_(OutboxEvent.next_attempt_at.is_(None), OutboxEvent.next_attempt_at <= func.now()),
    )


class OutboxRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_event(self, event: OutboxEvent) -> OutboxEvent:
        self.session.add(event)
        await self.session.flush()
        return event

    async def get_unpublished_events(self, limit: int = 50) -> list[OutboxEvent]:
        """Due events, oldest first, locked (SKIP LOCKED) against other dispatchers.

        An event waiting for its next retry is not due, so it never holds up
        the events behind it.
        """
        stmt = (
            select(OutboxEvent)
            .where(_due())
            .order_by(OutboxEvent.created_at.asc(), OutboxEvent.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_unpublished_events_by_type(self, event_type: str, limit: int = 50) -> list[OutboxEvent]:
        stmt = (
            select(OutboxEvent)
            .where(_due(), OutboxEvent.event_type == event_type)
            .order_by(OutboxEvent.created_at.asc(), OutboxEvent.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def claim_unpublished(self, event_id: int) -> OutboxEvent | None:
        """Lock *event_id* for this transaction if it is still due and not locked elsewhere.

        The row is read afresh (populate_existing): an instance expired by an
        earlier commit or rollback in the same session is refreshed, not
        lazy-loaded.  None: delivered, given up, rescheduled, or another
        dispatcher holds it.
        """
        stmt = (
            select(OutboxEvent)
            .where(OutboxEvent.id == event_id, _due())
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_event(self, event_id: int) -> OutboxEvent | None:
        result = await self.session.execute(
            select(OutboxEvent).where(OutboxEvent.id == event_id)
        )
        return result.scalars().first()

    async def get_event_for_update(self, event_id: int) -> OutboxEvent | None:
        result = await self.session.execute(
            select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
        )
        return result.scalars().first()

    async def mark_published(self, event_id: int, payload_updates: dict | None = None) -> None:
        values: dict = {"published_at": func.now(), "next_attempt_at": None}
        if payload_updates:
            event = await self.get_event(event_id)
            if event:
                new_payload = dict(event.payload)
                new_payload.update(payload_updates)
                values["payload"] = new_payload
        await self.session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(**values)
        )

    async def record_failure(self, event_id: int, failure: Failure) -> Decision | None:
        """Record a failed attempt at *event_id*: schedule the next one, or give up.

        Call after rolling back the failed attempt's work, and commit after.
        The event is locked again first (SKIP LOCKED): None, and nothing
        recorded, when it was delivered or given up meanwhile, or another
        dispatcher holds it.  The payload is not touched.
        """
        row = (
            await self.session.execute(
                select(OutboxEvent, func.now())
                .where(OutboxEvent.id == event_id, _pending())
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        ).first()
        if row is None:
            return None
        event, now = row
        attempts = event.attempts + 1
        first_failed_at = event.first_failed_at or now
        decision = decide(
            failure,
            attempts=attempts,
            seconds_since_first_failure=(now - first_failed_at).total_seconds(),
        )
        values: dict = {
            "attempts": attempts,
            "first_failed_at": first_failed_at,
            "last_failed_at": now,
            "last_error": failure.error,
            "last_error_category": failure.category.value,
        }
        if decision.give_up:
            values.update(failed_at=now, next_attempt_at=None)
        else:
            values["next_attempt_at"] = now + timedelta(seconds=decision.delay or 0.0)
        await self.session.execute(update(OutboxEvent).where(OutboxEvent.id == event_id).values(**values))

        labels = {"event_type": event.event_type, "category": failure.category.value}
        if decision.give_up:
            inc_counter("codeforge_outbox_events_failed_total", labels=labels)
            logger.error(
                "Outbox event %s (%s) given up after %s attempt(s), %s: %s",
                event_id, event.event_type, attempts, decision.reason, failure.error,
            )  # fmt: skip
        else:
            inc_counter("codeforge_outbox_retries_scheduled_total", labels=labels)
            logger.warning(
                "Outbox event %s (%s) attempt %s failed (%s), %s: %s",
                event_id, event.event_type, attempts, failure.category.value, decision.reason, failure.error,
            )  # fmt: skip
        return decision

    async def mark_failed(self, event_id: int, error: str) -> Decision | None:
        """Give *event_id* up now: a permanent failure (malformed, refused, unknown type)."""
        return await self.record_failure(event_id, Failure(FailureCategory.PERMANENT, error))

    async def requeue(self, event_id: int) -> bool:
        """Make a failed event due again, with a fresh retry budget.  False: not a failed event.

        The last error and its category are kept, as the history of why it
        failed, until a new attempt replaces them.
        """
        result = await self.session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id == event_id,
                OutboxEvent.failed_at.is_not(None),
                OutboxEvent.published_at.is_(None),
            )
            .values(failed_at=None, attempts=0, first_failed_at=None, next_attempt_at=None)
        )
        return bool(getattr(result, "rowcount", 0))
