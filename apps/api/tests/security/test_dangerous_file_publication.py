"""Phase 10B.5 Stage 0 — workflow files must never be published.

A change under ``.github/workflows/`` is executed by GitHub Actions with the
repository's secrets as soon as the branch is pushed (``on: push``) or a PR is
opened (``on: pull_request``).  The publication gate must stop such changes
before commit, push or PR creation.

CF-05 widens the filter to everything under ``.github/`` (composite actions,
CODEOWNERS, dependabot, ...).  The filter is defense in depth only: CI also
runs ordinary repository code, so publication additionally requires a human
approval (tests/security/test_publication_approval.py).
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

# Composite and local actions run inside workflows with the same secrets.
ACTION_PATHS = [
    ".github/actions/setup/action.yml",
    ".github/actions/setup/action.yaml",
    ".github/actions/build/run.sh",
    ".GITHUB/actions/x/action.yml",
    "./.github/actions/x/action.yml",
    "src/../.github/actions/x/index.js",
]

# Every other file under .github configures repository automation or review.
OTHER_GITHUB_PATHS = [
    ".github/CODEOWNERS",
    ".github/dependabot.yml",
    ".github/ISSUE_TEMPLATE/bug.md",
    ".github/scripts/release.sh",
    ".github",
    "sub/project/.github/workflows/ci.yml",
]

# Normalization tricks that must not slip a .github path past the filter.
NORMALIZATION_BYPASS_PATHS = [
    ".github\\workflows\\ci.yml",
    "a/b/../../.github/workflows/ci.yml",
    "./././.github/actions/x/action.yml",
    "//.github/CODEOWNERS",
    ".github/./workflows/ci.yml",
    "x/./../.GitHub/dependabot.yml",
]

SAFE_PATHS = [
    "docs/workflows/ci.yml",
    "src/github/workflows.py",
    "workflows/.github.yml",
    "github/workflows/ci.yml",
    ".githubx/workflows/ci.yml",
    "docs/.github-notes.md",
]


@pytest.mark.parametrize("path", WORKFLOW_PATHS)
def test_workflow_paths_are_dangerous(path: str) -> None:
    assert GitHubExecutionService._is_dangerous_file(path) is True


@pytest.mark.parametrize("path", ACTION_PATHS)
def test_action_paths_are_dangerous(path: str) -> None:
    assert GitHubExecutionService._is_dangerous_file(path) is True


@pytest.mark.parametrize("path", OTHER_GITHUB_PATHS)
def test_other_github_paths_are_dangerous(path: str) -> None:
    assert GitHubExecutionService._is_dangerous_file(path) is True


@pytest.mark.parametrize("path", NORMALIZATION_BYPASS_PATHS)
def test_normalization_cannot_bypass_github_filter(path: str) -> None:
    assert GitHubExecutionService._is_dangerous_file(path) is True


@pytest.mark.parametrize("path", SAFE_PATHS)
def test_paths_outside_github_are_not_dangerous(path: str) -> None:
    assert GitHubExecutionService._is_dangerous_file(path) is False


@pytest.mark.parametrize(
    "path",
    [".env", ".env.local", "config/id_rsa", "keys/id_ed25519", "certs/server.pem", "certs/server.key",
     ".aws/credentials", "config/credentials/prod.json"],
)
def test_existing_secret_patterns_still_dangerous(path: str) -> None:
    assert GitHubExecutionService._is_dangerous_file(path) is True


@pytest.mark.parametrize(
    "modified",
    [
        [".github/workflows/ci.yml"],
        ["src/app.py", ".github/workflows/deploy.yml"],
        ["src/app.py", "docs/../.github/workflows/ci.yml"],
        ["src/app.py", ".github/actions/setup/action.yml"],
        [".github/CODEOWNERS"],
    ],
)
@pytest.mark.asyncio
async def test_workflow_change_blocks_commit_push_and_pr(modified: list[str], monkeypatch) -> None:
    from tests.conftest import FakeInstallationTokens, repository_access_for

    tokens = FakeInstallationTokens()
    monkeypatch.setattr("app.execution.github_execution.get_installation_token_service", lambda: tokens)
    with (
        patch("app.execution.github_execution.GitHubClient") as client_cls,
        patch("app.execution.github_execution.SafeGitWrapper") as git_cls,
        patch("app.execution.github_execution.WorkspaceManager") as wm_cls,
        patch("app.services.task_service.Orchestrator"),
    ):
        client = client_cls.return_value
        client_cls.for_installation.return_value = client
        client.aclose = AsyncMock()
        client.get_repository = AsyncMock(return_value=MagicMock(default_branch="main"))
        client.get_branch = AsyncMock(return_value=MagicMock(sha="abc123"))
        client.create_branch = AsyncMock()
        client.create_pull_request = AsyncMock()

        service = GitHubExecutionService(repository_access_for("owner/repo"))
        final = MagicMock(
            workflow_status=WorkflowStatus.COMPLETED,
            task_id="task-wf",
            task_description="Fix bug",
            failure_reason=None,
            failure_code=None,
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
    assert ".github/" in (result.error_message or "")
    assert result.github is None
    assert result.publication_artifact is None
    client.create_branch.assert_not_awaited()
    assert tokens.write_scopes() == []  # no write credential exists during generation
    git.commit_files.assert_not_called()
    git.push.assert_not_called()
    client.create_pull_request.assert_not_awaited()
