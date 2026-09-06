"""GitHub execution workflow."""

import logging
import tempfile
from pathlib import Path

from app.config import settings
from app.execution.ownership import OwnershipLostError
from app.github.client import GitHubClient
from app.github.exceptions import GitHubError
from app.github.git import GitError, SafeGitWrapper
from app.github.models import CreateBranchRequest, CreatePullRequestRequest
from app.orchestration.models import FinalTaskResult, WorkflowStatus
from app.schemas.task import (
    GitHubFailureStage,
    GitHubPublicationMetadata,
    TaskRequest,
    TaskResult,
    TaskStatus,
)
from app.services.task_service import TaskService, _map_to_task_result
from app.workspace.manager import WorkspaceError, WorkspaceManager

logger = logging.getLogger(__name__)


class GitHubExecutionError(Exception):
    def __init__(self, message: str, stage: GitHubFailureStage):
        super().__init__(message)
        self.stage = stage


class GitHubExecutionService:
    """Executes a coding task against a GitHub repository."""

    def __init__(self) -> None:
        self.github_client = GitHubClient()
        self.task_service = TaskService()
        self._orchestrator = self.task_service._orchestrator

    def _truncate_output(self, text: str, max_bytes: int = 50000) -> str:
        """Safely truncate text for PR bodies or API responses."""
        if not text:
            return ""
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return truncated + "\n\n... (output truncated)"

    def _is_dangerous_file(self, path: str) -> bool:
        """Check if a modified file is potentially dangerous (e.g. secret)."""
        # Very basic check as per prompt "Do NOT build a sophisticated secret scanner yet"
        lower_path = path.lower()
        if ".env" in lower_path:
            return True
        if "id_rsa" in lower_path or "id_ed25519" in lower_path:
            return True
        if ".pem" in lower_path or ".key" in lower_path:
            return True
        if "credentials" in lower_path or "token" in lower_path:
            # We don't want to accidentally block 'test_token.py', but since it's a basic check:
            # let's only block exact matches or obvious secret extensions
            parts = Path(lower_path).parts
            if any(p in ("credentials", "tokens", ".aws", ".ssh") for p in parts):
                return True
        return False

    async def execute(self, request: TaskRequest, task_id: str) -> TaskResult:
        """Execute a task against a GitHub repository."""
        logger.info("GitHubExecutionService starting task %s", task_id)

        if not request.github_repository:
            return self._fail(task_id, request.description, "GitHub repository is required.", None)

        try:
            owner, repo = request.github_repository.split("/")
        except ValueError:
            return self._fail(task_id, request.description, "Invalid repository format. Must be 'owner/repo'.", None)

        try:
            # 1. Retrieve Repository Metadata
            try:
                gh_repo = await self.github_client.get_repository(owner, repo)
            except GitHubError as exc:
                raise GitHubExecutionError(f"Failed to get repository: {exc}", GitHubFailureStage.REPOSITORY_RESOLUTION_FAILED) from exc

            # 2. Determine base branch
            base_branch_name = request.github_base_branch or gh_repo.default_branch
            try:
                base_branch = await self.github_client.get_branch(owner, repo, base_branch_name)
            except GitHubError as exc:
                raise GitHubExecutionError(f"Failed to resolve base branch: {exc}", GitHubFailureStage.REPOSITORY_RESOLUTION_FAILED) from exc

            # 3. Create working branch (via GitHub API to avoid collisions and local push races)
            working_branch_name = f"codeforge/task-{task_id}"

            if base_branch_name == working_branch_name:
                 raise GitHubExecutionError("Base branch cannot be the CodeForge working branch.", GitHubFailureStage.BRANCH_CREATION_FAILED)

            try:
                await self.github_client.create_branch(
                    CreateBranchRequest(
                        owner=owner,
                        repo=repo,
                        branch_name=working_branch_name,
                        base_sha=base_branch.sha,
                    )
                )
            except GitHubError as exc:
                raise GitHubExecutionError(f"Failed to create branch on GitHub: {exc}", GitHubFailureStage.BRANCH_CREATION_FAILED) from exc

            # 4. Acquire Repository
            enforced_root = Path(settings.workspace_root) if settings.workspace_root else Path(tempfile.gettempdir())

            with tempfile.TemporaryDirectory(dir=enforced_root, prefix=f"task_{task_id}_") as temp_dir:
                workspace_path = Path(temp_dir).resolve()

                # Clone safely
                git_wrapper = SafeGitWrapper(workspace_path, self.github_client.token)
                logger.info("Cloning repository %s/%s to %s", owner, repo, workspace_path)
                try:
                    git_wrapper.clone(owner, repo)
                    git_wrapper.checkout_new_branch(working_branch_name, base_branch.sha)
                except GitError as exc:
                    raise GitHubExecutionError(f"Failed to acquire repository: {exc}", GitHubFailureStage.REPOSITORY_ACQUISITION_FAILED) from exc

                # Setup WorkspaceManager
                try:
                    workspace = WorkspaceManager(workspace_path, enforced_root=enforced_root)
                except WorkspaceError as exc:
                    raise GitHubExecutionError(f"Failed to initialize workspace: {exc}", GitHubFailureStage.REPOSITORY_ACQUISITION_FAILED) from exc

                # 5. Run Phase 5 Orchestrator
                try:
                    final_result: FinalTaskResult = await self._orchestrator.run(
                        workspace=workspace,
                        task_description=request.description,
                        model=settings.codeforge_model,
                    )
                except Exception as exc:
                    raise GitHubExecutionError(f"Orchestration crashed: {exc}", GitHubFailureStage.ORCHESTRATION_FAILED) from exc

                # 6. Change Validation
                if final_result.workflow_status != WorkflowStatus.COMPLETED:
                    task_res = _map_to_task_result(final_result)
                    task_res.failure_stage = GitHubFailureStage.ORCHESTRATION_FAILED
                    return task_res

                modified_paths = workspace.get_modified_paths()
                if not modified_paths:
                    final_result.workflow_status = WorkflowStatus.FAILED
                    final_result.failure_reason = "No changes were produced by the agent."
                    task_res = _map_to_task_result(final_result)
                    task_res.failure_stage = GitHubFailureStage.NO_CHANGES
                    return task_res

                # Check for dangerous files
                for path in modified_paths:
                    if self._is_dangerous_file(path):
                        final_result.workflow_status = WorkflowStatus.FAILED
                        final_result.failure_reason = f"Refused to commit dangerous file: {path}"
                        task_res = _map_to_task_result(final_result)
                        task_res.failure_stage = GitHubFailureStage.COMMIT_FAILED
                        return task_res

                # 7. Commit
                commit_message = self._truncate_output(f"CodeForge: {request.description}", 200)
                try:
                    from app.execution.ownership import verify_async_ownership
                    await verify_async_ownership()
                    commit_sha = git_wrapper.commit_files(modified_paths, commit_message)
                except GitError as exc:
                    final_result.workflow_status = WorkflowStatus.FAILED
                    final_result.failure_reason = f"Commit failed: {exc}"
                    task_res = _map_to_task_result(final_result)
                    task_res.failure_stage = GitHubFailureStage.COMMIT_FAILED
                    return task_res

                # 8. Push
                try:
                    await verify_async_ownership()
                    git_wrapper.push(working_branch_name)
                except GitError as exc:
                    final_result.workflow_status = WorkflowStatus.FAILED
                    final_result.failure_reason = f"Push failed: {exc}"
                    task_res = _map_to_task_result(final_result)
                    task_res.failure_stage = GitHubFailureStage.PUSH_FAILED
                    return task_res

                # 9. Pull Request Creation
                pr_body = (
                    f"## CodeForge AI - Implementation for Task `{task_id}`\n\n"
                    f"**Task Description:**\n{self._truncate_output(request.description, 1000)}\n\n"
                    f"**Agent Output:**\n```\n{self._truncate_output(final_result.final_message, 2000)}\n```\n"
                )
                try:
                    from app.execution.ownership import verify_async_ownership
                    await verify_async_ownership()

                    pr = await self.github_client.create_pull_request(
                        CreatePullRequestRequest(
                            owner=owner,
                            repo=repo,
                            title=commit_message,
                            body=pr_body,
                            head_branch=working_branch_name,
                            base_branch=base_branch_name,
                        )
                    )
                except GitHubError as exc:
                    final_result.workflow_status = WorkflowStatus.FAILED
                    final_result.failure_reason = f"Pull Request creation failed: {exc}"
                    task_res = _map_to_task_result(final_result)
                    task_res.failure_stage = GitHubFailureStage.PULL_REQUEST_FAILED
                    return task_res

                # 10. Success
                task_res = _map_to_task_result(final_result)
                task_res.github = GitHubPublicationMetadata(
                    repository=f"{owner}/{repo}",
                    base_branch=base_branch_name,
                    working_branch=working_branch_name,
                    commit_sha=commit_sha,
                    pull_request_number=pr.number,
                    pull_request_url=pr.html_url
                )
                return task_res

        except GitHubExecutionError as exc:
            return self._fail(task_id, request.description, str(exc), exc.stage)
        except OwnershipLostError:
            raise
        except Exception as exc:
            logger.exception("Unexpected error in GitHub workflow")
            return self._fail(task_id, request.description, f"Internal execution error: {exc}", GitHubFailureStage.ORCHESTRATION_FAILED)

    def _fail(self, task_id: str, description: str, reason: str, stage: GitHubFailureStage | None) -> TaskResult:
        """Construct a generic failed task result."""
        return TaskResult(
            task_id=task_id,
            status=TaskStatus.error,
            description=description,
            plan=[],
            changed_files=[],
            diff="",
            test_result=None,
            error_message=reason,
            agent_output="",
            failure_stage=stage
        )
