"""GitHub webhook endpoint."""

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import WebhookDelivery
from app.db.repositories.webhook_repository import WebhookRepository
from app.db.session import get_db_session
from app.github.authorization import (
    UnauthorizedInstallation,
    UnauthorizedRepository,
)
from app.github.command_parser import InvalidCodeForgeCommand
from app.github.event_mapper import EventMapper
from app.github.webhooks import (
    InvalidWebhookPayload,
    UnsupportedWebhookEvent,
    WebhookError,
    parse_webhook_payload,
    verify_signature,
)
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/github/webhooks", tags=["github_webhooks"])

@router.post("")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(..., description="GitHub event type"),
    x_github_delivery: str = Header(..., description="GitHub delivery ID"),
    x_hub_signature_256: str = Header(..., description="HMAC SHA256 signature"),
    session: AsyncSession = Depends(get_db_session)
) -> JSONResponse:
    """Handle incoming GitHub webhooks."""
    raw_body = await request.body()
    if len(raw_body) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Payload too large")

    secret = settings.github_webhook_secret
    if not secret:
        raise HTTPException(status_code=503, detail="Webhook secret not configured")

    if not verify_signature(raw_body, x_hub_signature_256, secret):
        raise HTTPException(status_code=401, detail="Invalid signature")

    webhook_repo = WebhookRepository(session)

    try:
        # 1. Parse payload (event validation)
        event = parse_webhook_payload(raw_body, x_github_event, x_github_delivery)

        # 2. Authorize installation against DB
        install_id = event.installation.id if event.installation else None
        if not install_id:
            raise UnauthorizedInstallation("No installation context")

        repo_full_name = event.repository.full_name if event.repository else None
        if not repo_full_name:
            raise UnauthorizedRepository("No repository context")

        # Database Check
        install = await webhook_repo.get_installation(install_id)
        if not install or not install.active:
            raise UnauthorizedInstallation(f"Unknown or inactive installation: {install_id}")

        authorized = await webhook_repo.is_repository_authorized(install_id, repo_full_name)
        if not authorized:
            raise UnauthorizedRepository(f"Repository {repo_full_name} not authorized for installation {install_id}")

        # 3. Idempotency Check (delivery deduplication)
        delivery_record = WebhookDelivery(
            delivery_id=x_github_delivery,
            event_type=x_github_event,
            installation_id=install_id,
            repository=repo_full_name,
            status="accepted"
        )
        inserted = await webhook_repo.record_delivery(delivery_record)
        if not inserted:
            return JSONResponse(content={"status": "ignored", "reason": "duplicate delivery"}, status_code=200)

        # 4. Map to task
        task_request = EventMapper.map_event(event)
        if not task_request:
            return JSONResponse(content={"status": "ignored", "reason": "No actionable intent"}, status_code=200)

        # 5. Create Task (Atomically)
        task_service = TaskService(session)
        task = await task_service.create_task(task_request)

        await session.commit()

        return JSONResponse(
            content={
                "status": "accepted",
                "task_id": task.task_id,
            },
            status_code=202
        )

    except UnsupportedWebhookEvent:
        return JSONResponse(content={"status": "ignored", "reason": "unsupported event"}, status_code=200)
    except InvalidWebhookPayload as exc:
        raise HTTPException(status_code=422, detail="Invalid payload") from exc
    except InvalidCodeForgeCommand as exc:
        return JSONResponse(content={"status": "ignored", "reason": f"invalid command: {exc}"}, status_code=200)
    except (UnauthorizedInstallation, UnauthorizedRepository) as exc:
        raise HTTPException(status_code=403, detail="Unauthorized context") from exc
    except WebhookError as exc:
        raise HTTPException(status_code=400, detail="Webhook processing error") from exc
