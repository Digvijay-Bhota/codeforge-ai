import logging
from typing import Any

from sqlalchemy.sql import func

from app.db.models import Task, TaskEvent, TaskStatusEnum
from app.db.repositories.task_repository import TaskRepository

logger = logging.getLogger(__name__)

# Valid transitions
VALID_TRANSITIONS = {
    TaskStatusEnum.PENDING.value: {TaskStatusEnum.QUEUED.value, TaskStatusEnum.FAILED.value},
    TaskStatusEnum.QUEUED.value: {TaskStatusEnum.ANALYZING.value, TaskStatusEnum.FAILED.value},
    TaskStatusEnum.ANALYZING.value: {TaskStatusEnum.PLANNING.value, TaskStatusEnum.FAILED.value},
    TaskStatusEnum.PLANNING.value: {TaskStatusEnum.CODING.value, TaskStatusEnum.FAILED.value},
    TaskStatusEnum.CODING.value: {TaskStatusEnum.TESTING.value, TaskStatusEnum.FAILED.value},
    TaskStatusEnum.TESTING.value: {TaskStatusEnum.COMPLETED.value, TaskStatusEnum.FAILED.value},
    TaskStatusEnum.COMPLETED.value: set(), # Terminal
    TaskStatusEnum.FAILED.value: set(),    # Terminal
}

class InvalidTaskTransitionError(Exception):
    pass

class TaskStateMachine:
    def __init__(self, task_repo: TaskRepository):
        self.repo = task_repo

    async def transition(
        self,
        task: Task,
        to_status: TaskStatusEnum,
        message: str | None = None,
        metadata: dict[str, Any] | None = None
    ) -> Task:
        from_status = task.status

        # Validation
        if to_status.value not in VALID_TRANSITIONS.get(from_status, set()):
            raise InvalidTaskTransitionError(f"Cannot transition task {task.task_id} from {from_status} to {to_status.value}")

        logger.info("Task %s transition: %s -> %s", task.task_id, from_status, to_status.value)

        # Update Task Status
        task.status = to_status.value

        # Timestamps
        if to_status == TaskStatusEnum.ANALYZING:
            task.started_at = func.now()
        elif to_status in (TaskStatusEnum.COMPLETED, TaskStatusEnum.FAILED):
            task.completed_at = func.now()

        import json

        from app.orchestration.stages import _bound_text

        safe_message = _bound_text(message) if message else None

        safe_metadata = None
        if metadata:
            meta_str = json.dumps(metadata)
            if len(meta_str) > 10000:
                safe_metadata = {"_truncated": True, "error": "metadata exceeded 10KB limit"}
            else:
                safe_metadata = metadata

        # Emit Event
        event = TaskEvent(
            task_id=task.task_id,
            event_type=f"TASK_{to_status.value}",
            from_status=from_status,
            to_status=to_status.value,
            message=safe_message,
            metadata_obj=safe_metadata
        )
        await self.repo.add_event(event)

        return await self.repo.update_task(task)
