"""Health-check route handlers.

Kept deliberately thin — no business logic lives here.
The health check simply confirms the API process is running.
Database / Redis connectivity checks are added for readiness.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.services.queue_service import get_redis_client

logger = logging.getLogger(__name__)

class HealthResponse(BaseModel):
    status: str

router = APIRouter()

@router.get("/health", response_model=HealthResponse)
async def root_health() -> HealthResponse:
    """Root-level liveness check endpoint."""
    return HealthResponse(status="ok")

@router.get("/ready", response_model=HealthResponse)
async def readiness_check(session: AsyncSession = Depends(get_db_session)) -> HealthResponse:
    """Readiness check endpoint that verifies dependencies."""
    try:
        # Check PostgreSQL
        await session.execute(text("SELECT 1"))

        # Check Redis
        redis_client = get_redis_client()
        await redis_client.ping()

        return HealthResponse(status="ready")
    except Exception as exc:
        logger.error("Readiness check failed: %s", exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service unavailable") from exc
