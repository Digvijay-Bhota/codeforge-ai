"""Task API route — POST /api/v1/tasks.

Exposes the coding agent to callers.  The endpoint is synchronous from the
caller's perspective but internally awaits the agent's async execution.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter

from app.execution.github_execution import GitHubExecutionService
from app.schemas.task import ExecutionTarget, TaskRequest, TaskResult
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tasks"])

_task_service = TaskService()


@router.post("/tasks", response_model=TaskResult, summary="Submit a coding task")
async def create_task(request: TaskRequest) -> TaskResult:
    """Submit a coding task to the CodeForge agent.

    The agent will inspect the workspace, apply the minimal necessary code
    changes, run the test suite, and return the full result including a
    unified diff and test output.

    Returns a :class:`TaskResult` regardless of success or failure.
    Internal errors are represented as ``status: error`` in the response body.
    """
    logger.info("POST /tasks | execution_target=%s", request.execution_target)
    if request.execution_target == ExecutionTarget.github:
        _github_service = GitHubExecutionService()
        return await _github_service.execute(request, str(uuid.uuid4()))
    return await _task_service.run_task(request)
