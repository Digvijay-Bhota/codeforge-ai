import asyncio
import logging
from datetime import UTC

from sqlalchemy.sql import func

from app.config import settings
from app.db.models import JobStatusEnum, TaskStatusEnum
from app.db.repositories.job_repository import JobRepository
from app.db.repositories.task_repository import TaskRepository
from app.db.session import async_session_maker
from app.schemas.task import ExecutionTarget, TaskRequest
from app.services.queue_service import QueueService
from app.services.task_service import TaskService
from app.services.task_state_machine import TaskStateMachine

logger = logging.getLogger("worker")

async def heartbeat_loop(job_id: int, worker_id: str, lease_version: int, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.sleep(60)
            if stop_event.is_set():
                break
            async with async_session_maker() as session:
                job_repo = JobRepository(session)
                success = await job_repo.renew_lease(job_id, worker_id, lease_version, lease_seconds=3600)
                if not success:
                    logger.error("Heartbeat failed for job %s: ownership lost", job_id)
                    stop_event.set()
                    break
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Heartbeat error: %s", exc)

async def process_job(job_id: int) -> None:
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

        try:
            if task.status == TaskStatusEnum.PENDING.value:
                task = await state_machine.transition(task, TaskStatusEnum.QUEUED)
            task = await state_machine.transition(task, TaskStatusEnum.ANALYZING)
        except Exception as exc:
            logger.error("Failed to transition task %s: %s", task.task_id, exc)
            return

        await session.commit()

    from app.observability.context import reset_observability_context, set_observability_context
    obs_tokens = set_observability_context(
        task_id=job.task_id,
        job_id=job.id,
        execution_id=job.execution_id,
        worker_id=settings.task_worker_id,
        db_session=session
    )

    stop_event = asyncio.Event()
    ownership_lost_flag = [False]

    async def heartbeat_loop_wrapper():
        await heartbeat_loop(job_id, settings.task_worker_id, lease_version, stop_event)
        ownership_lost_flag[0] = True

    heartbeat_task = asyncio.create_task(heartbeat_loop_wrapper())

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
            description=task.requested_task
        )

        from app.execution.ownership import (
            OwnershipLostError,
            reset_async_ownership_verifier,
            reset_ownership_verifier,
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
            if not job2 or job2.worker_id != settings.task_worker_id or job2.lease_version != lease_version:
                ownership_lost_flag[0] = True
                stop_event.set()
                raise OwnershipLostError(f"Job {job.id} ownership lost in DB")

        token = set_ownership_verifier(verify_still_owns_job)
        async_token = set_async_ownership_verifier(verify_authoritative_ownership)

        try:
            from datetime import datetime

            from app.observability.events import EventType
            from app.observability.metrics import inc_counter
            from app.observability.tracing import record_event

            job_start_time = datetime.now(UTC)
            await record_event(session, EventType.JOB_EXECUTION_STARTED, component="worker")
            inc_counter("codeforge_jobs_started_total")

            task = await state_machine.transition(task, TaskStatusEnum.PLANNING)
            task = await state_machine.transition(task, TaskStatusEnum.CODING)
            task = await state_machine.transition(task, TaskStatusEnum.TESTING)
            await session.commit()

            result = await task_service.execute_task(request, task.task_id)

            # Verify ownership before finalizing
            job2 = await job_repo.get_job_for_update(job.id)
            if not job2 or job2.worker_id != settings.task_worker_id or job2.lease_version != lease_version:
                logger.error("Ownership lost for job %s prior to completion", job.id)
                ownership_lost_flag[0] = True
                stop_event.set()
                return

            job_end_time = datetime.now(UTC)
            job_duration_ms = int((job_end_time - job_start_time).total_seconds() * 1000)

            if result.status == "success":
                task = await state_machine.transition(task, TaskStatusEnum.COMPLETED)
                job2.status = JobStatusEnum.SUCCEEDED.value
                await record_event(
                    session, EventType.JOB_EXECUTION_COMPLETED, component="worker",
                    status="success", duration_ms=job_duration_ms
                )
                inc_counter("codeforge_jobs_succeeded_total")
            else:
                task = await state_machine.transition(task, TaskStatusEnum.FAILED)
                job2.status = JobStatusEnum.FAILED.value
                await record_event(
                    session, EventType.JOB_EXECUTION_FAILED, component="worker",
                    status="failed", error_code="TASK_FAILED", duration_ms=job_duration_ms
                )
                inc_counter("codeforge_jobs_failed_total")

            task.failure_reason = _bound_text(result.error_message) if result.error_message else None
            task.final_message = _bound_text(result.agent_output) if result.agent_output else None

            # Truncate diff inside task_result just in case
            result_dict = result.dict()
            if result_dict.get("diff"):
                result_dict["diff"] = _bound_text(result_dict["diff"])

            task.implementation_plan = {"steps": [p.dict() for p in result.plan]} if result.plan else None
            task.task_result = result_dict
            job2.completed_at = func.now()  # type: ignore

            # Evaluate Task
            from app.observability.evaluation import TaskEvaluator
            evaluator = TaskEvaluator(session)
            await evaluator.evaluate_task(task.task_id, str(job.execution_id), result, job_duration_ms)

            await session.commit()
        except Exception as exc:
            logger.exception("Worker execution failed")
            job2 = await job_repo.get_job_for_update(job.id)
            if not job2 or job2.worker_id != settings.task_worker_id or job2.lease_version != lease_version:
                ownership_lost_flag[0] = True
                stop_event.set()
                return

            task = await state_machine.transition(task, TaskStatusEnum.FAILED)

            error_str = _bound_text(str(exc))
            task.failure_reason = error_str
            job2.status = JobStatusEnum.FAILED.value
            job2.last_error = error_str
            job2.completed_at = func.now()  # type: ignore
            await session.commit()
        finally:
            ownership_lost_flag[0] = True
            stop_event.set()
            heartbeat_task.cancel()
            reset_ownership_verifier(token)
            reset_async_ownership_verifier(async_token)
            reset_observability_context(obs_tokens)

async def reap_stale_jobs() -> None:
    queue = QueueService()
    while True:
        try:
            async with async_session_maker() as session:
                from sqlalchemy import update

                from app.db.models import Job, JobStatusEnum

                stmt = (
                    update(Job)
                    .where(
                        Job.status == JobStatusEnum.RUNNING.value,
                        Job.available_at <= func.now()
                    )
                    .values(
                        status=JobStatusEnum.PENDING.value,
                        worker_id=None,
                        lease_version=Job.lease_version + 1
                    )
                    .returning(Job.id)
                )
                result = await session.execute(stmt)
                stale_job_ids = result.scalars().all()
                if stale_job_ids:
                    await session.commit()
                    for jid in stale_job_ids:
                        logger.info("Reaper: Reclaiming stale job %s", jid)
                        await queue.enqueue(jid)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Reaper error: %s", exc)

        await asyncio.sleep(60)

async def worker_main() -> None:
    asyncio.create_task(reap_stale_jobs())
    queue = QueueService()
    while True:
        try:
            job_id_str = await queue.dequeue(timeout=5)
            if job_id_str:
                await process_job(int(job_id_str))
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(1)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(worker_main())
