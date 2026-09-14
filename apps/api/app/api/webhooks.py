"""GitHub webhook endpoint."""

import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.config import settings
from app.db.models import OutboxEvent, WebhookDelivery
from app.db.repositories.outbox_repository import OutboxRepository
from app.db.repositories.webhook_repository import WebhookRepository
from app.db.session import get_db_session
from app.github.command_parser import CommandParser, InvalidCodeForgeCommand
from app.github.webhook_models import CodeForgeCommandEvent
from app.github.webhooks import (
    InvalidWebhookPayload,
    UnsupportedWebhookEvent,
    parse_issue_comment_payload,
    verify_signature,
)
from app.services.authorization_service import AuthorizationService

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/github/webhooks", tags=["github_webhooks"])


@router.post("")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(..., description="GitHub event type"),
    x_github_delivery: str | None = Header(None, description="GitHub delivery ID"),
    x_hub_signature_256: str = Header(..., description="HMAC SHA256 signature"),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Handle incoming GitHub webhooks safely and durably."""
    raw_body = await request.body()
    if len(raw_body) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Payload too large")

    secret = settings.github_webhook_secret
    if not secret:
        raise HTTPException(status_code=503, detail="Webhook secret not configured")

    if not verify_signature(raw_body, x_hub_signature_256, secret):
        raise HTTPException(status_code=401, detail="Invalid signature")

    if not x_github_delivery or not x_github_delivery.strip():
        raise HTTPException(status_code=400, detail="Missing delivery identifier")

    webhook_repo = WebhookRepository(session)

    # 1. Quick acknowledgement for ping event
    if x_github_event == "ping":
        logger.info("GitHub ping received (delivery_id=%s)", x_github_delivery)
        return JSONResponse(
            content={"status": "ignored", "reason": "ping received"},
            status_code=200,
        )

    # 2. Non-issue_comment events: deduplicate if metadata present, acknowledge safely
    if x_github_event != "issue_comment":
        logger.info(
            "Unsupported event received: %s (delivery_id=%s)",
            x_github_event,
            x_github_delivery,
        )
        try:
            payload_data = json.loads(raw_body.decode("utf-8"))
            install_id = payload_data.get("installation", {}).get("id")
            repo_name = payload_data.get("repository", {}).get("full_name")
            if install_id and repo_name:
                delivery_record = WebhookDelivery(
                    delivery_id=x_github_delivery,
                    event_type=x_github_event,
                    installation_id=int(install_id),
                    repository=str(repo_name),
                    status="ignored",
                    processed_at=func.now(),
                )
                inserted = await webhook_repo.record_delivery(delivery_record)
                if not inserted:
                    return JSONResponse(
                        content={"status": "ignored", "reason": "duplicate delivery"},
                        status_code=200,
                    )
                await session.commit()
        except Exception as exc:
            logger.debug(
                "Could not record deduplication for unsupported event %s: %s",
                x_github_event,
                exc,
            )
        return JSONResponse(

            content={"status": "ignored", "reason": "unsupported event"},
            status_code=200,
        )

    # 3. Parse and normalize issue_comment payload
    try:
        normalized = parse_issue_comment_payload(raw_body, x_github_delivery)
    except UnsupportedWebhookEvent as exc:
        return JSONResponse(
            content={"status": "ignored", "reason": str(exc)},
            status_code=200,
        )
    except InvalidWebhookPayload as exc:
        raise HTTPException(status_code=422, detail="Invalid payload") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Webhook processing error") from exc

    # 4. Command parsing from comment body
    try:
        cmd = CommandParser.parse(normalized.comment_body)
    except InvalidCodeForgeCommand as exc:
        logger.info(
            "Invalid @codeforge command: delivery_id=%s error=%s",
            x_github_delivery,
            exc,
        )
        return JSONResponse(
            content={"status": "ignored", "reason": f"invalid command: {exc}"},
            status_code=200,
        )

    if not cmd:
        return JSONResponse(
            content={"status": "ignored", "reason": "No @codeforge command found"},
            status_code=200,
        )

    # 5. Authorize Installation against DB
    install = await webhook_repo.get_installation(normalized.installation_id)
    if not install or not install.active:
        logger.warning(
            "Unauthorized or inactive installation %s for delivery %s",
            normalized.installation_id,
            x_github_delivery,
        )
        return JSONResponse(
            content={"status": "ignored", "reason": "unauthorized installation"},
            status_code=200,
        )

    # 6. Authorize Repository against DB
    is_repo_auth = await webhook_repo.is_repository_authorized(
        normalized.installation_id, normalized.repository_full_name
    )
    if not is_repo_auth:
        logger.warning(
            "Repository %s not authorized for installation %s (delivery %s)",
            normalized.repository_full_name,
            normalized.installation_id,
            x_github_delivery,
        )
        return JSONResponse(
            content={"status": "ignored", "reason": "unauthorized repository"},
            status_code=200,
        )

    # 7. Authorize Actor (Collaborator permission check)
    auth_service = AuthorizationService(session)
    actor_authorized = await auth_service.can_actor_write_repository(
        installation_id=normalized.installation_id,
        owner=normalized.repository_owner,
        repo=normalized.repository_name,
        github_login=normalized.actor_login,
        github_user_id=normalized.actor_github_id,
    )
    if not actor_authorized:
        logger.warning(
            "Actor %s unauthorized for repository %s (delivery %s)",
            normalized.actor_login,
            normalized.repository_full_name,
            x_github_delivery,
        )
        return JSONResponse(
            content={"status": "ignored", "reason": "unauthorized actor"},
            status_code=200,
        )

    # 8. Delivery Deduplication (atomic in DB transaction)
    delivery_record = WebhookDelivery(
        delivery_id=x_github_delivery,
        event_type=x_github_event,
        installation_id=normalized.installation_id,
        repository=normalized.repository_full_name,
        status="accepted",
        processed_at=func.now(),
    )
    inserted = await webhook_repo.record_delivery(delivery_record)
    if not inserted:
        logger.info("Duplicate delivery ignored: delivery_id=%s", x_github_delivery)
        return JSONResponse(
            content={"status": "ignored", "reason": "duplicate delivery"},
            status_code=200,
        )

    # 9. Create OutboxEvent atomically
    command_event = CodeForgeCommandEvent(
        delivery_id=x_github_delivery,
        repository=normalized.repository_full_name,
        repository_id=normalized.repository_id,
        repository_owner=normalized.repository_owner,
        repository_name=normalized.repository_name,
        installation_id=normalized.installation_id,
        actor_github_id=normalized.actor_github_id,
        actor_login=normalized.actor_login,
        issue_number=normalized.issue_number,
        issue_title=normalized.issue_title,
        issue_html_url=normalized.issue_html_url,
        is_pull_request=normalized.is_pull_request,
        head_sha=normalized.head_sha,
        comment_id=normalized.comment_id,
        command=cmd,
        received_at=datetime.now(UTC),
        ingestion_status="accepted",
    )

    outbox_repo = OutboxRepository(session)
    outbox_event = OutboxEvent(
        event_type="GITHUB_COMMAND_INGESTED",
        aggregate_id=x_github_delivery,
        payload=command_event.model_dump(mode="json"),
    )
    await outbox_repo.create_event(outbox_event)

    # 10. Commit transaction
    await session.commit()

    logger.info(
        "Successfully ingested @codeforge command: delivery_id=%s repo=%s command=%s actor=%s",
        x_github_delivery,
        normalized.repository_full_name,
        cmd.name.value,
        normalized.actor_login,
    )

    return JSONResponse(
        content={
            "status": "accepted",
            "delivery_id": x_github_delivery,
            "command": cmd.name.value,
        },
        status_code=202,
    )
