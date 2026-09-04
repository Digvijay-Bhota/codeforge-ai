"""Health-check route handlers.

Kept deliberately thin — no business logic lives here.
The health check simply confirms the API process is running.
Database / Redis connectivity checks will be added in Phase 1.
"""

from fastapi import APIRouter
from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str


router = APIRouter()


def _health() -> HealthResponse:
    """Return a minimal liveness response."""
    return HealthResponse(status="ok")


@router.get("/health", response_model=HealthResponse)
async def root_health() -> HealthResponse:
    """Root-level health check endpoint."""
    return _health()
