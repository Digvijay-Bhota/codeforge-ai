"""Phase 10B.4.2: GitHub Status Consumer for Check Run & Comment Lifecycle.

Connects the CodeForge Task/Job lifecycle to GitHub-facing visibility:
1. Managed acknowledgement comment on the originating GitHub issue or pull request.
2. GitHub Check Run creation (status: queued) for pull request commands targeting authoritative head SHA.
3. GitHub Check Run state updates tied to Job lifecycle transitions (in_progress, completed/success, completed/failure).
4. Strictly monotonic state transitions (queued -> in_progress -> completed) preventing stale event regression.
5. Idempotent check-then-act operations backed by durable TaskGitHubLink persistence.
6. Error classification separating transient GitHub network issues from terminal configuration errors.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Job, JobStatusEnum, OutboxEvent, Task, TaskGitHubLink
from app.db.repositories.job_repository import JobRepository
from app.db.repositories.outbox_repository import OutboxRepository
from app.db.repositories.task_repository import TaskRepository
from app.db.repositories.user_repository import UserRepository
from app.github.client import GitHubClient
from app.github.command_parser import CodeForgeCommandType
from app.github.models import (
    CheckRunOutput,
    CreateCheckRunRequest,
    GitHubCheckRun,
    UpdateCheckRunRequest,
)
from app.github.repository_access import (
    STATUS_PERMISSIONS,
    RepositoryAccessError,
    resolve_repository_access,
)
from app.github.token_service import TokenScope
from app.observability.events import AuditEventType
from app.observability.tracing import record_audit_event
from app.outbox_policy import FailureCategory, classify

logger = logging.getLogger("github.status_consumer")

CHECK_RUN_NAME = "CodeForge AI"

# Numeric ranking for state monotonicity enforcement
STATUS_RANK: dict[str, int] = {
    "queued": 1,
    "in_progress": 2,
    "completed": 3,
}


def is_retryable_error(exc: Exception) -> bool:
    """Whether a status sync failure is retried with backoff (app/outbox_policy.py)."""
    return classify(exc).category in (FailureCategory.TRANSIENT, FailureCategory.RATE_LIMITED)



# Allows for clock skew between CodeForge and GitHub.
_COMMENT_SEARCH_MARGIN = timedelta(minutes=10)


def _comments_since(task: Task) -> str | None:
    """Where to start looking for this task's managed comment (ISO 8601), or None: from the start."""
    created = task.created_at
    if created is None:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (created - _COMMENT_SEARCH_MARGIN).isoformat()


def _command_display(command_name: str | None) -> str:
    """The command as typed: ``@codeforge <action>``, for a known action only."""
    try:
        return f"@codeforge {CodeForgeCommandType(command_name).value}"
    except ValueError:
        return "@codeforge"


# Characters that are invisible or reorder text; never echoed into a comment.
_UNSAFE_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def _plain_text(text: str) -> str:
    """Normalise line endings, drop control and bidirectional-override characters, trim."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _UNSAFE_CHARS.sub("", text).strip()


def _fenced(text: str) -> str:
    """A fenced code block that *text* cannot close: the fence is longer than any backtick run in it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"

class GitHubStatusConsumer:
    """Consumes lifecycle events and updates GitHub Check Runs and issue/PR comments."""

    def __init__(
        self,
        session: AsyncSession,
        github_client: GitHubClient | None = None,
        client_factory: Callable[[int, TokenScope], GitHubClient] | None = None,
    ) -> None:
        self.session = session
        self.outbox_repo = OutboxRepository(session)
        self.task_repo = TaskRepository(session)
        self.job_repo = JobRepository(session)
        self.user_repo = UserRepository(session)
        self._github_client = github_client
        self._client_factory = client_factory

    async def _get_client(self, link: TaskGitHubLink) -> GitHubClient | None:
        """A client for the link's repository, limited to status permissions (CF-06).

        The token covers only the repository the signed webhook named
        (link.repository_id), with checks, issues and pull-request read
        access.  None, and so no GitHub update, unless the installation is
        still active and authorized for that repository, and the repository
        name still denotes that repository.  A transient failure to check
        that is raised, so the event is retried.
        """
        if self._github_client is not None:
            return self._github_client
        try:
            access = await resolve_repository_access(
                self.session,
                link.repository_full_name,
                expected_installation_id=link.installation_id,
                expected_repository_id=link.repository_id,
            )
        except RepositoryAccessError as exc:
            if exc.retryable:
                raise
            logger.warning(
                "Task %s: no status credential for %s (installation %s): %s; skipping GitHub status sync.",
                link.task_id,
                link.repository_full_name,
                link.installation_id,
                exc,
            )
            return None
        scope = access.scope(STATUS_PERMISSIONS)
        if self._client_factory is not None:
            return self._client_factory(access.installation_id, scope)
        return GitHubClient.for_installation(access.installation_id, scope=scope)

    @staticmethod
    def is_monotonic_transition(current_status: str | None, target_status: str) -> bool:
        """Enforce strict forward monotonicity of Check Run statuses.

        Returns True if the transition is allowed (forward or same state), False if stale.
        """
        if current_status is None:
            return True
        if current_status == "completed":
            # Terminal states can never be transitioned out of
            return target_status == "completed"
        current_rank = STATUS_RANK.get(current_status, 0)
        target_rank = STATUS_RANK.get(target_status, 0)
        return target_rank >= current_rank

    @staticmethod
    def _map_job_status_to_check_run(
        job_status: str,
    ) -> tuple[str, str | None]:
        """Map CodeForge JobStatusEnum to GitHub Check Run (status, conclusion)."""
        if job_status == JobStatusEnum.PENDING.value or job_status == "PENDING":
            return "queued", None
        if job_status == JobStatusEnum.RUNNING.value or job_status == "RUNNING":
            return "in_progress", None
        if job_status == JobStatusEnum.SUCCEEDED.value or job_status == "SUCCEEDED":
            return "completed", "success"
        if job_status == JobStatusEnum.FAILED.value or job_status == "FAILED":
            return "completed", "failure"
        # Fallback default
        return "in_progress", None

    @staticmethod
    def _build_acknowledgement_body(
        task: Task,
        job: Job | None,
        link: TaskGitHubLink,
        command_name: str | None = None,
        arguments: str | None = None,
    ) -> str:
        """Build the Markdown body of the managed acknowledgement comment.

        The comment is posted once, when the command is accepted, and not
        updated afterwards.  The commenter's instructions are untrusted text:
        they are shown verbatim in a fenced code block, so Markdown, HTML and
        @mentions in them are not rendered (and nobody is notified).  The task's
        internal prompt (``task.requested_task``) is never shown.
        """
        target_display = (
            f"Pull Request #{link.pull_request_number}"
            if link.pull_request_number
            else f"Issue #{link.issue_number}"
        )
        lines = [
            "### 🚀 CodeForge AI Command Accepted",
            "",
            "| Context | Details |",
            "|:---|:---|",
            f"| **Command** | `{_command_display(command_name)}` |",
            f"| **Repository** | `{link.repository_full_name}` |",
            f"| **Target** | {target_display} |",
            f"| **Task ID** | `{task.task_id}` |",
            "",
        ]
        instructions = _plain_text(arguments or "")
        if instructions:
            lines += ["**Instructions**", "", _fenced(instructions), ""]
        lines.append(
            "Queued. CodeForge prepares a plan first; no code is written until the plan is "
            "approved in the CodeForge console. This comment is not updated: follow the task there."
        )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _build_check_run_output(
        status: str,
        conclusion: str | None,
        task: Task,
        job: Job | None,
        error: str | None = None,
    ) -> CheckRunOutput:
        """Construct a structured CheckRunOutput payload without exposing secrets."""
        job_id_str = str(job.id) if job else "N/A"
        task_id_str = task.task_id

        if status == "queued":
            return CheckRunOutput(
                title="CodeForge AI - Queued",
                summary=f"Task `{task_id_str}` has been accepted and is queued for processing.",
                text=f"Repository: {task.repository}\nTask ID: {task_id_str}\nJob ID: {job_id_str}",
            )
        if status == "in_progress":
            return CheckRunOutput(
                title="CodeForge AI - In Progress",
                summary=f"Task `{task_id_str}` (Job `{job_id_str}`) is currently executing.",
                text=f"Repository: {task.repository}\nTask ID: {task_id_str}\nJob ID: {job_id_str}",
            )
        if status == "completed":
            if conclusion == "success":
                return CheckRunOutput(
                    title="CodeForge AI - Completed",
                    summary=f"Task `{task_id_str}` completed successfully.",
                    text=task.final_message or "Task execution finished successfully.",
                )
            error_detail = error or task.failure_reason or "Execution encountered an error."
            return CheckRunOutput(
                title="CodeForge AI - Failed",
                summary=f"Task `{task_id_str}` failed: {error_detail}",
                text=f"Failure Reason: {error_detail}",
            )

        return CheckRunOutput(
            title="CodeForge AI",
            summary=f"Status: {status}",
            text=f"Task ID: {task_id_str}",
        )

    async def sync_status(
        self,
        task_id: str,
        job_id: int | None = None,
        job_status: str = "PENDING",
        event_name: str | None = None,
        command_name: str | None = None,
        arguments: str | None = None,
        error: str | None = None,
    ) -> TaskGitHubLink | None:
        """Idempotently sync GitHub comment acknowledgement and Check Run status.

        Ensures:
        - Safe under concurrent executions via PostgreSQL row locking on TaskGitHubLink.
        - Exactly one Check Run is created per CodeForge task/job.
        - Exactly one managed acknowledgement comment per task.
        - Check Run is only created for pull requests targeting authoritative head SHA.
        - Stale/out-of-order state transitions are safely ignored.
        """
        link = await self.user_repo.get_task_github_link(task_id, for_update=True)
        if not link:
            logger.info("Task %s has no GitHub link, skipping GitHub status sync.", task_id)
            return None

        task = await self.task_repo.get_task(task_id)
        if not task:
            logger.warning("Task %s not found in DB, skipping GitHub status sync.", task_id)
            return None

        job: Job | None = None
        if job_id is not None:
            job = await self.job_repo.get_job(job_id)
        if not job:
            job = await self.job_repo.get_job_by_task(task_id)

        # Parse owner and repo from repository_full_name ("owner/repo")
        if "/" not in link.repository_full_name:
            logger.error("Invalid repository_full_name: %s", link.repository_full_name)
            return None
        owner, repo = link.repository_full_name.split("/", 1)

        client = await self._get_client(link)
        if client is None:
            return None
        issue_number = link.pull_request_number or link.issue_number

        # ── 1. Managed Acknowledgement Comment ─────────────────────────────────
        # Post or update acknowledgement comment if we have an issue/PR number
        if issue_number and (event_name == "COMMAND_ACCEPTED" or link.acknowledgement_comment_id is None):
            marker_id = f"ack-task-{task.task_id}"
            body = self._build_acknowledgement_body(
                task=task,
                job=job,
                link=link,
                command_name=command_name,
                arguments=arguments,
            )
            comment = await client.upsert_issue_comment(
                owner=owner,
                repo=repo,
                issue_number=issue_number,
                marker_id=marker_id,
                body=body,
                # The comment was posted after the task existed: skip older ones.
                since=_comments_since(task),
            )
            link.acknowledgement_comment_id = comment.id
            logger.info(
                "Managed acknowledgement comment upserted for task %s (comment_id=%s, issue=%s)",
                task_id,
                comment.id,
                issue_number,
            )

        # ── 2. Check Run Creation & Transition ─────────────────────────────────
        target_status, target_conclusion = self._map_job_status_to_check_run(job_status)

        # Determine authoritative commit SHA
        head_sha = link.head_sha
        if not head_sha and link.pull_request_number:
            try:
                pr = await client.get_pull_request(owner, repo, link.pull_request_number)
                if pr.head_sha:
                    head_sha = pr.head_sha
                    link.head_sha = head_sha
            except Exception as exc:
                logger.warning(
                    "Could not fetch pull request #%s for SHA discovery: %s",
                    link.pull_request_number,
                    exc,
                )

        if not head_sha:
            # Ordinary issue without commit SHA: Check Run creation is explicitly skipped
            logger.info(
                "Task %s (issue #%s) has no authoritative commit SHA; skipping Check Run creation.",
                task_id,
                link.issue_number,
            )
        else:
            if link.check_run_id is None:
                # Reconcile before creating: this task's run may exist on
                # GitHub already, created by an attempt whose response was
                # lost or whose transaction never committed.
                adopted = await self._find_check_run(client, owner, repo, head_sha, task.task_id)
                if adopted is not None:
                    link.check_run_id = adopted.id
                    link.check_run_status = adopted.status
                    link.check_run_conclusion = adopted.conclusion
                    logger.warning(
                        "Adopted existing Check Run %s for task %s on %s/%s (status=%s); not creating another.",
                        adopted.id,
                        task_id,
                        owner,
                        repo,
                        adopted.status,
                    )
                    await record_audit_event(
                        self.session,
                        event_type=AuditEventType.GITHUB_CHECK_RUN_ADOPTED,
                        actor_type="SYSTEM",
                        resource_type="check_run",
                        resource_id=str(adopted.id),
                        metadata={"task_id": task_id, "head_sha": head_sha, "status": adopted.status},
                    )

            # Check monotonicity before performing any side effect
            if not self.is_monotonic_transition(link.check_run_status, target_status):
                logger.warning(
                    "Ignoring stale Check Run transition for task %s: current=%s, target=%s",
                    task_id,
                    link.check_run_status,
                    target_status,
                )
            else:
                output = self._build_check_run_output(
                    status=target_status,
                    conclusion=target_conclusion,
                    task=task,
                    job=job,
                    error=error,
                )

                if link.check_run_id is None:
                    # Initial Check Run creation
                    create_req = CreateCheckRunRequest(
                        owner=owner,
                        repo=repo,
                        name=CHECK_RUN_NAME,
                        head_sha=head_sha,
                        status=target_status,
                        conclusion=target_conclusion,
                        external_id=task.task_id,
                        output=output,
                    )
                    check_run = await client.create_check_run(create_req)
                    link.check_run_id = check_run.id
                    link.check_run_status = target_status
                    link.check_run_conclusion = target_conclusion
                    logger.info(
                        "Created Check Run %s on %s/%s (sha=%s, status=%s)",
                        check_run.id,
                        owner,
                        repo,
                        head_sha[:8],
                        target_status,
                    )
                    await record_audit_event(
                        self.session,
                        event_type=AuditEventType.GITHUB_CHECK_RUN_CREATED,
                        actor_type="SYSTEM",
                        resource_type="check_run",
                        resource_id=str(check_run.id),
                        metadata={
                            "task_id": task_id,
                            "job_id": job.id if job else None,
                            "head_sha": head_sha,
                            "status": target_status,
                            "conclusion": target_conclusion,
                        },
                    )
                else:
                    # Update existing Check Run only if status or conclusion changes
                    if (
                        link.check_run_status != target_status
                        or link.check_run_conclusion != target_conclusion
                    ):
                        update_req = UpdateCheckRunRequest(
                            owner=owner,
                            repo=repo,
                            check_run_id=link.check_run_id,
                            name=CHECK_RUN_NAME,
                            external_id=task.task_id,
                            status=target_status,
                            conclusion=target_conclusion,
                            output=output,
                        )
                        await client.update_check_run(update_req)
                        link.check_run_status = target_status
                        link.check_run_conclusion = target_conclusion
                        logger.info(
                            "Updated Check Run %s on %s/%s (status=%s, conclusion=%s)",
                            link.check_run_id,
                            owner,
                            repo,
                            target_status,
                            target_conclusion,
                        )
                        await record_audit_event(
                            self.session,
                            event_type=AuditEventType.GITHUB_CHECK_RUN_UPDATED,
                            actor_type="SYSTEM",
                            resource_type="check_run",
                            resource_id=str(link.check_run_id),
                            metadata={
                                "task_id": task_id,
                                "job_id": job.id if job else None,
                                "status": target_status,
                                "conclusion": target_conclusion,
                            },
                        )

        # Durably save link updates and commit to release row lock
        await self.user_repo.update_task_github_link(link)
        await self.session.commit()
        return link

    @staticmethod
    async def _find_check_run(
        client: GitHubClient, owner: str, repo: str, head_sha: str, task_id: str
    ) -> GitHubCheckRun | None:
        """This task's check run on *head_sha*, if CodeForge already created one; the oldest if several.

        Matched by name, by this GitHub App (another app's run with the same
        name and external_id is not ours to update) and by external_id, the
        task ID.  GitHub does not make external_id unique, so duplicates from
        before this check existed may be listed; the first one is kept.
        """
        app_id = int(settings.github_app_id) if settings.github_app_id.isdigit() else None
        runs = await client.list_check_runs_for_ref(
            owner, repo, head_sha, check_name=CHECK_RUN_NAME, app_id=app_id
        )
        mine = [
            run for run in runs
            if run.external_id == task_id and (app_id is None or run.app_id in (None, app_id))
        ]  # fmt: skip
        return min(mine, key=lambda run: run.id) if mine else None

    async def process_event(self, event: OutboxEvent) -> TaskGitHubLink | None:
        """Process a durable GITHUB_STATUS_UPDATE outbox event."""
        if event.published_at is not None:
            logger.info("Outbox event %s is already marked published, skipping.", event.id)
            return None

        event_id = event.id
        payload = event.payload or {}
        task_id = payload.get("task_id")
        if not task_id:
            logger.error("Outbox event %s missing task_id in payload, marking terminal failure.", event_id)
            await self._handle_terminal_failure(
                event=event,
                error_msg="Missing task_id in payload",
                error_type="MalformedPayloadError",
            )
            return None

        job_id = payload.get("job_id")
        job_status = payload.get("job_status", "PENDING")
        event_name = payload.get("event")
        command_name = payload.get("command")
        arguments = payload.get("arguments")
        error = payload.get("error")

        try:
            link = await self.sync_status(
                task_id=task_id,
                job_id=job_id,
                job_status=job_status,
                event_name=event_name,
                command_name=command_name,
                arguments=arguments,
                error=error,
            )

            # Mark outbox event published and record audit event
            await self.outbox_repo.mark_published(event_id)
            await record_audit_event(
                self.session,
                event_type=AuditEventType.GITHUB_STATUS_DISPATCHED,
                actor_type="SYSTEM",
                resource_type="outbox_event",
                resource_id=str(event_id),
                metadata={
                    "task_id": task_id,
                    "job_id": job_id,
                    "job_status": job_status,
                    "event": event_name,
                },
            )
            await self.session.commit()
            return link

        except Exception as exc:
            # The task is unaffected: a status that could not be shown on
            # GitHub is retried (app/outbox_policy.py), never reported as done.
            failure = classify(exc)
            logger.warning(
                "GitHub status sync failed for event %s (task=%s, %s): %s",
                event_id,
                task_id,
                failure.category.value,
                failure.error,
            )
            await self.session.rollback()
            decision = await self.outbox_repo.record_failure(event_id, failure)
            if decision is not None and decision.give_up:
                await self._audit_failed(event_id, failure.error, failure.category.value, decision.reason)
            await self.session.commit()
            return None

    async def _handle_terminal_failure(
        self,
        event: OutboxEvent,
        error_msg: str,
        error_type: str,
    ) -> None:
        """Give up an event that can never be delivered, without failing the CodeForge task/job."""
        event_id = event.id
        await self.session.rollback()
        decision = await self.outbox_repo.mark_failed(event_id, f"{error_type}: {error_msg}")
        if decision is not None:
            await self._audit_failed(event_id, error_msg, FailureCategory.PERMANENT.value, decision.reason)
        await self.session.commit()

    async def _audit_failed(self, event_id: int, error: str, category: str, reason: str) -> None:
        await record_audit_event(
            self.session,
            event_type=AuditEventType.GITHUB_STATUS_FAILED,
            actor_type="SYSTEM",
            resource_type="outbox_event",
            resource_id=str(event_id),
            result="FAILED",
            metadata={"error": error, "category": category, "reason": reason},
        )
