import asyncio
import logging
import random
import signal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.repositories.outbox_repository import OutboxRepository
from app.db.session import async_session_maker
from app.github.command_consumer import GitHubCommandConsumer
from app.github.exceptions import CommandConsumerError
from app.github.status_consumer import GitHubStatusConsumer
from app.observability.events import AuditEventType
from app.observability.tracing import record_audit_event
from app.outbox_policy import PermanentDeliveryError, classify, describe
from app.services.queue_service import QueueService

logger = logging.getLogger("outbox")


async def dispatch_batch(
    limit: int = 50,
    session: AsyncSession | None = None,
    queue: QueueService | None = None,
    github_client: Any = None,
) -> int:
    """Process a bounded batch of unpublished outbox events.

    Supports GITHUB_COMMAND_INGESTED, GITHUB_STATUS_UPDATE, and JOB_CREATED events.
    Returns the number of processed events.
    """
    if queue is None:
        queue = QueueService()

    if session is not None:
        return await _process_batch_with_session(session, queue, limit, github_client=github_client)

    async with async_session_maker() as new_session:
        return await _process_batch_with_session(new_session, queue, limit, github_client=github_client)


async def _process_batch_with_session(
    session: AsyncSession,
    queue: QueueService,
    limit: int,
    github_client: Any = None,
) -> int:
    repo = OutboxRepository(session)
    events = await repo.get_unpublished_events(limit=limit)
    if not events:
        return 0

    # Each event is handled in its own transaction, so the batch's row locks
    # end with the first commit or rollback, and a rollback expires the other
    # instances.  Claim every event again, freshly and locked, just before
    # handling it: another dispatcher may have taken it in the meantime.
    event_ids = [event.id for event in events]
    processed_count = 0
    for event_id in event_ids:
        claimed = await repo.claim_unpublished(event_id)
        if claimed is None:
            continue
        event = claimed
        event_type = event.event_type
        try:
            if event_type == "JOB_CREATED":
                job_id = event.payload.get("job_id")
                if job_id:
                    await queue.enqueue(job_id)
                await repo.mark_published(event_id)
                await session.commit()
            elif event_type == "GITHUB_COMMAND_INGESTED":
                consumer = GitHubCommandConsumer(session, queue=queue)
                await consumer.process_event(event)
            elif event_type in STATUS_EVENT_TYPES:
                # Records its own failures (it knows the task and the GitHub state).
                status_consumer = GitHubStatusConsumer(session, github_client=github_client)
                await status_consumer.process_event(event)
            else:
                raise PermanentDeliveryError(f"Unrecognized outbox event type {event_type!r}")
            processed_count += 1
        except CommandConsumerError:
            # Permanent refusals; the consumer already recorded the event as failed.
            processed_count += 1
        except Exception as exc:
            await session.rollback()
            await record_failure(session, repo, event_id, event_type, exc)

    return processed_count


STATUS_EVENT_TYPES = frozenset(
    {"GITHUB_STATUS_UPDATE", "GITHUB_COMMAND_ACCEPTED", "JOB_STARTED", "JOB_COMPLETED", "JOB_FAILED"}
)


async def record_failure(
    session: AsyncSession, repo: OutboxRepository, event_id: int, event_type: str, exc: BaseException
) -> None:
    """Record a failed attempt (the caller rolled its work back): retry later, or give up."""
    try:
        decision = await repo.record_failure(event_id, classify(exc))
        if decision is not None and decision.give_up:
            await record_audit_event(
                session,
                event_type=AuditEventType.OUTBOX_EVENT_FAILED,
                actor_type="SYSTEM",
                resource_type="outbox_event",
                resource_id=str(event_id),
                result="FAILED",
                metadata={"event_type": event_type, "reason": decision.reason},
            )
        await session.commit()
    except Exception as record_exc:
        # Not recorded: the event stays due and is attempted again.
        logger.error("Failed to record the failure of outbox event %s: %s", event_id, describe(record_exc))
        await session.rollback()


async def dispatch_outbox():
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def handle_sigterm():
        logger.info("Received termination signal, shutting down gracefully...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_sigterm)

    queue = QueueService()
    logger.info("Outbox dispatcher started.")
    from app.services.configuration_status import log_configuration_status

    log_configuration_status(settings, "dispatcher")

    while not stop_event.is_set():
        try:
            await dispatch_batch(
                limit=settings.outbox_batch_size,
                queue=queue,
            )

            # Wait with bounded timeout to allow stop_event to interrupt quickly
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.outbox_poll_interval)
            except TimeoutError:
                pass

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Outbox dispatcher error: %s", exc)
            try:
                # Fixed backoff with jitter
                await asyncio.wait_for(stop_event.wait(), timeout=5.0 + random.uniform(0, 2.0))  # nosec B311
            except TimeoutError:
                pass

    logger.info("Outbox dispatcher stopped.")
    # Close resources
    from app.db.session import engine
    from app.services.queue_service import get_redis_client

    await engine.dispose()
    redis = get_redis_client()
    await redis.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(dispatch_outbox())
