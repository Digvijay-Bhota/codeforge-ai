import logging

from redis.asyncio import Redis

from app.config import settings

logger = logging.getLogger(__name__)

class QueueService:
    def __init__(self):
        self.redis = Redis.from_url(settings.redis_url, decode_responses=True)
        self.queue_name = settings.task_queue_name

    async def enqueue(self, job_id: int) -> None:
        """Enqueue a job ID."""
        logger.info("Enqueueing job_id=%s to %s", job_id, self.queue_name)
        await self.redis.lpush(self.queue_name, str(job_id))

    async def dequeue(self, timeout: int = 0) -> str | None:
        """Dequeue a job ID, waiting up to timeout seconds."""
        result = await self.redis.brpop(self.queue_name, timeout=timeout)
        if result:
            return str(result[1])
        return None
