import logging
from datetime import datetime
from typing import Any  # noqa: E402

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.observability_repository import ObservabilityRepository
from app.observability.context import get_observability_context
from app.observability.events import AuditEventType, EventType

logger = logging.getLogger(__name__)

# Bounding limits
MAX_METADATA_LENGTH = 2000

def _truncate_payload(data: Any, max_length: int = MAX_METADATA_LENGTH) -> Any:
    """Recursively truncate strings in the payload to prevent giant DB rows or leaks."""
    if isinstance(data, str):
        if len(data) > max_length:
            return data[:max_length] + "... [truncated]"
        return data
    elif isinstance(data, dict):
        return {k: _truncate_payload(v, max_length) for k, v in data.items()}
    elif isinstance(data, list):
        return [_truncate_payload(item, max_length) for item in data]
    return data

async def record_event(
    session: AsyncSession | None = None,
    event_type: EventType | str = EventType.TASK_CREATED,
    component: str | None = None,
    stage: str | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    duration_ms: int | None = None,
    status: str | None = None,
    metadata: dict[str, Any] | None = None,
    error_code: str | None = None,
    parent_event_id: int | None = None,
) -> int | None:
    """Records an observability event fail-safe."""
    ctx = get_observability_context()
    task_id = ctx.get("task_id")
    job_id = ctx.get("job_id")
    execution_id = ctx.get("execution_id")
    trace_id = ctx.get("trace_id")

    if not task_id:
        logger.debug(f"Skipping telemetry {event_type} - no task_id in context")
        return None

    if session is None:
        session = ctx.get("db_session")

    safe_metadata = _truncate_payload(metadata) if metadata else None

    # Do structured logging
    log_extra = {
        "event_type": event_type.value if hasattr(event_type, "value") else str(event_type),
        "task_id": task_id,
        "job_id": job_id,
        "execution_id": execution_id,
        "trace_id": trace_id,
        "component": component,
        "stage": stage,
        "status": status,
        "duration_ms": duration_ms,
        "error_code": error_code
    }
    logger.info(f"TraceEvent: {log_extra['event_type']} task={task_id}", extra={"telemetry": log_extra})

    if session is None:
        return None

    try:
        repo = ObservabilityRepository(session)
        event = await repo.create_event(
            task_id=str(task_id),
            job_id=int(job_id) if job_id else None,
            execution_id=str(execution_id) if execution_id else None,
            trace_id=str(trace_id) if trace_id else None,
            event_type=event_type.value if hasattr(event_type, "value") else str(event_type),
            component=component,
            stage=stage,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            status=status,
            metadata_payload=safe_metadata,
            error_code=error_code,
            parent_event_id=parent_event_id,
        )
        return event.id
    except Exception as e:
        logger.warning(f"Failed to persist observability event {event_type}: {e}", exc_info=True)
        return None

async def record_audit_event(
    session: AsyncSession | None = None,
    event_type: AuditEventType | str = AuditEventType.TASK_CREATED,
    actor_type: str = "SYSTEM",
    actor_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    result: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int | None:
    ctx = get_observability_context()
    task_id = ctx.get("task_id")
    job_id = ctx.get("job_id")
    execution_id = ctx.get("execution_id")
    worker_id = ctx.get("worker_id")

    if actor_id is None and actor_type == "WORKER" and worker_id:
        actor_id = str(worker_id)

    if session is None:
        session = ctx.get("db_session")

    safe_metadata = _truncate_payload(metadata) if metadata else None

    log_extra = {
        "event_type": event_type.value if hasattr(event_type, "value") else str(event_type),
        "task_id": task_id,
        "job_id": job_id,
        "execution_id": execution_id,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "result": result
    }
    logger.info(f"AuditEvent: {log_extra['event_type']} on {resource_type}={resource_id} by {actor_type}={actor_id}", extra={"audit": log_extra})

    if session is None:
        return None

    try:
        repo = ObservabilityRepository(session)
        event = await repo.create_audit_event(
            event_type=event_type.value if hasattr(event_type, "value") else str(event_type),
            actor_type=actor_type,
            task_id=str(task_id) if task_id else None,
            execution_id=str(execution_id) if execution_id else None,
            actor_id=actor_id,
            resource_type=resource_type,
            resource_id=resource_id,
            result=result,
            metadata_payload=safe_metadata,
        )
        return event.id
    except Exception as e:
        logger.warning(f"Failed to persist audit event {event_type}: {e}", exc_info=True)
        return None
import asyncio  # noqa: E402
from typing import Any  # noqa: E402

from agents import TracingProcessor, add_trace_processor  # noqa: E402
from app.observability.events import EventType  # noqa: E402


class ModelUsageProcessor(TracingProcessor):
    def on_trace_start(self, trace): pass
    def on_trace_end(self, trace): pass
    def on_span_start(self, span): pass
    def on_span_end(self, span) -> None:
        try:
            data = getattr(span, "data", None)
            is_gen = False
            usage = None

            if isinstance(data, dict):
                is_gen = data.get("type") == "generation"
                usage = data.get("usage")
            elif data:
                is_gen = getattr(data, "type", None) == "generation"
                usage = getattr(data, "usage", None)

            if is_gen:
                from app.observability.tracing import record_event
                duration_ms = None
                if hasattr(span, "duration"):
                    duration_ms = int(span.duration * 1000)
                meta = {}
                if usage:
                    if isinstance(usage, dict):
                        meta = {
                            "input_tokens": usage.get("input_tokens", 0),
                            "output_tokens": usage.get("output_tokens", 0),
                            "total_tokens": usage.get("total_tokens", 0),
                        }
                    else:
                        meta = {
                            "input_tokens": getattr(usage, "input_tokens", 0),
                            "output_tokens": getattr(usage, "output_tokens", 0),
                            "total_tokens": getattr(usage, "total_tokens", 0),
                        }
                loop = asyncio.get_running_loop()
                loop.create_task(record_event(None, EventType.MODEL_CALL_COMPLETED, component="model", duration_ms=duration_ms, metadata=meta))
        except RuntimeError:
            pass
        except Exception as exc:
            logger.warning(f"ModelUsageProcessor failed: {exc}")


    def shutdown(self): pass
    def force_flush(self): pass

add_trace_processor(ModelUsageProcessor())
