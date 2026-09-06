import logging

from app.observability.context import get_observability_context

logger = logging.getLogger(__name__)

# Very simple in-memory metrics for phase 8 (just structured logging that could be scraped or replaced)
# To avoid cardinality explosion, we only include low-cardinality labels.

def inc_counter(name: str, value: int = 1, labels: dict[str, str] | None = None) -> None:
    """Increment a logical counter."""
    ctx = get_observability_context()
    log_extra = {
        "metric_type": "counter",
        "metric_name": name,
        "metric_value": value,
        "labels": labels or {},
        "task_id": ctx.get("task_id"),
        "job_id": ctx.get("job_id"),
        "execution_id": ctx.get("execution_id")
    }
    logger.debug(f"Metric Counter: {name} += {value}", extra=log_extra)

def record_histogram(name: str, value: float, labels: dict[str, str] | None = None) -> None:
    """Record a value in a logical histogram (e.g., duration)."""
    ctx = get_observability_context()
    log_extra = {
        "metric_type": "histogram",
        "metric_name": name,
        "metric_value": value,
        "labels": labels or {},
        "task_id": ctx.get("task_id"),
        "job_id": ctx.get("job_id"),
        "execution_id": ctx.get("execution_id")
    }
    logger.debug(f"Metric Histogram: {name} = {value}", extra=log_extra)
