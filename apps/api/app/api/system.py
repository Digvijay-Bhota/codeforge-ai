"""Server capability status for signed-in users (no secrets)."""

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.config import settings
from app.db.models import User
from app.services.configuration_status import configuration_status

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/status", summary="Which optional capabilities the API is configured for")
async def system_status(_: User = Depends(get_current_user)) -> dict[str, Any]:
    """Booleans and modes only: lets the console explain refusals caused by configuration."""
    return configuration_status(settings)
