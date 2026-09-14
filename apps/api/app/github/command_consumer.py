"""Phase 10B.4.1: Durable consumer/dispatcher for GITHUB_COMMAND_INGESTED events.

Connects the durable GITHUB_COMMAND_INGESTED outbox events created by the GitHub
webhook ingestion layer to the existing CodeForge task/job lifecycle.

Flow:
1. Claim pending GITHUB_COMMAND_INGESTED outbox events using FOR UPDATE SKIP LOCKED.
2. Validate event payload against the durable CodeForgeCommandEvent contract.
3. Resolve the GitHub actor to a known CodeForge User (from Phase 10B.1).
4. Perform database-level idempotency checks (TaskGitHubLink uniqueness on repo+comment).
5. Derive TaskRequest (execution_target=github, github_repository=..., description=...).
6. Create CodeForge Task, Job, and JOB_CREATED outbox event using TaskService.
7. Attach TaskGitHubLink mapping the originating GitHub context.
8. Record audit events.
9. Mark the GITHUB_COMMAND_INGESTED event as published/processed atomically upon commit.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import OutboxEvent, Task, TaskGitHubLink
from app.db.repositories.job_repository import JobRepository
from app.db.repositories.outbox_repository import OutboxRepository
from app.db.repositories.task_repository import TaskRepository
from app.db.repositories.user_repository import UserRepository
from app.github.exceptions import (
    CommandConsumerError,
    InactiveUserError,
    InvalidCommandActionError,
    MalformedCommandEventError,
    UnresolvableIdentityError,
)
from app.github.webhook_models import CodeForgeCommandEvent
from app.observability.events import AuditEventType
from app.observability.tracing import record_audit_event
from app.schemas.task import ExecutionTarget, TaskRequest
from app.services.queue_service import QueueService
from app.services.task_service import TaskService

logger = logging.getLogger("github.command_consumer")


def is_retryable_error(exc: Exception) -> bool:
    """Classify whether an exception encountered during consumption is retryable."""
    if isinstance(
        exc,
        MalformedCommandEventError
        | UnresolvableIdentityError
        | InactiveUserError
        | InvalidCommandActionError
        | ValidationError,
    ):
        return False
    if isinstance(exc, CommandConsumerError):
        return exc.retryable
    if isinstance(exc, IntegrityError | OperationalError | TimeoutError | ConnectionError | OSError):
        return True
    # Default unexpected errors to retryable bounded by max retries
    return True


class GitHubCommandConsumer:
    """Durable consumer for GITHUB_COMMAND_INGESTED outbox events."""

    def __init__(
        self,
        session: AsyncSession,
        queue: QueueService | None = None,
    ) -> None:
        self.session = session
        self.task_repo = TaskRepository(session)
        self.job_repo = JobRepository(session)
        self.outbox_repo = OutboxRepository(session)
        self.user_repo = UserRepository(session)
        self.queue = queue or QueueService()

    @staticmethod
    def build_task_description(command_event: CodeForgeCommandEvent) -> str:
        """Derive natural language task description from the command event."""
        target_type = "pull request" if command_event.is_pull_request else "issue"
        cmd_action = command_event.command.name.value
        desc = f"Execute @codeforge {cmd_action} for {target_type} #{command_event.issue_number}."
        if command_event.issue_title:
            desc += f"\nTitle: {command_event.issue_title}"
        if command_event.command.arguments:
            desc += f"\nArguments:\n{command_event.command.arguments}"
        return desc

    async def process_event(self, event: OutboxEvent) -> Task | None:
        """Process a single GITHUB_COMMAND_INGESTED outbox event safely and idempotently.

        Returns the created or converged Task, or None if skipped/already processed.
        Raises classified CommandConsumerError on unhandled failure.
        """
        event_id = event.id
        if event.published_at is not None:
            logger.info("Event %s is already marked published, skipping.", event_id)
            return None

        if event.event_type != "GITHUB_COMMAND_INGESTED":
            logger.warning(
                "Event %s is of type %s, expected GITHUB_COMMAND_INGESTED.",
                event_id,
                event.event_type,
            )
            return None

        # 1. Validate Event Payload
        try:
            command_event = CodeForgeCommandEvent.model_validate(event.payload)
        except Exception as exc:
            err_msg = f"Malformed GITHUB_COMMAND_INGESTED payload: {exc}"
            logger.error("Outbox event %s validation error: %s", event_id, err_msg)
            await self._handle_terminal_failure(
                event=event,
                error_msg=err_msg,
                error_type="MalformedCommandEventError",
            )
            raise MalformedCommandEventError(err_msg) from exc

        # 2. Idempotency Check (Check-then-act optimization)
        existing_link = await self.user_repo.get_task_github_link_by_comment(
            repository_id=command_event.repository_id,
            comment_id=command_event.comment_id,
        )
        if existing_link:
            logger.info(
                "Idempotency hit: repo=%s comment=%s already linked to task=%s. Marking outbox event %s published.",
                command_event.repository_id,
                command_event.comment_id,
                existing_link.task_id,
                event.id,
            )
            existing_task = await self.task_repo.get_task(existing_link.task_id)
            await self.outbox_repo.mark_published(event.id)
            await self.session.commit()
            return existing_task

        # 3. Resolve GitHub Actor to CodeForge User
        user = await self.user_repo.get_user_by_github_id(command_event.actor_github_id)
        if not user:
            user = await self.user_repo.get_user_by_github_login(command_event.actor_login)

        if not user:
            err_msg = (
                f"GitHub actor '{command_event.actor_login}' (id={command_event.actor_github_id}) "
                f"cannot be resolved to a CodeForge user"
            )
            logger.warning("Unresolvable actor identity for event %s: %s", event.id, err_msg)
            await self._handle_terminal_failure(
                event=event,
                error_msg=err_msg,
                error_type="UnresolvableIdentityError",
                command_event=command_event,
            )
            raise UnresolvableIdentityError(err_msg)

        if not user.is_active:
            err_msg = f"CodeForge user '{user.id}' for actor '{command_event.actor_login}' is inactive"
            logger.warning("Inactive user rejected for event %s: %s", event.id, err_msg)
            await self._handle_terminal_failure(
                event=event,
                error_msg=err_msg,
                error_type="InactiveUserError",
                command_event=command_event,
            )
            raise InactiveUserError(err_msg)

        # 4. Map Command to TaskRequest
        description = self.build_task_description(command_event)
        task_req = TaskRequest(
            execution_target=ExecutionTarget.github,
            github_repository=command_event.repository,
            description=description,
        )

        # 5. Create Task, Job, and JOB_CREATED outbox event atomically
        task_service = TaskService(self.session)
        task = await task_service.create_task(task_req, creator_id=user.id)

        if command_event.is_pull_request:
            task.pr_metadata = {
                "pr_number": command_event.issue_number,
                "issue_html_url": command_event.issue_html_url,
            }
            await self.task_repo.update_task(task)

        # 6. Create TaskGitHubLink & guard concurrent uniqueness boundary
        link = TaskGitHubLink(
            task_id=task.task_id,
            installation_id=command_event.installation_id,
            repository_id=command_event.repository_id,
            repository_full_name=command_event.repository,
            issue_number=command_event.issue_number,
            pull_request_number=command_event.issue_number if command_event.is_pull_request else None,
            trigger_comment_id=command_event.comment_id,
            triggering_github_user_id=command_event.actor_github_id,
            triggering_github_login=command_event.actor_login,
            head_sha=command_event.head_sha,
        )

        try:
            await self.user_repo.create_task_github_link(link)
            # Mark the GITHUB_COMMAND_INGESTED outbox event as published
            await self.outbox_repo.mark_published(event.id)

            # Emit GITHUB_STATUS_UPDATE for Phase 10B.4.2 GitHub Check Run & Comment Lifecycle
            job = await self.job_repo.get_job_by_task(task.task_id)
            status_event = OutboxEvent(
                event_type="GITHUB_STATUS_UPDATE",
                aggregate_id=task.task_id,
                payload={
                    "task_id": task.task_id,
                    "job_id": job.id if job else None,
                    "job_status": "PENDING",
                    "event": "COMMAND_ACCEPTED",
                    "command": command_event.command.name.value,
                    "arguments": command_event.command.arguments,
                },
            )
            await self.outbox_repo.create_event(status_event)

            # 7. Record Audit Event
            await record_audit_event(
                self.session,
                event_type=AuditEventType.GITHUB_COMMAND_DISPATCHED,
                actor_type="GITHUB_USER",
                actor_id=user.id,
                resource_type="task",
                resource_id=task.task_id,
                metadata={
                    "delivery_id": command_event.delivery_id,
                    "command": command_event.command.name.value,
                    "repository_id": command_event.repository_id,
                    "comment_id": command_event.comment_id,
                    "issue_number": command_event.issue_number,
                    "is_pull_request": command_event.is_pull_request,
                },
            )

            # Durably commit the transaction
            await self.session.commit()
            logger.info(
                "Successfully dispatched command %s: task=%s user=%s repo=%s comment=%s",
                command_event.command.name.value,
                task.task_id,
                user.id,
                command_event.repository,
                command_event.comment_id,
            )
            return task

        except IntegrityError as exc:
            # Concurrent duplicate execution: another worker committed TaskGitHubLink for this comment
            logger.info(
                "Concurrent conflict detected on uq_task_github_links_repo_comment: repo=%s comment=%s. "
                "Rolling back and converging on existing task.",
                command_event.repository_id,
                command_event.comment_id,
            )
            await self.session.rollback()

            # In clean state, find the winning task and mark this outbox event published
            existing_link = await self.user_repo.get_task_github_link_by_comment(
                repository_id=command_event.repository_id,
                comment_id=command_event.comment_id,
            )
            if existing_link:
                existing_task = await self.task_repo.get_task(existing_link.task_id)
                await self.outbox_repo.mark_published(event_id)
                await self.session.commit()
                return existing_task
            raise exc

    async def _handle_terminal_failure(
        self,
        event: OutboxEvent,
        error_msg: str,
        error_type: str,
        command_event: CodeForgeCommandEvent | None = None,
    ) -> None:
        """Safely records terminal failure so event does not loop forever."""
        event_id = event.id
        actor_id = str(command_event.actor_github_id) if command_event else None
        delivery_id = command_event.delivery_id if command_event else getattr(event, "aggregate_id", "")
        await self.session.rollback()
        await self.outbox_repo.mark_failed_terminal(
            event_id=event_id,
            error_msg=error_msg,
            error_type=error_type,
        )
        await record_audit_event(
            self.session,
            event_type=AuditEventType.GITHUB_COMMAND_FAILED,
            actor_type="GITHUB_USER",
            actor_id=actor_id,
            resource_type="outbox_event",
            resource_id=str(event_id),
            result="FAILED",
            metadata={
                "delivery_id": delivery_id,
                "error": error_msg,
                "error_type": error_type,
            },
        )
        await self.session.commit()

    async def claim_and_process_batch(self, limit: int = 50) -> list[Task]:
        """Claim and process a bounded batch of GITHUB_COMMAND_INGESTED outbox events.

        Uses SELECT ... FOR UPDATE SKIP LOCKED to prevent concurrency contention.
        """
        events = await self.outbox_repo.get_unpublished_events_by_type(
            event_type="GITHUB_COMMAND_INGESTED",
            limit=limit,
        )
        tasks: list[Task] = []
        for event in events:
            event_id = event.id
            try:
                task = await self.process_event(event)
                if task:
                    tasks.append(task)
            except (
                MalformedCommandEventError,
                UnresolvableIdentityError,
                InactiveUserError,
                InvalidCommandActionError,
            ):
                # Terminal failures are already marked published and recorded
                continue
            except Exception as exc:
                logger.exception("Transient failure processing outbox event %s: %s", event_id, exc)
                await self.session.rollback()
                await self.outbox_repo.record_retry_attempt(
                    event_id=event_id,
                    error_msg=str(exc),
                    max_attempts=settings.outbox_max_retries,
                )
                await self.session.commit()

        return tasks
