from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.observability_repository import ObservabilityRepository
from app.db.session import get_db_session

router = APIRouter(prefix="/tasks/{task_id}", tags=["observability"])

@router.get("/timeline", summary="Get task execution timeline")
async def get_task_timeline(
    task_id: str,
    limit: int = Query(100, le=500),
    offset: int = 0,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    repo = ObservabilityRepository(session)
    events = await repo.get_timeline(task_id, limit, offset)

    events_list = []
    for e in events:
        events_list.append({
            "event_type": e.event_type,
            "component": e.component,
            "stage": e.stage,
            "started_at": e.started_at.isoformat() if e.started_at else None,
            "completed_at": e.completed_at.isoformat() if e.completed_at else None,
            "duration_ms": e.duration_ms,
            "status": e.status,
            "metadata": e.metadata_payload,
            "error_code": e.error_code,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        })

    return {
        "task_id": task_id,
        "events": events_list
    }

@router.get("/evaluation", summary="Get task evaluation")
async def get_task_evaluation(
    task_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    repo = ObservabilityRepository(session)
    evaluation = await repo.get_evaluation(task_id)
    if not evaluation:
        raise HTTPException(status_code=404, detail="Evaluation not found for task")

    return {
        "task_id": evaluation.task_id,
        "execution_id": evaluation.execution_id,
        "overall_status": evaluation.overall_status,
        "task_success": evaluation.task_success,
        "tests_passed": evaluation.tests_passed,
        "tests_failed": evaluation.tests_failed,
        "tests_total": evaluation.tests_total,
        "changes_made": evaluation.changes_made,
        "planned_files": evaluation.planned_files,
        "changed_files": evaluation.changed_files,
        "plan_adherence": evaluation.plan_adherence,
        "regression_detected": evaluation.regression_detected,
        "security_violation": evaluation.security_violation,
        "tool_failure_count": evaluation.tool_failure_count,
        "model_call_count": evaluation.model_call_count,
        "duration_ms": evaluation.duration_ms,
        "estimated_cost_usd": evaluation.estimated_cost_usd,
        "score": evaluation.score,
        "created_at": evaluation.created_at.isoformat() if evaluation.created_at else None,
    }
