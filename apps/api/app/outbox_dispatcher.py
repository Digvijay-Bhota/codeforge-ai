import asyncio
import logging
import random

from app.config import settings
from app.db.repositories.outbox_repository import OutboxRepository
from app.db.session import async_session_maker
from app.services.queue_service import QueueService

logger = logging.getLogger("outbox")


import signal  # noqa: E402


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
            async with async_session_maker() as session:
                repo = OutboxRepository(session)
                events = await repo.get_unpublished_events(limit=settings.outbox_batch_size)

                for event in events:
                    if event.event_type == "JOB_CREATED":
                        job_id = event.payload.get("job_id")
                        if job_id:
                            # We can implement bounded retry logic for enqueue
                            # Wait, enqueue is a robust Redis push.
                            await queue.enqueue(job_id)
                    await repo.mark_published(event.id)

                if events:
                    await session.commit()

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
                await asyncio.wait_for(stop_event.wait(), timeout=5.0 + random.uniform(0, 2.0))
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
