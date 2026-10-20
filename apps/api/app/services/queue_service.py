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

    async def is_queued(self, job_id: int) -> bool:
        """Whether a message for *job_id* is waiting in the queue.

        Used by recovery to avoid duplicate messages.  If Redis cannot answer,
        report False: recovery then enqueues, and a duplicate message is
        harmless (only one worker can claim a job), whereas a lost one is not.
        """
        try:
            return await self.redis.lpos(self.queue_name, str(job_id)) is not None
        except Exception as exc:
            logger.warning("Could not check queue for job_id=%s: %s", job_id, exc)
            return False

    async def dequeue(self, timeout: int = 0) -> str | None:
        """Dequeue a job ID, waiting up to timeout seconds."""
        result = await self.redis.brpop(self.queue_name, timeout=timeout)
        if result:
            return str(result[1])
        return None
