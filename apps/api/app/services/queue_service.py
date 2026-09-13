import logging

from redis.asyncio import Redis

from app.config import settings

logger = logging.getLogger(__name__)

# Global redis connection pool per process
_redis_client: Redis | None = None

def get_redis_client() -> Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=5.0,
            socket_connect_timeout=5.0,
            retry_on_timeout=True,
            health_check_interval=30
        )
    return _redis_client

class QueueService:
    def __init__(self):
        self.redis = get_redis_client()
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
