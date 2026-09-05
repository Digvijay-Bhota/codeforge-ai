import asyncio
import logging

from app.config import settings
from app.db.repositories.outbox_repository import OutboxRepository
from app.db.session import async_session_maker
from app.services.queue_service import QueueService

logger = logging.getLogger("outbox")

async def dispatch_outbox():
    queue = QueueService()
    while True:
        try:
            async with async_session_maker() as session:
                repo = OutboxRepository(session)
                events = await repo.get_unpublished_events(limit=settings.outbox_batch_size)

                for event in events:
                    if event.event_type == "JOB_CREATED":
                        job_id = event.payload.get("job_id")
                        if job_id:
                            await queue.enqueue(job_id)
                    await repo.mark_published(event.id)

                if events:
                    await session.commit()

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Outbox dispatcher error: %s", exc)

        await asyncio.sleep(settings.outbox_poll_interval)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(dispatch_outbox())
