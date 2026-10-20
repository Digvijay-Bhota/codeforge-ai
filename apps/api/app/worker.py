import asyncio
import logging
import signal
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.config import settings
from app.db.models import (
    PUBLICATION_APPROVAL,
    ApprovalStatusEnum,
    Job,
    JobStatusEnum,
    OutboxEvent,
    Task,
    TaskApproval,
    TaskPublicationArtifact,
    TaskStatusEnum,
)
from app.db.repositories.job_repository import MAX_JOB_ATTEMPTS, JobRepository
from app.db.repositories.outbox_repository import OutboxRepository
from app.db.repositories.task_repository import TaskRepository
from app.db.session import async_session_maker
from app.observability.events import AuditEventType, EventType
from app.observability.metrics import inc_counter
from app.observability.tracing import record_audit_event, record_event
from app.schemas.task import ExecutionTarget, GitHubPublicationMetadata, TaskRequest, TaskStatus
from app.services.queue_service import QueueService
from app.services.task_service import TaskService, expire_publication_requests, parse_plan_dict
from app.services.task_state_machine import InvalidTaskTransitionError, TaskStateMachine

logger = logging.getLogger("worker")

# Longest interval between lease renewals.  Lease loss is noticed within one
# interval.  It never exceeds a third of TASK_LEASE_SECONDS (a guard: settings
# validation already keeps the lease at or above MIN_TASK_LEASE_SECONDS).
HEARTBEAT_INTERVAL_SECONDS: float = 60
# Seconds between recovery passes (see recover_jobs).
RECOVERY_INTERVAL_SECONDS: float = 60

# Task failure codes written by recovery.
JOB_ATTEMPTS_EXHAUSTED = "JOB_ATTEMPTS_EXHAUSTED"
JOB_INTERRUPTED = "JOB_INTERRUPTED"
_TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatusEnum.COMPLETED.value, TaskStatusEnum.FAILED.value, TaskStatusEnum.CANCELLED.value}
)


def heartbeat_interval() -> float:
    """Seconds between renewals: at most a third of TASK_LEASE_SECONDS."""
    return min(HEARTBEAT_INTERVAL_SECONDS, settings.task_lease_seconds / 3)


async def heartbeat_loop(
    job_id: int, worker_id: str, lease_version: int, stop_event: asyncio.Event
) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.sleep(heartbeat_interval())
            if stop_event.is_set():
                break
            async with async_session_maker() as session:
                job_repo = JobRepository(session)
                success = await job_repo.renew_lease(job_id, worker_id, lease_version)
                if not success:
                    logger.error("Heartbeat failed for job %s: ownership lost", job_id)
                    stop_event.set()
                    break
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Heartbeat error: %s", exc)


async def _fail_task_for_job(
    session: AsyncSession, task: Task, job: Job, failure_code: str, reason: str
) -> bool:
    """Move *task* to FAILED for *job*'s terminal failure, unless it is already terminal.

    Emits the same GitHub status update as an ordinary job failure.  Returns
    whether the task changed.  The caller commits.
    """
    if task.status in _TERMINAL_TASK_STATUSES:
        return False
    task.failure_code = failure_code
    task.failure_reason = reason
    await TaskStateMachine(TaskRepository(session)).transition(
        task, TaskStatusEnum.FAILED, message=reason
    )
    if task.execution_target in (ExecutionTarget.github.value, "github"):
        await OutboxRepository(session).create_event(
            OutboxEvent(
                event_type="GITHUB_STATUS_UPDATE",
                aggregate_id=task.task_id,
                payload={
                    "task_id": task.task_id,
                    "job_id": job.id,
                    "job_status": JobStatusEnum.FAILED.value,
                    "event": "JOB_FAILED",
                    "error": reason,
                },
            )
        )
    return True


async def _fail_unstartable_job(session: AsyncSession, job: Job, task: Task, exc: Exception) -> None:
    """Commit a terminal failure for a claimed job whose task cannot be started.

    The job was claimed in this session.  Returning without committing would
    roll the claim back, leaving the job PENDING with no queue message and its
    attempt uncounted, so it would never run or fail.  A task that a previous
    attempt already moved past QUEUED cannot resume mid-run (its workspace or
    branch may hold partial changes); it fails and can be retried by the user.
    """
    logger.error("Job %s cannot start task %s (%s): %s", job.id, task.task_id, task.status, exc)
    if task.status in _TERMINAL_TASK_STATUSES:
        reason = f"Task is already {task.status}; job not run."
    else:
        reason = (
            "A previous execution attempt was interrupted after the task had started "
            f"(status {task.status}); tasks cannot resume mid-run. Retry the task to run it again."
        )
    job.status = JobStatusEnum.FAILED.value
    job.worker_id = None
    job.last_error = reason
    job.completed_at = func.now()  # type: ignore[assignment]
    await _fail_task_for_job(session, task, job, JOB_INTERRUPTED, reason)
    await session.commit()


async def _request_publication_approval(
    session: AsyncSession, task: Task, artifact: dict[str, Any]
) -> None:
    """Store the generated changes and wait for a human to approve publishing them (CF-05).

    Nothing has been pushed: the task waits in WAITING_PUBLICATION with a
    pending PUBLICATION approval bound to the artifact's digest.
    """
    session.add(
        TaskPublicationArtifact(
            task_id=task.task_id,
            repository=artifact["repository"],
            base_branch=artifact["base_branch"],
            base_sha=artifact["base_sha"],
            commit_message=artifact["commit_message"],
            pull_request_title=artifact["pull_request_title"],
            pull_request_body=artifact["pull_request_body"],
            files=artifact["files"],
            digest=artifact["digest"],
            total_bytes=artifact["total_bytes"],
        )
    )
    timeout_hours = int((task.approval_config or {}).get("timeout_hours", 24))
    session.add(
        TaskApproval(
            task_id=task.task_id,
            approval_type=PUBLICATION_APPROVAL,
            status=ApprovalStatusEnum.PENDING.value,
            requested_by="worker",
            artifact_digest=artifact["digest"],
            expires_at=datetime.now(UTC) + timedelta(hours=timeout_hours),
        )
    )
    await TaskStateMachine(TaskRepository(session)).transition(
        task,
        TaskStatusEnum.WAITING_PUBLICATION,
        message="Generated changes are awaiting publication approval",
        metadata={"artifact_digest": artifact["digest"]},
    )


@dataclass
class PublicationOutcome:
    metadata: GitHubPublicationMetadata | None = None
    error: str | None = None
    # A GitHub-side failure (clone, push, pull request) that a later attempt
    # may get past; refusals of the approval or the artifact are never retried.
    retryable: bool = False


# GitHub-side failures worth retrying with the same approval.
_RETRYABLE_PUBLICATION_STAGES = frozenset({
    "REPOSITORY_ACQUISITION_FAILED", "PUSH_FAILED", "PULL_REQUEST_FAILED",
})


def _unpublishable(artifact: dict[str, Any] | None) -> str | None:
    """Why a generation result's artifact cannot be stored for review, or None."""
    from app.execution.publication import PublicationError, verify_artifact

    if not artifact:
        return "No reviewable changes were captured for publication."
    try:
        verify_artifact(artifact, artifact.get("digest", ""))
    except (PublicationError, KeyError) as exc:
        return f"Changes cannot be prepared for review: {exc}"
    return None


async def _approver_refusal(session: AsyncSession, approval: TaskApproval, repository: str) -> str | None:
    """Why the stored approver may not publish to *repository* now, or None.

    Reads the user afresh and checks write access without the permission
    cache, so a deactivation or revocation since the approval counts.
    """
    from sqlalchemy import select

    from app.db.models import User
    from app.services.authorization_service import AuthorizationService

    approver = None
    if approval.approved_by_user_id:
        approver = (
            await session.execute(
                select(User)
                .where(User.id == approval.approved_by_user_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    if approver is None or not approver.is_active:
        return "The approving user is no longer active."
    if not await AuthorizationService(session).can_write_repository(approver, repository, use_cache=False):
        return "The approving user no longer has write access to the repository."
    return None


async def _publish_approved_artifact(session: AsyncSession, task: Task) -> PublicationOutcome:
    """Publish a task's approved artifact, re-checking every condition first.

    Nothing is pushed unless every check passes.
    """
    from app.execution.github_execution import GitHubExecutionError, GitHubExecutionService
    from app.execution.publication import (
        PublicationError,
        PublicationUnauthorizedError,
        stored_artifact,
        verify_artifact,
    )

    task_repo = TaskRepository(session)
    artifact = await task_repo.get_publication_artifact(task.task_id)
    approval = await task_repo.get_latest_publication_approval(task.task_id)
    if artifact is None:
        return PublicationOutcome(error="No generated changes are stored for publication.")
    if (
        approval is None
        or approval.status != ApprovalStatusEnum.APPROVED.value
        or approval.artifact_digest != artifact.digest
    ):
        return PublicationOutcome(error="Publication has not been approved for these exact changes.")
    try:
        verify_artifact(stored_artifact(artifact), artifact.digest)
    except PublicationError as exc:
        return PublicationOutcome(error=str(exc))
    if artifact.repository != task.repository:
        return PublicationOutcome(error="The stored changes target a different repository.")

    # The approver must still be an active user with write access; checked
    # now, and again by publish() right before each remote side effect.
    refusal = await _approver_refusal(session, approval, artifact.repository)
    if refusal:
        return PublicationOutcome(error=refusal)
    repository = artifact.repository

    async def authorize() -> None:
        refusal = await _approver_refusal(session, approval, repository)
        if refusal:
            raise PublicationUnauthorizedError(refusal)

    if artifact.published_at is not None and artifact.pull_request_number is not None:
        # An earlier attempt finished publishing; report it instead of repeating it.
        return PublicationOutcome(
            metadata=GitHubPublicationMetadata(
                repository=artifact.repository,
                base_branch=artifact.base_branch,
                working_branch=f"codeforge/task-{task.task_id}",
                commit_sha=artifact.commit_sha or "",
                pull_request_number=artifact.pull_request_number,
                pull_request_url=artifact.pull_request_url or "",
            )
        )

    try:
        metadata = await GitHubExecutionService().publish(
            task.task_id,
            {
                **stored_artifact(artifact),
                "digest": artifact.digest,
                "created_ts": int(artifact.created_at.timestamp()),
            },
            approved_by=approval.approved_by or "unknown",
            authorize=authorize,
        )
    except PublicationUnauthorizedError as exc:
        return PublicationOutcome(error=str(exc))
    except GitHubExecutionError as exc:
        return PublicationOutcome(
            error=str(exc), retryable=exc.stage.value in _RETRYABLE_PUBLICATION_STAGES
        )
    artifact.published_at = func.now()  # type: ignore[assignment]
    artifact.commit_sha = metadata.commit_sha
    artifact.pull_request_number = metadata.pull_request_number
    artifact.pull_request_url = metadata.pull_request_url
    return PublicationOutcome(metadata=metadata)


async def process_job(job_id: int) -> None:
    is_resumed = False
    is_approval_needed = False
    is_publication = False

    async with async_session_maker() as session:
        job_repo = JobRepository(session)
        task_repo = TaskRepository(session)
        state_machine = TaskStateMachine(task_repo)

        job = await job_repo.claim_job(job_id=job_id, worker_id=settings.task_worker_id)
        if not job:
            return

        task = await task_repo.get_task(job.task_id)
        if not task:
            return

        lease_version = job.lease_version

        # Check if this is a resumed job for an approved plan
        latest_approval = await task_repo.get_latest_approval(task.task_id)
        if task.status == TaskStatusEnum.PUBLISHING.value:
            # Publication of approved changes (CF-05).  No transition here: every
            # condition is re-checked at the publication boundary below.
            is_publication = True
        elif (
            latest_approval
            and latest_approval.status == ApprovalStatusEnum.APPROVED.value
            and task.implementation_plan
            and task.status == TaskStatusEnum.QUEUED.value
        ):
            is_resumed = True
            try:
                task = await state_machine.transition(task, TaskStatusEnum.CODING)
            except InvalidTaskTransitionError as exc:
                await _fail_unstartable_job(session, job, task, exc)
                return
        else:
            approval_config = task.approval_config or {}
            is_approval_needed = bool(approval_config.get("require_plan_approval", False))
            try:
                if task.status == TaskStatusEnum.PENDING.value:
                    task = await state_machine.transition(task, TaskStatusEnum.QUEUED)
                task = await state_machine.transition(task, TaskStatusEnum.ANALYZING)
            except InvalidTaskTransitionError as exc:
                await _fail_unstartable_job(session, job, task, exc)
                return

        if task.execution_target in (ExecutionTarget.github.value, "github"):
            outbox_repo = OutboxRepository(session)
            await outbox_repo.create_event(
                OutboxEvent(
                    event_type="GITHUB_STATUS_UPDATE",
                    aggregate_id=task.task_id,
                    payload={
                        "task_id": task.task_id,
                        "job_id": job.id,
                        "job_status": JobStatusEnum.RUNNING.value,
                        "event": "JOB_STARTED",
                    },
                )
            )

        await session.commit()

    from app.observability.context import reset_observability_context, set_observability_context

    obs_tokens = set_observability_context(
        task_id=job.task_id,
        job_id=job.id,
        execution_id=job.execution_id,
        worker_id=settings.task_worker_id,
        db_session=session,
    )

    # Per-job.  Set when this worker loses the job's lease, which cancels the
    # active execution (run_until_ownership_lost); the finally block also sets
    # it once the job is done, to stop the heartbeat.
    stop_event = asyncio.Event()
    ownership_lost_flag = [False]

    async def heartbeat_loop_wrapper():
        await heartbeat_loop(job_id, settings.task_worker_id, lease_version, stop_event)
        ownership_lost_flag[0] = True

    async with async_session_maker() as session:
        task_service = TaskService(session)
        task_repo = TaskRepository(session)
        state_machine = TaskStateMachine(task_repo)
        job_repo = JobRepository(session)

        task = await task_repo.get_task(job.task_id)
        if not task:
            stop_event.set()
            ownership_lost_flag[0] = True
            return

        request = TaskRequest(
            execution_target=ExecutionTarget(task.execution_target),
            github_repository=task.repository,
            workspace_path=task.workspace_path or "",
            description=task.requested_task,
            require_plan_approval=is_approval_needed,
            approval_config=task.approval_config,
        )

        from app.execution.ownership import (
            OwnershipLostError,
            reset_async_ownership_verifier,
            reset_ownership_verifier,
            run_until_ownership_lost,
            set_async_ownership_verifier,
            set_ownership_verifier,
        )
        from app.orchestration.stages import _bound_text

        def verify_still_owns_job():
            if ownership_lost_flag[0]:
                raise OwnershipLostError(f"Job {job.id} lease expired or lost")

        async def verify_authoritative_ownership():
            if ownership_lost_flag[0]:
                raise OwnershipLostError(f"Job {job.id} lease expired or lost locally")

            job2 = await job_repo.get_job(job.id)
            if (
                not job2
                or job2.worker_id != settings.task_worker_id
                or job2.lease_version != lease_version
            ):
                ownership_lost_flag[0] = True
                stop_event.set()
                raise OwnershipLostError(f"Job {job.id} ownership lost in DB")

        token = set_ownership_verifier(verify_still_owns_job)
        async_token = set_async_ownership_verifier(verify_authoritative_ownership)

        # Started only here, right before the try whose finally stops it: if
        # anything above raises, no heartbeat is left renewing the lease forever.
        # The job then keeps its claim lease, which expires, and recovery
        # returns it (the next delivery closes it as interrupted).
        heartbeat_task = asyncio.create_task(heartbeat_loop_wrapper())
        try:
            job_start_time = datetime.now(UTC)
            await record_event(session, EventType.JOB_EXECUTION_STARTED, component="worker")
            inc_counter("codeforge_jobs_started_total")

            # ── PATH D: Publish approved changes (CF-05) ──
            if is_publication:
                outcome = await run_until_ownership_lost(
                    _publish_approved_artifact(session, task), stop_event
                )
                job2 = await job_repo.get_job_for_update(job.id)
                if (
                    not job2
                    or job2.worker_id != settings.task_worker_id
                    or job2.lease_version != lease_version
                ):
                    logger.error("Ownership lost for job %s prior to publication result", job.id)
                    ownership_lost_flag[0] = True
                    stop_event.set()
                    return
                error = _bound_text(outcome.error) if outcome.error else "Publication failed"
                if outcome.metadata is None and outcome.retryable and job2.attempt_count < MAX_JOB_ATTEMPTS:
                    # Keep the task PUBLISHING under the same approval; recovery
                    # re-enqueues the job once it has been pending for a lease
                    # period, and exhausting its attempts fails the task.
                    logger.warning("Publication of task %s failed, will retry: %s", task.task_id, error)
                    job2.status = JobStatusEnum.PENDING.value
                    job2.worker_id = None
                    job2.lease_version = job2.lease_version + 1
                    job2.available_at = func.now()  # type: ignore[assignment]
                    job2.last_error = error
                    await session.commit()
                    return
                if outcome.metadata is not None:
                    task = await state_machine.transition(task, TaskStatusEnum.COMPLETED)
                    task.pr_metadata = outcome.metadata.model_dump()
                    job2.status = JobStatusEnum.SUCCEEDED.value
                else:
                    task = await state_machine.transition(task, TaskStatusEnum.FAILED, message=error)
                    task.failure_code = "PUBLICATION_FAILED"
                    task.failure_reason = error
                    job2.status = JobStatusEnum.FAILED.value
                    job2.last_error = error
                job2.completed_at = func.now()  # type: ignore
                await OutboxRepository(session).create_event(
                    OutboxEvent(
                        event_type="GITHUB_STATUS_UPDATE",
                        aggregate_id=task.task_id,
                        payload={
                            "task_id": task.task_id,
                            "job_id": job2.id,
                            "job_status": job2.status,
                            "event": "JOB_COMPLETED" if outcome.metadata is not None else "JOB_FAILED",
                            "error": task.failure_reason if outcome.metadata is None else None,
                        },
                    )
                )
                await session.commit()
                return

            # ── PATH A: Stage A Plan-Only Approval Flow ──
            if is_approval_needed:
                task = await state_machine.transition(task, TaskStatusEnum.PLANNING)
                await session.commit()

                result = await run_until_ownership_lost(
                    task_service.execute_task(
                        request,
                        task.task_id,
                        stop_after_plan=True,
                        creator_id=task.creator_id,
                    ),
                    stop_event,
                )

                job2 = await job_repo.get_job_for_update(job.id)
                if (
                    not job2
                    or job2.worker_id != settings.task_worker_id
                    or job2.lease_version != lease_version
                ):
                    logger.error("Ownership lost for job %s prior to approval checkpoint", job.id)
                    ownership_lost_flag[0] = True
                    stop_event.set()
                    return

                job_end_time = datetime.now(UTC)
                job_duration_ms = int((job_end_time - job_start_time).total_seconds() * 1000)

                if result.status == "success" and (result.full_plan or result.plan):
                    task.implementation_plan = result.full_plan or {
                        "steps": [p.model_dump() for p in result.plan]
                    }
                    task.final_message = (
                        _bound_text(result.agent_output) if result.agent_output else None
                    )
                    task = await state_machine.transition(task, TaskStatusEnum.WAITING_APPROVAL)

                    # Create pending approval record
                    timeout_hours = int((task.approval_config or {}).get("timeout_hours", 24))
                    approval = TaskApproval(
                        task_id=task.task_id,
                        approval_type="plan",
                        status=ApprovalStatusEnum.PENDING.value,
                        requested_by="worker",
                        expires_at=datetime.now(UTC) + timedelta(hours=timeout_hours),
                    )
                    await task_repo.create_approval(approval)

                    # Mark Stage A job as SUCCEEDED and yield lease cleanly
                    job2.status = JobStatusEnum.SUCCEEDED.value
                    job2.completed_at = func.now()  # type: ignore

                    await record_event(
                        session,
                        EventType.APPROVAL_REQUESTED,
                        component="worker",
                        metadata={"timeout_hours": timeout_hours},
                    )
                    await record_audit_event(
                        session,
                        AuditEventType.APPROVAL_REQUESTED,
                        actor_type="SYSTEM",
                        resource_type="task",
                        resource_id=task.task_id,
                        metadata={"timeout_hours": timeout_hours, "approval_id": approval.id},
                    )

                    if task.execution_target in (ExecutionTarget.github.value, "github"):
                        outbox_repo = OutboxRepository(session)
                        await outbox_repo.create_event(
                            OutboxEvent(
                                event_type="GITHUB_STATUS_UPDATE",
                                aggregate_id=task.task_id,
                                payload={
                                    "task_id": task.task_id,
                                    "job_id": job2.id,
                                    "job_status": JobStatusEnum.SUCCEEDED.value,
                                    "event": "JOB_COMPLETED",
                                },
                            )
                        )

                    await session.commit()
                    return
                else:
                    task = await state_machine.transition(task, TaskStatusEnum.FAILED)
                    job2.status = JobStatusEnum.FAILED.value
                    task.failure_reason = (
                        _bound_text(result.error_message)
                        if result.error_message
                        else "Plan generation failed"
                    )
                    job2.last_error = task.failure_reason
                    job2.completed_at = func.now()  # type: ignore
                    await record_event(
                        session,
                        EventType.JOB_EXECUTION_FAILED,
                        component="worker",
                        status="failed",
                        error_code="PLAN_FAILED",
                        duration_ms=job_duration_ms,
                    )

                    if task.execution_target in (ExecutionTarget.github.value, "github"):
                        outbox_repo = OutboxRepository(session)
                        await outbox_repo.create_event(
                            OutboxEvent(
                                event_type="GITHUB_STATUS_UPDATE",
                                aggregate_id=task.task_id,
                                payload={
                                    "task_id": task.task_id,
                                    "job_id": job2.id,
                                    "job_status": JobStatusEnum.FAILED.value,
                                    "event": "JOB_FAILED",
                                    "error": task.failure_reason,
                                },
                            )
                        )

                    await session.commit()
                    return

            # ── PATH B: Resumed Execution from Approved Plan ──
            if is_resumed:
                initial_plan = parse_plan_dict(task.implementation_plan)
                task = await state_machine.transition(task, TaskStatusEnum.TESTING)
                await session.commit()

                result = await run_until_ownership_lost(
                    task_service.execute_task(
                        request,
                        task.task_id,
                        initial_plan=initial_plan,
                        stop_after_plan=False,
                        creator_id=task.creator_id,
                    ),
                    stop_event,
                )
            else:
                # ── PATH C: Standard Full Execution ──
                task = await state_machine.transition(task, TaskStatusEnum.PLANNING)
                task = await state_machine.transition(task, TaskStatusEnum.CODING)
                task = await state_machine.transition(task, TaskStatusEnum.TESTING)
                await session.commit()

                result = await run_until_ownership_lost(
                    task_service.execute_task(request, task.task_id, creator_id=task.creator_id),
                    stop_event,
                )

            # Verify ownership before finalizing
            job2 = await job_repo.get_job_for_update(job.id)
            if (
                not job2
                or job2.worker_id != settings.task_worker_id
                or job2.lease_version != lease_version
            ):
                logger.error("Ownership lost for job %s prior to completion", job.id)
                ownership_lost_flag[0] = True
                stop_event.set()
                return

            job_end_time = datetime.now(UTC)
            job_duration_ms = int((job_end_time - job_start_time).total_seconds() * 1000)

            is_github = task.execution_target in (ExecutionTarget.github.value, "github")
            awaiting_publication = False
            if result.status == "success" and is_github:
                # A GitHub task must end with valid, storable, reviewable
                # changes; never complete or wait for approval without them.
                problem = _unpublishable(result.publication_artifact)
                if problem:
                    result = result.model_copy(update={"status": TaskStatus.error, "error_message": problem})
            if result.status == "success":
                if is_github:
                    # Generated changes are never published here: they wait for
                    # a human's publication approval (CF-05).
                    artifact = cast(dict[str, Any], result.publication_artifact)  # non-None: checked above
                    await _request_publication_approval(session, task, artifact)
                    awaiting_publication = True
                else:
                    task = await state_machine.transition(task, TaskStatusEnum.COMPLETED)
                job2.status = JobStatusEnum.SUCCEEDED.value
                await record_event(
                    session,
                    EventType.JOB_EXECUTION_COMPLETED,
                    component="worker",
                    status="success",
                    duration_ms=job_duration_ms,
                )
                inc_counter("codeforge_jobs_succeeded_total")
            else:
                task = await state_machine.transition(task, TaskStatusEnum.FAILED)
                job2.status = JobStatusEnum.FAILED.value
                await record_event(
                    session,
                    EventType.JOB_EXECUTION_FAILED,
                    component="worker",
                    status="failed",
                    error_code="TASK_FAILED",
                    duration_ms=job_duration_ms,
                )
                inc_counter("codeforge_jobs_failed_total")

            task.failure_reason = (
                _bound_text(result.error_message) if result.error_message else None
            )
            task.final_message = _bound_text(result.agent_output) if result.agent_output else None

            # Truncate diff inside task_result just in case.  The publication
            # artifact is stored on its own (bounded, digest-bound), never here.
            result_dict = result.model_dump(exclude={"publication_artifact"})
            if result_dict.get("diff"):
                result_dict["diff"] = _bound_text(result_dict["diff"])

            if result.full_plan:
                task.implementation_plan = result.full_plan
            elif result.plan and not task.implementation_plan:
                task.implementation_plan = {"steps": [p.model_dump() for p in result.plan]}

            task.task_result = result_dict
            if result.github:
                task.pr_metadata = result.github.model_dump()

            job2.completed_at = func.now()  # type: ignore

            # Evaluate Task
            from app.observability.evaluation import TaskEvaluator

            evaluator = TaskEvaluator(session)
            await evaluator.evaluate_task(
                task.task_id, str(job.execution_id), result, job_duration_ms
            )

            # While awaiting publication approval the check run stays in progress;
            # the publication job reports the final outcome.
            if is_github and not awaiting_publication:
                outbox_repo = OutboxRepository(session)
                await outbox_repo.create_event(
                    OutboxEvent(
                        event_type="GITHUB_STATUS_UPDATE",
                        aggregate_id=task.task_id,
                        payload={
                            "task_id": task.task_id,
                            "job_id": job2.id,
                            "job_status": job2.status,
                            "event": "JOB_COMPLETED" if job2.status == JobStatusEnum.SUCCEEDED.value else "JOB_FAILED",
                            "error": task.failure_reason,
                        },
                    )
                )

            await session.commit()
        except OwnershipLostError as exc:
            # This worker no longer holds the lease (renewal failed or the DB
            # names another owner), so whoever holds it now decides the outcome.
            # Write nothing: no task/job state, no result, no GitHub status.
            logger.error("Job %s abandoned without finalizing: %s", job.id, exc)
            ownership_lost_flag[0] = True
            stop_event.set()
            return
        except Exception as exc:
            logger.exception("Worker execution failed")
            job2 = await job_repo.get_job_for_update(job.id)
            if (
                not job2
                or job2.worker_id != settings.task_worker_id
                or job2.lease_version != lease_version
            ):
                ownership_lost_flag[0] = True
                stop_event.set()
                return

            task = await state_machine.transition(task, TaskStatusEnum.FAILED)

            error_str = _bound_text(str(exc))
            task.failure_reason = error_str
            job2.status = JobStatusEnum.FAILED.value
            job2.last_error = error_str
            job2.completed_at = func.now()  # type: ignore

            if task and task.execution_target in (ExecutionTarget.github.value, "github"):
                outbox_repo = OutboxRepository(session)
                await outbox_repo.create_event(
                    OutboxEvent(
                        event_type="GITHUB_STATUS_UPDATE",
                        aggregate_id=task.task_id,
                        payload={
                            "task_id": task.task_id,
                            "job_id": job2.id,
                            "job_status": JobStatusEnum.FAILED.value,
                            "event": "JOB_FAILED",
                            "error": error_str,
                        },
                    )
                )

            await session.commit()
        finally:
            ownership_lost_flag[0] = True
            stop_event.set()
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
            reset_ownership_verifier(token)
            reset_async_ownership_verifier(async_token)
            reset_observability_context(obs_tokens)


@dataclass
class JobRecovery:
    """What one recovery pass did."""

    failed: list[int] = field(default_factory=list)
    released: list[int] = field(default_factory=list)
    requeued: list[int] = field(default_factory=list)
    expired_publications: list[str] = field(default_factory=list)


async def recover_jobs(queue: QueueService) -> JobRecovery:
    """One recovery pass over the jobs table.

    1. Jobs that used all MAX_JOB_ATTEMPTS and are not actively leased are
       marked FAILED, and their tasks FAILED (JOB_ATTEMPTS_EXHAUSTED).
    2. RUNNING jobs whose lease expired (their worker died) return to PENDING.
    3. PENDING jobs unclaimed for TASK_LEASE_SECONDS are presumed to have lost
       their queue message.
    4. Tasks whose publication approval request expired are cancelled (CF-05).

    Database changes commit first; only then are the jobs from 2 and 3
    enqueued, so a crash in between loses nothing (the next pass finds the
    same jobs stale).  Jobs from 3 still in the queue are not enqueued again.
    A duplicate message is harmless: claim_job lets only one worker run a job.
    Every step is a conditional UPDATE, so concurrent passes do not overlap.
    """
    result = JobRecovery()
    async with async_session_maker() as session:
        job_repo = JobRepository(session)
        task_repo = TaskRepository(session)
        reason = f"Job exhausted its {MAX_JOB_ATTEMPTS} execution attempts."
        for job in await job_repo.fail_exhausted_jobs(reason):
            result.failed.append(job.id)
            task = await task_repo.get_task_for_update(job.task_id)
            if task is not None:
                await _fail_task_for_job(session, task, job, JOB_ATTEMPTS_EXHAUSTED, reason)
        result.released = await job_repo.release_expired_leases()
        stale = await job_repo.refresh_stale_pending(settings.task_lease_seconds)
        result.expired_publications = await expire_publication_requests(session)
        await session.commit()

    for job_id in result.failed:
        logger.warning("Recovery: job %s exhausted its attempts; marked FAILED", job_id)
    for job_id in result.released:
        logger.info("Recovery: job %s lease expired; re-enqueued", job_id)
        await queue.enqueue(job_id)
    for job_id in stale:
        if await queue.is_queued(job_id):
            continue
        logger.info("Recovery: job %s pending with no queue message; re-enqueued", job_id)
        await queue.enqueue(job_id)
        result.requeued.append(job_id)
    return result


async def reap_stale_jobs() -> None:
    queue = QueueService()
    while True:
        try:
            await recover_jobs(queue)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Reaper error: %s", exc)

        await asyncio.sleep(RECOVERY_INTERVAL_SECONDS)


async def worker_main() -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def handle_sigterm():
        logger.info("Received termination signal, shutting down gracefully...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_sigterm)

    reaper_task = asyncio.create_task(reap_stale_jobs())
    queue = QueueService()

    logger.info("Worker started.")

    while not stop_event.is_set():
        try:
            # We use wait_for so we can periodically check stop_event
            job_id_str = await asyncio.wait_for(queue.dequeue(timeout=5), timeout=5.0)
            if job_id_str:
                await process_job(int(job_id_str))
        except TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Worker error: %s", exc)
            await asyncio.sleep(1)

    logger.info("Worker stopped, cancelling reaper...")
    reaper_task.cancel()

    # Close resources
    from app.db.session import engine
    from app.services.queue_service import get_redis_client

    await engine.dispose()
    redis = get_redis_client()
    await redis.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(worker_main())
