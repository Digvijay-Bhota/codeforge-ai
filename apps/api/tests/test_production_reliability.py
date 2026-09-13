import asyncio

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services.queue_service import QueueService


def test_production_config_requires_secrets():
    """Verify that production environments enforce strict configuration."""
    with pytest.raises(ValidationError, match="OPENAI_API_KEY is required in production"):
        Settings(app_env="production", openai_api_key="")

    with pytest.raises(ValidationError, match="DATABASE_URL must be explicitly configured"):
        Settings(
            app_env="production",
            openai_api_key="sk-test",
            database_url="postgresql+asyncpg://codeforge:codeforge@postgres:5432/codeforge"
        )

    # Valid prod settings
    valid = Settings(
        app_env="production",
        openai_api_key="sk-test",
        database_url="postgresql+asyncpg://user:pass@db:5432/prod"
    )
    assert valid.app_env == "production"

@pytest.mark.asyncio
async def test_redis_resilience(monkeypatch):
    """Scenario C: Redis becomes unavailable doesn't crash everything if gracefully handled."""

    class BrokenRedis:
        async def lpush(self, name, *values):
            raise ConnectionError("Redis is down")

        async def brpop(self, keys, timeout=0):
            raise ConnectionError("Redis is down")

        async def ping(self):
            raise ConnectionError("Redis is down")

        async def aclose(self):
            pass

    monkeypatch.setattr("app.services.queue_service.get_redis_client", lambda: BrokenRedis())

    QueueService()

    # Enqueue should raise ConnectionError, but our outbox catches it and leaves the event as unpublished!
    # Let's test the outbox dispatcher behavior when Redis fails.
    from app.outbox_dispatcher import dispatch_outbox


    async def run_dispatcher():
        try:
            await asyncio.wait_for(dispatch_outbox(), timeout=0.1)
        except TimeoutError:
            pass

    # The dispatcher runs in an continuous bounded-backoff retry loop. It should catch the exception and sleep, without crashing.
    # If it doesn't crash, the test passes.
    await run_dispatcher()
