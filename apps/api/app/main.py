import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.v1 import router as v1_router
from app.config import settings
from app.db.session import engine
from app.services.queue_service import get_redis_client

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown."""
    logger.info("Starting up CodeForge API...")
    yield
    logger.info("Shutting down CodeForge API...")
    # Clean up DB engine
    await engine.dispose()
    # Clean up Redis client
    redis = get_redis_client()
    await redis.aclose()

def create_app() -> FastAPI:
    """Build and return the configured FastAPI application."""
    logging.basicConfig(level=settings.log_level.upper())

    app = FastAPI(
        title=settings.app_name,
        description="AI software engineering agent platform — Phase 0 foundation.",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # Root-level endpoints
    app.include_router(health_router)

    # Versioned API routes
    app.include_router(v1_router)

    return app


# ASGI entry point used by Uvicorn / Gunicorn.
app = create_app()
