"""FastAPI application factory.

Call create_app() to get a configured FastAPI instance.
This factory pattern keeps the application composable and testable — tests
can call create_app() directly without starting a real server.
"""

import logging

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.v1 import router as v1_router
from app.config import settings


def create_app() -> FastAPI:
    """Build and return the configured FastAPI application."""
    logging.basicConfig(level=settings.log_level.upper())

    app = FastAPI(
        title=settings.app_name,
        description="AI software engineering agent platform — Phase 0 foundation.",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Root-level health endpoint (GET /health)
    app.include_router(health_router)

    # Versioned API routes (GET /api/v1/health, etc.)
    app.include_router(v1_router)

    return app


# ASGI entry point used by Uvicorn / Gunicorn.
app = create_app()
