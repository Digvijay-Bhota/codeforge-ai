"""Phase 10B.5 Stage 0 — workflow files must never be published.

A change under ``.github/workflows/`` is executed by GitHub Actions with the
repository's secrets as soon as the branch is pushed (``on: push``) or a PR is
opened (``on: pull_request``).  The publication gate must stop such changes
before commit, push or PR creation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.execution.github_execution import GitHubExecutionService
from app.orchestration.models import WorkflowStatus
from app.schemas.task import ExecutionTarget, GitHubFailureStage, TaskRequest, TaskStatus

WORKFLOW_PATHS = [
    ".github/workflows/ci.yml",
    ".github/workflows/release.yaml",
    ".github/workflows/nested/dir/x.yml",
    ".github/workflows/README.md",
    "./.github/workflows/ci.yml",
    "/.github/workflows/ci.yml",
    "docs/../.github/workflows/ci.yml",
    ".github//workflows/ci.yml",
    ".GitHub/Workflows/CI.yml",
]

SAFE_PATHS = [
    ".github/CODEOWNERS",
    ".github/dependabot.yml",
    ".github/ISSUE_TEMPLATE/bug.md",
    "docs/workflows/ci.yml",
    "src/github/workflows.py",
    "workflows/.github.yml",
]


@pytest.mark.parametrize("path", WORKFLOW_PATHS)
def test_workflow_paths_are_dangerous(path: str) -> None:
    assert GitHubExecutionService._is_dangerous_file(path) is True


@pytest.mark.parametrize("path", SAFE_PATHS)
def test_non_workflow_github_paths_are_not_dangerous(path: str) -> None:
    assert GitHubExecutionService._is_dangerous_file(path) is False


@pytest.mark.parametrize("path", [".env", ".env.local", "config/id_rsa", "certs/server.pem", ".aws/credentials"])
def test_existing_secret_patterns_still_dangerous(path: str) -> None:
    assert GitHubExecutionService._is_dangerous_file(path) is True


@pytest.mark.parametrize(
    "modified",
    [
        [".github/workflows/ci.yml"],
        ["src/app.py", ".github/workflows/deploy.yml"],
        ["src/app.py", "docs/../.github/workflows/ci.yml"],
    ],
)
@pytest.mark.asyncio
async def test_workflow_change_blocks_commit_push_and_pr(modified: list[str]) -> None:
    with (
        patch("app.execution.github_execution.GitHubClient") as client_cls,
        patch("app.execution.github_execution.SafeGitWrapper") as git_cls,
        patch("app.execution.github_execution.WorkspaceManager") as wm_cls,
        patch("app.services.task_service.Orchestrator"),
    ):
        client = client_cls.return_value
        client.get_repository = AsyncMock(return_value=MagicMock(default_branch="main"))
        client.get_branch = AsyncMock(return_value=MagicMock(sha="abc123"))
        client.create_branch = AsyncMock()
        client.create_pull_request = AsyncMock()

        service = GitHubExecutionService()
        final = MagicMock(
            workflow_status=WorkflowStatus.COMPLETED,
            task_id="task-wf",
            task_description="Fix bug",
            failure_reason=None,
            changed_files=modified,
            diff="",
            final_message="Done.",
            implementation_plan=None,
            test_result=None,
            repair_attempts_used=0,
        )
        service._orchestrator.run = AsyncMock(return_value=final)
        wm_cls.return_value.get_modified_paths.return_value = modified

        result = await service.execute(
            TaskRequest(
                execution_target=ExecutionTarget.github,
                github_repository="owner/repo",
                description="Fix the bug in CI",
            ),
            "task-wf",
        )

    git = git_cls.return_value
    assert result.status == TaskStatus.error
    assert result.failure_stage == GitHubFailureStage.COMMIT_FAILED
    assert ".github/workflows" in (result.error_message or "")
    assert result.github is None
    git.commit_files.assert_not_called()
    git.push.assert_not_called()
    client.create_pull_request.assert_not_awaited()
