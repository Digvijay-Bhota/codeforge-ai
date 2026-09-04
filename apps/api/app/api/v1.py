"""v1 API router — aggregates all v1 sub-routers."""

from fastapi import APIRouter

from app.api.health import router as health_router

router = APIRouter(prefix="/api/v1")

router.include_router(health_router)
