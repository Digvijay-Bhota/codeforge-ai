"""Tests for GitHubExecutionService."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.execution.github_execution import GitHubExecutionService
from app.github.exceptions import GitHubNotFoundError
from app.github.git import GitError
from app.orchestration.models import WorkflowStatus
from app.schemas.task import ExecutionTarget, GitHubFailureStage, TaskRequest, TaskStatus


@pytest.fixture
def mock_orchestrator():
    with patch("app.services.task_service.Orchestrator") as mock:
        yield mock

@pytest.fixture
def mock_git_wrapper():
    with patch("app.execution.github_execution.SafeGitWrapper") as mock:
        yield mock

@pytest.fixture
def mock_github_client():
    with patch("app.execution.github_execution.GitHubClient") as mock:
        yield mock

@pytest.mark.asyncio
async def test_execute_missing_repo(mock_github_client):
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="github_repository is required"):
        TaskRequest(
            execution_target=ExecutionTarget.github,
            description="Fix the bug",
            github_repository=None
        )

@pytest.mark.asyncio
async def test_execute_invalid_repo(mock_github_client):
    service = GitHubExecutionService()
    request = TaskRequest(
        execution_target=ExecutionTarget.github,
        description="Fix the bug",
        github_repository="invalid-format"
    )
    result = await service.execute(request, "task-123")
    assert result.status == TaskStatus.error
    assert "Invalid repository format" in result.error_message
    assert result.failure_stage is None

@pytest.mark.asyncio
async def test_execute_repo_not_found(mock_github_client):
    service = GitHubExecutionService()
    client_instance = mock_github_client.return_value
    client_instance.get_repository = AsyncMock(side_effect=GitHubNotFoundError("Not Found"))

    request = TaskRequest(
        execution_target=ExecutionTarget.github,
        description="Fix the bug",
        github_repository="owner/repo"
    )
    result = await service.execute(request, "task-123")
    assert result.status == TaskStatus.error
    assert "Failed to get repository" in result.error_message
    assert result.failure_stage == GitHubFailureStage.REPOSITORY_RESOLUTION_FAILED

@pytest.mark.asyncio
async def test_execute_success(mock_github_client, mock_git_wrapper, mock_orchestrator):
    service = GitHubExecutionService()
    client_instance = mock_github_client.return_value

    # Mock GitHub API
    repo_mock = MagicMock(default_branch="main")
    client_instance.get_repository = AsyncMock(return_value=repo_mock)
    branch_mock = MagicMock(sha="123456")
    client_instance.get_branch = AsyncMock(return_value=branch_mock)
    client_instance.create_branch = AsyncMock()
    pr_mock = MagicMock(number=42, html_url="https://github.com/owner/repo/pull/42")
    client_instance.create_pull_request = AsyncMock(return_value=pr_mock)

    # Mock Git Wrapper
    git_instance = mock_git_wrapper.return_value
    git_instance.has_changes.return_value = True
    git_instance.commit_files.return_value = "new_sha"

    # Mock Orchestrator
    orchestrator_instance = service._orchestrator
    final_result_mock = MagicMock()
    final_result_mock.workflow_status = WorkflowStatus.COMPLETED
    final_result_mock.task_id = "task-123"
    final_result_mock.task_description = "Fix bug"
    final_result_mock.failure_reason = None
    final_result_mock.changed_files = ["app/main.py"]
    final_result_mock.diff = "--- a/app/main.py\n+++ b/app/main.py\n"
    final_result_mock.final_message = "Done."
    final_result_mock.implementation_plan = None
    final_result_mock.test_result = None
    orchestrator_instance.run = AsyncMock(return_value=final_result_mock)

    with patch("app.execution.github_execution.WorkspaceManager") as mock_wm:
        mock_wm_instance = mock_wm.return_value
        mock_wm_instance.get_modified_paths.return_value = ["app/main.py"]

        request = TaskRequest(
            execution_target=ExecutionTarget.github,
            description="Fix the bug",
            github_repository="owner/repo"
        )
        result = await service.execute(request, "task-123")

        assert result.status == TaskStatus.success
        assert result.github is not None
        assert result.github.repository == "owner/repo"
        assert result.github.pull_request_number == 42
        assert result.github.commit_sha == "new_sha"
        git_instance.push.assert_called_once_with("codeforge/task-task-123")

@pytest.mark.asyncio
async def test_execute_no_changes(mock_github_client, mock_git_wrapper, mock_orchestrator):
    service = GitHubExecutionService()
    client_instance = mock_github_client.return_value

    repo_mock = MagicMock(default_branch="main")
    client_instance.get_repository = AsyncMock(return_value=repo_mock)
    branch_mock = MagicMock(sha="123456")
    client_instance.get_branch = AsyncMock(return_value=branch_mock)
    client_instance.create_branch = AsyncMock()

    git_instance = mock_git_wrapper.return_value

    orchestrator_instance = service._orchestrator
    final_result_mock = MagicMock()
    final_result_mock.workflow_status = WorkflowStatus.COMPLETED
    final_result_mock.task_id = "task-123"
    final_result_mock.task_description = "Fix bug"
    final_result_mock.failure_reason = None
    final_result_mock.changed_files = []
    final_result_mock.diff = ""
    final_result_mock.final_message = "Done."
    final_result_mock.implementation_plan = None
    final_result_mock.test_result = None
    orchestrator_instance.run = AsyncMock(return_value=final_result_mock)

    with patch("app.execution.github_execution.WorkspaceManager") as mock_wm:
        mock_wm_instance = mock_wm.return_value
        mock_wm_instance.get_modified_paths.return_value = []

        request = TaskRequest(
            execution_target=ExecutionTarget.github,
            description="Fix the bug",
            github_repository="owner/repo"
        )
        result = await service.execute(request, "task-123")

        # Because of no changes, it should fail
        assert result.status == TaskStatus.failure
        assert "No changes were produced" in result.error_message
        assert result.failure_stage == GitHubFailureStage.NO_CHANGES
        assert result.github is None
        git_instance.push.assert_not_called()
        client_instance.create_pull_request.assert_not_called()

@pytest.mark.asyncio
async def test_execute_dangerous_file(mock_github_client, mock_git_wrapper, mock_orchestrator):
    service = GitHubExecutionService()
    client_instance = mock_github_client.return_value

    repo_mock = MagicMock(default_branch="main")
    client_instance.get_repository = AsyncMock(return_value=repo_mock)
    branch_mock = MagicMock(sha="123456")
    client_instance.get_branch = AsyncMock(return_value=branch_mock)
    client_instance.create_branch = AsyncMock()

    git_instance = mock_git_wrapper.return_value

    orchestrator_instance = service._orchestrator
    final_result_mock = MagicMock()
    final_result_mock.workflow_status = WorkflowStatus.COMPLETED
    final_result_mock.task_id = "task-123"
    final_result_mock.task_description = "Fix bug"
    final_result_mock.failure_reason = None
    final_result_mock.changed_files = [".env"]
    final_result_mock.diff = "--- /dev/null\n+++ b/.env\n"
    final_result_mock.final_message = "Done."
    final_result_mock.implementation_plan = None
    final_result_mock.test_result = None
    orchestrator_instance.run = AsyncMock(return_value=final_result_mock)

    with patch("app.execution.github_execution.WorkspaceManager") as mock_wm:
        mock_wm_instance = mock_wm.return_value
        mock_wm_instance.get_modified_paths.return_value = [".env"]

        request = TaskRequest(
            execution_target=ExecutionTarget.github,
            description="Fix the bug",
            github_repository="owner/repo"
        )
        result = await service.execute(request, "task-123")

        assert result.status == TaskStatus.error
        assert "Refused to commit dangerous file" in result.error_message
        assert result.failure_stage == GitHubFailureStage.COMMIT_FAILED
        git_instance.push.assert_not_called()

@pytest.mark.asyncio
async def test_execute_commit_failure_preserves_result(mock_github_client, mock_git_wrapper, mock_orchestrator):
    service = GitHubExecutionService()
    client_instance = mock_github_client.return_value

    repo_mock = MagicMock(default_branch="main")
    client_instance.get_repository = AsyncMock(return_value=repo_mock)
    branch_mock = MagicMock(sha="123456")
    client_instance.get_branch = AsyncMock(return_value=branch_mock)
    client_instance.create_branch = AsyncMock()

    git_instance = mock_git_wrapper.return_value
    git_instance.has_changes.return_value = True
    # Simulate a commit failure
    git_instance.commit_files.side_effect = GitError("Commit failed")

    orchestrator_instance = service._orchestrator
    final_result_mock = MagicMock()
    final_result_mock.workflow_status = WorkflowStatus.COMPLETED
    final_result_mock.task_id = "task-123"
    final_result_mock.task_description = "Fix bug"
    final_result_mock.failure_reason = None
    final_result_mock.changed_files = ["app/main.py"]
    final_result_mock.diff = "--- a/app/main.py\n+++ b/app/main.py\n"
    final_result_mock.final_message = "Agent worked hard"
    final_result_mock.implementation_plan = None
    final_result_mock.test_result = None
    orchestrator_instance.run = AsyncMock(return_value=final_result_mock)

    with patch("app.execution.github_execution.WorkspaceManager") as mock_wm:
        mock_wm_instance = mock_wm.return_value
        mock_wm_instance.get_modified_paths.return_value = ["app/main.py"]

        request = TaskRequest(
            execution_target=ExecutionTarget.github,
            description="Fix the bug",
            github_repository="owner/repo"
        )
        result = await service.execute(request, "task-123")

        assert result.status == TaskStatus.error
        assert result.failure_stage == GitHubFailureStage.COMMIT_FAILED
        assert result.agent_output == "Agent worked hard" # result is preserved!
        assert result.diff == "--- a/app/main.py\n+++ b/app/main.py\n"

        # Verify later stages not executed
        git_instance.push.assert_not_called()
        client_instance.create_pull_request.assert_not_called()
