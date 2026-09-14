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
from app.github.exceptions import (
    InactiveUserError,
    InvalidCommandActionError,
    MalformedCommandEventError,
    UnresolvableIdentityError,
)
from app.github.status_consumer import GitHubStatusConsumer
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

    processed_count = 0
    for event in events:
        event_id = event.id
        event_type = event.event_type
        try:
            if event_type == "JOB_CREATED":
                job_id = event.payload.get("job_id")
                if job_id:
                    await queue.enqueue(job_id)
                await repo.mark_published(event_id)
                await session.commit()
                processed_count += 1
            elif event_type == "GITHUB_COMMAND_INGESTED":
                consumer = GitHubCommandConsumer(session, queue=queue)
                await consumer.process_event(event)
                processed_count += 1
            elif event_type in (
                "GITHUB_STATUS_UPDATE",
                "GITHUB_COMMAND_ACCEPTED",
                "JOB_STARTED",
                "JOB_COMPLETED",
                "JOB_FAILED",
            ):
                status_consumer = GitHubStatusConsumer(session, github_client=github_client)
                await status_consumer.process_event(event)
                processed_count += 1
            else:
                logger.warning(
                    "Unrecognized outbox event type %s for event %s",
                    event_type,
                    event_id,
                )
                await repo.mark_published(event_id)
                await session.commit()
                processed_count += 1
        except (
            MalformedCommandEventError,
            UnresolvableIdentityError,
            InactiveUserError,
            InvalidCommandActionError,
        ) as term_exc:
            logger.warning(
                "Terminal failure for outbox event %s (%s): %s",
                event_id,
                event_type,
                term_exc,
            )
            processed_count += 1
        except Exception as exc:
            logger.error(
                "Error processing outbox event %s (%s): %s",
                event_id,
                event_type,
                exc,
            )
            await session.rollback()
            try:
                await repo.record_retry_attempt(
                    event_id=event_id,
                    error_msg=str(exc),
                    max_attempts=settings.outbox_max_retries,
                )
                await session.commit()
            except Exception as retry_exc:
                logger.error(
                    "Failed to record retry attempt for event %s: %s",
                    event_id,
                    retry_exc,
                )
                await session.rollback()

    return processed_count


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
