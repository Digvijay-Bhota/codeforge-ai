
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.db.models import OutboxEvent


class OutboxRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_event(self, event: OutboxEvent) -> OutboxEvent:
        self.session.add(event)
        await self.session.flush()
        return event

    async def get_unpublished_events(self, limit: int = 50) -> list[OutboxEvent]:
        # Lock rows for update to prevent concurrent dispatchers from grabbing the same
        stmt = (
            select(OutboxEvent)
            .where(OutboxEvent.published_at.is_(None))
            .order_by(OutboxEvent.created_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_unpublished_events_by_type(self, event_type: str, limit: int = 50) -> list[OutboxEvent]:
        stmt = (
            select(OutboxEvent)
            .where(
                OutboxEvent.published_at.is_(None),
                OutboxEvent.event_type == event_type,
            )
            .order_by(OutboxEvent.created_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

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
        values: dict = {"published_at": func.now()}
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

    async def mark_failed_terminal(
        self, event_id: int, error_msg: str, error_type: str = "TerminalError"
    ) -> None:
        event = await self.get_event(event_id)
        if event:
            new_payload = dict(event.payload)
            new_payload["error"] = error_msg
            new_payload["error_type"] = error_type
            new_payload["terminal_failure"] = True
            await self.session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == event_id)
                .values(payload=new_payload, published_at=func.now())
            )

    async def record_retry_attempt(
        self, event_id: int, error_msg: str, max_attempts: int = 3
    ) -> bool:
        """Record retry attempt. Returns True if event is still retryable, False if terminal."""
        event = await self.get_event(event_id)
        if not event:
            return False
        new_payload = dict(event.payload)
        attempts = new_payload.get("attempts", 0) + 1
        new_payload["attempts"] = attempts
        new_payload["last_error"] = error_msg
        if attempts >= max_attempts:
            new_payload["terminal_failure"] = True
            new_payload["max_attempts_exceeded"] = True
            await self.session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == event_id)
                .values(payload=new_payload, published_at=func.now())
            )
            return False
        else:
            await self.session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == event_id)
                .values(payload=new_payload)
            )
            return True

