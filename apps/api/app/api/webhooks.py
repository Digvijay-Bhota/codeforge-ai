"""GitHub webhook endpoint."""

import logging

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import settings

# Lazy imports for execution
from app.github.authorization import (
    GitHubInstallationAuthorizer,
    UnauthorizedInstallation,
    UnauthorizedRepository,
)
from app.github.command_parser import InvalidCodeForgeCommand
from app.github.event_mapper import EventMapper
from app.github.idempotency import DuplicateWebhookDelivery, WebhookDeliveryStore
from app.github.webhooks import (
    InvalidWebhookPayload,
    UnsupportedWebhookEvent,
    WebhookError,
    parse_webhook_payload,
    verify_signature,
)
from app.schemas.task import TaskStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/github/webhooks", tags=["github_webhooks"])

# In-memory delivery store for Phase 6C
_delivery_store = WebhookDeliveryStore()
_authorizer = GitHubInstallationAuthorizer()

@router.post("")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(..., description="GitHub event type"),
    x_github_delivery: str = Header(..., description="GitHub delivery ID"),
    x_hub_signature_256: str = Header(..., description="HMAC SHA256 signature")
) -> JSONResponse:
    """Handle incoming GitHub webhooks."""

    # 1. Read raw body
    raw_body = await request.body()

    # Bound the request size (e.g. 5MB)
    if len(raw_body) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Payload too large")

    secret = settings.github_webhook_secret
    if not secret:
        # If webhooks aren't configured, we shouldn't accept them.
        logger.error("Received webhook but GITHUB_WEBHOOK_SECRET is unconfigured.")
        raise HTTPException(status_code=503, detail="Webhook secret not configured")

    # 2. Verify signature
    if not verify_signature(raw_body, x_hub_signature_256, secret):
        logger.warning("Invalid GitHub webhook signature from %s", request.client.host if request.client else "unknown")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        # 3. Parse payload (event validation)
        event = parse_webhook_payload(raw_body, x_github_event, x_github_delivery)

        # 4. Authorize installation
        if not _authorizer.authorize(event):
            raise UnauthorizedInstallation("Installation not authorized")

        # 5. Idempotency Check (delivery deduplication)
        _delivery_store.mark_seen(x_github_delivery)

        # 6. Map to task
        task_request = EventMapper.map_event(event)

        if not task_request:
            return JSONResponse(content={"status": "ignored", "reason": "No actionable intent"}, status_code=200)

        # 7. Execute CodeForge
        from app.services.task_service import TaskService
        task_service = TaskService()

        # In a real synchronous system, this might timeout the webhook.
        # But per requirements: "Keep execution synchronous for now."
        result = await task_service.run_task(task_request)

        # Sanitize result to avoid leaking secrets
        if result.status == TaskStatus.error:
            # Mask internal errors from GitHub UI
            return JSONResponse(
                content={"status": "error", "task_id": result.task_id, "reason": "Execution failed"},
                status_code=200
            )

        return JSONResponse(
            content={
                "status": "success",
                "task_id": result.task_id,
                "github": result.github.model_dump() if result.github else None
            },
            status_code=200
        )

    except DuplicateWebhookDelivery:
        logger.info("Ignoring duplicate webhook delivery %s", x_github_delivery)
        return JSONResponse(content={"status": "ignored", "reason": "duplicate delivery"}, status_code=200)
    except UnsupportedWebhookEvent:
        return JSONResponse(content={"status": "ignored", "reason": "unsupported event"}, status_code=200)
    except InvalidWebhookPayload as exc:
        logger.error("Invalid webhook payload: %s", exc)
        raise HTTPException(status_code=422, detail="Invalid payload") from exc
    except InvalidCodeForgeCommand as exc:
        # A bad command should return 200 OK so GitHub doesn't retry it, but inform the user ideally.
        return JSONResponse(content={"status": "ignored", "reason": f"invalid command: {exc}"}, status_code=200)
    except (UnauthorizedInstallation, UnauthorizedRepository) as exc:
        raise HTTPException(status_code=403, detail="Unauthorized context") from exc
    except WebhookError as exc:
        logger.error("Webhook processing error: %s", exc)
        raise HTTPException(status_code=400, detail="Webhook processing error") from exc
    except Exception as exc:
        logger.exception("Unexpected error processing webhook")
        # Do not leak internal exception traces
        raise HTTPException(status_code=500, detail="Internal server error") from exc
