"""v1 API router — aggregates all v1 sub-routers."""

from fastapi import APIRouter

from app.api.health import router as health_router
from app.api.tasks import router as tasks_router
from app.api.webhooks import router as webhooks_router

router = APIRouter(prefix="/api/v1")

router.include_router(health_router)
router.include_router(tasks_router)
router.include_router(webhooks_router)
