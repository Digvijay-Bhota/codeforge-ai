import contextvars
import uuid
from typing import Any

# Context Variables
_task_id = contextvars.ContextVar[str | None]("task_id", default=None)
_job_id = contextvars.ContextVar[int | None]("job_id", default=None)
_execution_id = contextvars.ContextVar[str | None]("execution_id", default=None)
_worker_id = contextvars.ContextVar[str | None]("worker_id", default=None)
_trace_id = contextvars.ContextVar[str | None]("trace_id", default=None)
_db_session = contextvars.ContextVar[Any | None]("db_session", default=None)

def set_observability_context(
    task_id: str | None = None,
    job_id: int | None = None,
    execution_id: str | None = None,
    worker_id: str | None = None,
    trace_id: str | None = None,
    db_session: Any | None = None,
) -> dict[str, contextvars.Token]:
    """Set the observability context and return tokens for cleanup."""
    tokens: dict[str, contextvars.Token[Any]] = {}
    if task_id is not None:
        tokens["task_id"] = _task_id.set(task_id)
    if job_id is not None:
        tokens["job_id"] = _job_id.set(job_id)
    if execution_id is not None:
        tokens["execution_id"] = _execution_id.set(execution_id)
    if worker_id is not None:
        tokens["worker_id"] = _worker_id.set(worker_id)
    if trace_id is not None:
        tokens["trace_id"] = _trace_id.set(trace_id)
    elif execution_id is not None:
        tokens["trace_id"] = _trace_id.set(str(uuid.uuid4()))
    if db_session is not None:
        tokens["db_session"] = _db_session.set(db_session)
    return tokens

def reset_observability_context(tokens: dict[str, contextvars.Token]) -> None:
    """Reset the observability context using tokens."""
    if "task_id" in tokens:
        _task_id.reset(tokens["task_id"])
    if "job_id" in tokens:
        _job_id.reset(tokens["job_id"])
    if "execution_id" in tokens:
        _execution_id.reset(tokens["execution_id"])
    if "worker_id" in tokens:
        _worker_id.reset(tokens["worker_id"])
    if "trace_id" in tokens:
        _trace_id.reset(tokens["trace_id"])
    if "db_session" in tokens:
        _db_session.reset(tokens["db_session"])

def get_observability_context() -> dict[str, Any]:
    """Get the current observability context."""
    return {
        "task_id": _task_id.get(),
        "job_id": _job_id.get(),
        "execution_id": _execution_id.get(),
        "worker_id": _worker_id.get(),
        "trace_id": _trace_id.get(),
        "db_session": _db_session.get(),
    }
