import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TaskEvaluation
from app.db.repositories.observability_repository import ObservabilityRepository
from app.schemas.task import TaskResult

logger = logging.getLogger(__name__)

class TaskEvaluator:
    """Evaluates task execution deterministically based on evidence."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = ObservabilityRepository(session)

    async def evaluate_task(
        self,
        task_id: str,
        execution_id: str,
        result: TaskResult,
        job_duration_ms: int,
    ) -> TaskEvaluation:
        """Evaluate task success and quality from deterministic evidence."""
        from sqlalchemy import func, select

        from app.db.models import AuditEvent, ObservabilityEvent
        from app.observability.events import AuditEventType, EventType

        # Gather metrics from DB
        stmt_tool = select(func.count(ObservabilityEvent.id)).where(
            ObservabilityEvent.execution_id == execution_id,
            ObservabilityEvent.event_type == EventType.TOOL_CALL_FAILED.value
        )
        tool_failure_count = (await self.session.execute(stmt_tool)).scalar() or 0

        stmt_model = select(func.count(ObservabilityEvent.id)).where(
            ObservabilityEvent.execution_id == execution_id,
            ObservabilityEvent.event_type == EventType.MODEL_CALL_COMPLETED.value
        )
        model_call_count = (await self.session.execute(stmt_model)).scalar() or 0

        stmt_sec = select(func.count(AuditEvent.id)).where(
            AuditEvent.execution_id == execution_id,
            AuditEvent.event_type.in_([
                AuditEventType.AUTHORIZATION_DENIED.value,
                AuditEventType.OWNERSHIP_LOST.value,
                AuditEventType.DANGEROUS_OPERATION_BLOCKED.value
            ])
        )
        security_violation_count = (await self.session.execute(stmt_sec)).scalar() or 0
        security_violation = security_violation_count > 0

        # 1. Tests passed
        tests_passed = None
        tests_failed = None
        tests_total = None

        # 2. Plan Adherence
        plan_adherence = None
        planned_files = 0
        changed_files_count = len(result.changed_files) if result.changed_files else 0

        planned_files = len(result.plan) if result.plan else 0
        plan_adherence = None

        # 3. Regression
        regression_detected = None
        # We don't have baseline test results before coding in this implementation,
        # so we leave it as None per instructions.

        # 4. Task Success (Deterministic)
        # Criteria: Execution completed + no workflow failure + tests passed
        tests_passed_bool = result.test_result.passed if result.test_result else False
        changes_made = changed_files_count > 0
        task_success = (
            result.status.value == "success"
            and tests_passed_bool
            and changes_made
            and not security_violation
        )

        # 5. Score
        score = 0.0
        if task_success:
            score += 0.5
        if plan_adherence is not None:
            score += 0.3 * plan_adherence
        if tests_passed_bool:
            score += 0.2

        if security_violation:
            score = 0.0

        # Create evaluation
        evaluation = TaskEvaluation(
            task_id=task_id,
            execution_id=execution_id,
            overall_status=result.status.value,
            task_success=task_success,
            tests_passed=tests_passed,
            tests_failed=tests_failed,
            tests_total=tests_total,
            changes_made=changes_made,
            planned_files=planned_files,
            changed_files=changed_files_count,
            plan_adherence=plan_adherence,
            regression_detected=regression_detected,
            security_violation=security_violation,
            human_intervention_required=False,
            tool_failure_count=tool_failure_count,
            model_call_count=model_call_count,
            duration_ms=job_duration_ms,
            estimated_cost_usd=None, # Implement pricing map in future
            score=score
        )

        await self.repo.save_evaluation(evaluation)
        return evaluation
