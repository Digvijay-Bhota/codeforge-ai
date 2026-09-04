"""Task Service — orchestrates the full coding-agent lifecycle.

The service:
1. Validates and initialises the workspace.
2. Builds the coding agent.
3. Runs the agent and collects its output.
4. Runs the test suite to determine final pass/fail status.
5. Returns a :class:`~app.schemas.task.TaskResult`.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from agents import Runner
from app.agents.coding_agent import make_coding_agent
from app.config import settings
from app.schemas.task import (
    ChangedFile,
    PlanStep,
    TaskRequest,
    TaskResult,
    TaskStatus,
)
from app.workspace.manager import WorkspaceError, WorkspaceManager
from app.workspace.runner import TestRunner

logger = logging.getLogger(__name__)


class TaskService:
    """Runs coding tasks end-to-end using the CodeForge coding agent."""

    def __init__(self) -> None:
        self._test_runner = TestRunner()

    async def run_task(self, request: TaskRequest) -> TaskResult:
        """Execute *request* and return the full :class:`TaskResult`."""
        task_id = str(uuid.uuid4())
        logger.info(
            "Task %s started | workspace=%s | description=%.80s",
            task_id,
            request.workspace_path,
            request.description,
        )

        workspace_root = Path(request.workspace_path).resolve()

        # ── 1. Initialise workspace ──────────────────────────────────────────
        try:
            workspace = WorkspaceManager(workspace_root)
        except WorkspaceError as exc:
            logger.error("Task %s: workspace error — %s", task_id, exc)
            return TaskResult(
                task_id=task_id,
                status=TaskStatus.error,
                description=request.description,
                plan=[],
                changed_files=[],
                diff="",
                test_result=None,
                error_message=str(exc),
                agent_output="",
            )

        # ── 2. Build and run agent ───────────────────────────────────────────
        agent = make_coding_agent(
            workspace=workspace,
            runner=self._test_runner,
            model=settings.codeforge_model,
        )

        logger.info("Task %s: agent starting", task_id)
        try:
            run_result = await Runner.run(agent, request.description)
            agent_output: str = run_result.final_output or ""
        except Exception as exc:  # noqa: BLE001
            logger.exception("Task %s: agent raised %s", task_id, type(exc).__name__)
            return TaskResult(
                task_id=task_id,
                status=TaskStatus.error,
                description=request.description,
                plan=[],
                changed_files=[],
                diff="",
                test_result=None,
                error_message=f"Agent error: {exc}",
                agent_output="",
            )

        # ── 3. Collect workspace changes ─────────────────────────────────────
        modified_paths = workspace.get_modified_paths()
        diff = workspace.generate_diff()
        changed_files = [
            ChangedFile(path=p, action=workspace._get_action(p)) for p in modified_paths
        ]
        logger.info("Task %s: %d file(s) modified", task_id, len(changed_files))

        # ── 4. Final test run ────────────────────────────────────────────────
        test_result = None
        if modified_paths:
            logger.info("Task %s: running final test suite", task_id)
            test_result = self._test_runner.run(workspace_root, request.test_command)

        # ── 5. Determine status ──────────────────────────────────────────────
        if test_result is None:
            status = TaskStatus.success
        elif test_result.passed:
            status = TaskStatus.success
        else:
            status = TaskStatus.failure

        logger.info("Task %s completed | status=%s", task_id, status)

        return TaskResult(
            task_id=task_id,
            status=status,
            description=request.description,
            plan=[PlanStep(step=1, description="Agent executed task — see agent_output for details")],
            changed_files=changed_files,
            diff=diff,
            test_result=test_result,
            error_message=None,
            agent_output=agent_output,
        )
