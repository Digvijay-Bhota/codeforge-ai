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
from app.repository.context import build_repository_context, format_context_for_prompt
from app.schemas.task import (
    ChangedFile,
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
            enforced_root = Path(settings.workspace_root) if settings.workspace_root else None
            workspace = WorkspaceManager(workspace_root, enforced_root=enforced_root)
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
            repo_context = build_repository_context(workspace, request.description)
            repo_context_str = format_context_for_prompt(repo_context)
        except Exception as exc:
            logger.warning("Task %s: failed to build repository context: %s", task_id, exc)
            repo_context_str = f"Workspace path: {workspace.root}"

        prompt = (
            f"{repo_context_str}\n\n"
            f"Task description:\n{request.description}"
        )

        try:
            from app.agents.coding_agent import AgentFinalOutput
            run_result = await Runner.run(agent, prompt)

            if isinstance(run_result.final_output, AgentFinalOutput):
                agent_output = run_result.final_output.message
                plan = run_result.final_output.plan
            else:
                agent_output = str(run_result.final_output)
                plan = []

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
        if not modified_paths:
            logger.info("Task %s completed with no changes", task_id)
            return TaskResult(
                task_id=task_id,
                status=TaskStatus.failure,
                description=request.description,
                plan=plan,
                changed_files=[],
                diff="",
                test_result=None,
                error_message="The agent completed without making any changes to the workspace.",
                agent_output=agent_output,
            )

        logger.info("Task %s: running final test suite", task_id)
        test_result = self._test_runner.run(workspace_root)

        # ── 5. Determine status ──────────────────────────────────────────────
        if test_result.passed:
            status = TaskStatus.success
        else:
            status = TaskStatus.failure

        logger.info("Task %s completed | status=%s", task_id, status)

        return TaskResult(
            task_id=task_id,
            status=status,
            description=request.description,
            plan=plan,
            changed_files=changed_files,
            diff=diff,
            test_result=test_result,
            error_message=None,
            agent_output=agent_output,
        )
