from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditEvent, ObservabilityEvent, TaskEvaluation


class ObservabilityRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_event(
        self,
        task_id: str,
        job_id: int | None,
        execution_id: str | None,
        trace_id: str | None,
        event_type: str,
        component: str | None = None,
        stage: str | None = None,
        started_at: Any | None = None,
        completed_at: Any | None = None,
        duration_ms: int | None = None,
        status: str | None = None,
        metadata_payload: dict[str, Any] | None = None,
        error_code: str | None = None,
        parent_event_id: int | None = None,
    ) -> ObservabilityEvent:
        event = ObservabilityEvent(
            task_id=task_id,
            job_id=job_id,
            execution_id=execution_id,
            trace_id=trace_id,
            event_type=event_type,
            component=component,
            stage=stage,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            status=status,
            metadata_payload=metadata_payload,
            error_code=error_code,
            parent_event_id=parent_event_id,
        )
        self.session.add(event)
        # Flush to get the ID without committing the transaction fully if we don't want to
        await self.session.flush()
        return event

    async def create_audit_event(
        self,
        event_type: str,
        actor_type: str,
        task_id: str | None = None,
        execution_id: str | None = None,
        actor_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        result: str | None = None,
        metadata_payload: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            event_type=event_type,
            actor_type=actor_type,
            task_id=task_id,
            execution_id=execution_id,
            actor_id=actor_id,
            resource_type=resource_type,
            resource_id=resource_id,
            result=result,
            metadata_payload=metadata_payload,
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def save_evaluation(self, evaluation: TaskEvaluation) -> TaskEvaluation:
        self.session.add(evaluation)
        await self.session.flush()
        return evaluation

    async def get_timeline(self, task_id: str, limit: int = 100, offset: int = 0) -> list[ObservabilityEvent]:
        stmt = (
            select(ObservabilityEvent)
            .where(ObservabilityEvent.task_id == task_id)
            .order_by(ObservabilityEvent.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_evaluation(self, task_id: str) -> TaskEvaluation | None:
        stmt = (
            select(TaskEvaluation)
            .where(TaskEvaluation.task_id == task_id)
            .order_by(TaskEvaluation.created_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()
