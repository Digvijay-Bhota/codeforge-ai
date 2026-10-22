"""GitHub execution workflow."""

import logging
import posixpath
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.config import settings
from app.execution.local_policy import LocalExecutionError, github_clone_root
from app.execution.ownership import OwnershipLostError
from app.execution.publication import (
    PublicationError,
    apply_artifact,
    capture_artifact,
    digest_of_workspace,
    verify_artifact,
)
from app.github.client import GitHubClient
from app.github.exceptions import GitHubError
from app.github.git import GitError, SafeGitWrapper
from app.github.models import CreateBranchRequest, CreatePullRequestRequest
from app.github.repository_access import (
    GENERATION_PERMISSIONS,
    PUBLICATION_PERMISSIONS,
    RepositoryAccess,
)
from app.github.token_service import get_installation_token_service
from app.orchestration import failures
from app.orchestration.models import WorkflowStatus
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
    def __init__(self, message: str, stage: GitHubFailureStage, *, retryable: bool | None = None):
        super().__init__(message)
        self.stage = stage
        # None: decided by stage.  Set for failures whose stage does not say.
        self.retryable = retryable


class GitHubExecutionService:
    """Executes a coding task against one GitHub repository.

    Credentials come from *access* (CF-06): every API client and Git token is
    a GitHub App installation token limited to that repository.  Generation
    only ever holds a read-only token; publish() obtains a write token, and
    only after the CF-05 authorization re-check, right before its first
    remote change.
    """

    def __init__(self, access: RepositoryAccess) -> None:
        self.access = access
        self.task_service = TaskService()
        self._orchestrator = self.task_service._orchestrator

    def _client(self, permissions: dict[str, str]) -> GitHubClient:
        return GitHubClient.for_installation(
            self.access.installation_id, scope=self.access.scope(permissions)
        )

    async def _git_token(self, permissions: dict[str, str]) -> str:
        """A repository-scoped token for Git over HTTPS; GitHubExecutionError if none."""
        try:
            return await get_installation_token_service().get_token(
                self.access.installation_id, self.access.scope(permissions)
            )
        except (GitHubError, ValueError) as exc:
            raise GitHubExecutionError(
                f"Could not obtain a repository-scoped GitHub credential: {exc}",
                GitHubFailureStage.CREDENTIALS_FAILED,
                retryable=bool(getattr(exc, "retryable", False)),
            ) from None

    def _truncate_output(self, text: str, max_bytes: int = 50000) -> str:
        """Safely truncate text for PR bodies or API responses."""
        if not text:
            return ""
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return truncated + "\n\n... (output truncated)"

    @staticmethod
    def _is_dangerous_file(path: str) -> bool:
        """Check if a modified file must not be published (secrets, CI workflows)."""
        # Very basic check as per prompt "Do NOT build a sophisticated secret scanner yet"
        lower_path = posixpath.normpath("/" + path.replace("\\", "/").lower()).lstrip("/")
        # Anything under .github (workflows, composite actions, CODEOWNERS,
        # dependabot ...) configures the target repository's automation.  This is
        # defense in depth only: CI also runs ordinary code (tests, scripts), which
        # is why publication itself requires human approval (CF-05).
        if ".github" in lower_path.split("/"):
            return True
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

    async def execute(
        self,
        request: TaskRequest,
        task_id: str,
        initial_plan: Any | None = None,
        stop_after_plan: bool = False,
    ) -> TaskResult:
        """Execute a task against a GitHub repository."""
        logger.info("GitHubExecutionService starting task %s (stop_after_plan=%s)", task_id, stop_after_plan)

        if not request.github_repository:
            return self._fail(task_id, request.description, "GitHub repository is required.", None)

        try:
            owner, repo = request.github_repository.split("/")
        except ValueError:
            return self._fail(task_id, request.description, "Invalid repository format. Must be 'owner/repo'.", None)
        if request.github_repository.lower() != self.access.full_name.lower():
            return self._fail(
                task_id, request.description, "Repository credentials do not match the task's repository.",
                GitHubFailureStage.CREDENTIALS_FAILED,
            )

        # Generation is read-only: one read client and one read token, nothing else.
        read_client = self._client(GENERATION_PERMISSIONS)
        try:
            return await self._execute(request, task_id, owner, repo, read_client, initial_plan, stop_after_plan)
        finally:
            await read_client.aclose()

    async def _execute(
        self,
        request: TaskRequest,
        task_id: str,
        owner: str,
        repo: str,
        read_client: GitHubClient,
        initial_plan: Any | None,
        stop_after_plan: bool,
    ) -> TaskResult:
        try:
            # Clones live outside WORKSPACE_ROOT, so local tasks cannot reach them.
            # Checked before anything happens on GitHub.
            try:
                enforced_root = github_clone_root()
            except LocalExecutionError as exc:
                raise GitHubExecutionError(
                    f"GitHub clone directory is misconfigured: {exc}",
                    GitHubFailureStage.REPOSITORY_ACQUISITION_FAILED,
                ) from exc

            # 1. Retrieve Repository Metadata
            try:
                gh_repo = await read_client.get_repository(owner, repo)
            except GitHubError as exc:
                raise GitHubExecutionError(
                    f"Failed to get repository: {exc}", GitHubFailureStage.REPOSITORY_RESOLUTION_FAILED,
                    retryable=exc.retryable,
                ) from exc

            # 2. Determine base branch
            base_branch_name = request.github_base_branch or gh_repo.default_branch
            try:
                base_branch = await read_client.get_branch(owner, repo, base_branch_name)
            except GitHubError as exc:
                raise GitHubExecutionError(
                    f"Failed to resolve base branch: {exc}", GitHubFailureStage.REPOSITORY_RESOLUTION_FAILED,
                    retryable=exc.retryable,
                ) from exc

            if stop_after_plan:
                # In plan-only mode, clone and analyze/plan without modifying remote branch or opening PR
                read_token = await self._git_token(GENERATION_PERMISSIONS)
                with tempfile.TemporaryDirectory(dir=enforced_root, prefix=f"task_{task_id}_") as temp_dir:
                    workspace_path = Path(temp_dir).resolve()
                    git_wrapper = SafeGitWrapper(workspace_path, read_token)
                    try:
                        git_wrapper.clone(owner, repo)
                    except GitError as exc:
                        raise GitHubExecutionError(f"Failed to acquire repository: {exc}", GitHubFailureStage.REPOSITORY_ACQUISITION_FAILED) from exc

                    try:
                        workspace = WorkspaceManager(workspace_path, enforced_root=enforced_root)
                    except WorkspaceError as exc:
                        raise GitHubExecutionError(f"Failed to initialize workspace: {exc}", GitHubFailureStage.REPOSITORY_ACQUISITION_FAILED) from exc

                    final_result = await self._orchestrator.run(
                        workspace=workspace,
                        task_description=request.description,
                        model=settings.codeforge_model,
                        stop_after_plan=True,
                    )
                    return _map_to_task_result(final_result, task_id)

            # 3. Generation never touches the remote repository: no branch, push
            # or pull request until a human approves the exact changes (CF-05;
            # see app/execution/publication.py and publish() below).
            working_branch_name = f"codeforge/task-{task_id}"

            if base_branch_name == working_branch_name:
                 raise GitHubExecutionError("Base branch cannot be the CodeForge working branch.", GitHubFailureStage.BRANCH_CREATION_FAILED)

            # 4. Acquire Repository

            read_token = await self._git_token(GENERATION_PERMISSIONS)
            with tempfile.TemporaryDirectory(dir=enforced_root, prefix=f"task_{task_id}_") as temp_dir:
                workspace_path = Path(temp_dir).resolve()

                # Clone safely, with the read-only token
                git_wrapper = SafeGitWrapper(workspace_path, read_token)
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
                    final_result = await self._orchestrator.run(
                        workspace=workspace,
                        task_description=request.description,
                        model=settings.codeforge_model,
                        initial_plan=initial_plan,
                    )
                except OwnershipLostError:
                    raise
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

                # 7. Capture the exact changes, and the text they would be
                # published with, for human review.  Nothing is committed,
                # pushed or opened until publication is approved.
                commit_message = self._truncate_output(f"CodeForge: {request.description}", 200)
                pr_body = (
                    f"## CodeForge AI - Implementation for Task `{task_id}`\n\n"
                    f"**Task Description:**\n{self._truncate_output(request.description, 1000)}\n\n"
                    f"**Agent Output:**\n```\n{self._truncate_output(final_result.final_message, 2000)}\n```\n"
                )
                notice = final_result.test_result.snapshot_notice if final_result.test_result else None
                if notice:
                    pr_body += (
                        "\n**Test coverage warning:** the tests ran on an incomplete snapshot of "
                        "the repository.\n"
                        f"```\n{self._truncate_output(notice, 2000)}\n```\n"
                    )
                try:
                    artifact = capture_artifact(
                        workspace,
                        repository=f"{owner}/{repo}",
                        base_branch=base_branch_name,
                        base_sha=base_branch.sha,
                        commit_message=commit_message,
                        pull_request_title=commit_message,
                        pull_request_body=pr_body,
                    )
                except (PublicationError, WorkspaceError) as exc:
                    final_result.workflow_status = WorkflowStatus.FAILED
                    final_result.failure_reason = f"Changes cannot be prepared for review: {exc}"
                    task_res = _map_to_task_result(final_result)
                    task_res.failure_stage = GitHubFailureStage.COMMIT_FAILED
                    return task_res

                task_res = _map_to_task_result(final_result)
                task_res.publication_artifact = artifact
                return task_res

        except GitHubExecutionError as exc:
            result = self._fail(task_id, request.description, str(exc), exc.stage)
            if exc.retryable:
                # A GitHub outage or rate limit, not a missing repository or credential.
                result.failure_code = failures.GITHUB_UNAVAILABLE
            return result
        except OwnershipLostError:
            raise
        except Exception as exc:
            logger.exception("Unexpected error in GitHub workflow")
            return self._fail(task_id, request.description, f"Internal execution error: {exc}", GitHubFailureStage.ORCHESTRATION_FAILED)

    async def publish(
        self,
        task_id: str,
        artifact: dict[str, Any],
        approved_by: str,
        *,
        authorize: Callable[[], Awaitable[None]],
    ) -> GitHubPublicationMetadata:
        """Publish an approved artifact: branch, deterministic commit, push, pull request.

        Every check is repeated here, at the boundary: the stored artifact must
        match its approved digest, no path may be dangerous, and the files
        written onto the base commit must reproduce exactly that digest.  The
        commit message and pull request text are the artifact's, as reviewed.

        *authorize* re-checks, from server-side state, that the approver may
        still publish; it raises PublicationUnauthorizedError otherwise.  It
        runs immediately before each remote side effect: creating the branch,
        pushing, and opening the pull request.  This narrows the window for a
        revocation to slip through; it cannot make authorization atomic, since
        GitHub permissions can change right after any check.

        Credentials (CF-06): the clone uses a read-only token.  The write
        token (contents and pull requests, this repository only) is obtained
        after the first authorization re-check, immediately before the
        branch is created, and is the only credential that can change the
        repository.

        Idempotent: a retry recreates the same commit (fixed dates), pushes it
        as a no-op, and reuses an already-open pull request.  Raises
        GitHubExecutionError or PublicationUnauthorizedError; nothing is
        pushed if any check fails.
        """
        repository = artifact.get("repository")
        if not isinstance(repository, str) or repository.lower() != self.access.full_name.lower():
            raise GitHubExecutionError(
                "Repository credentials do not match the artifact's repository.",
                GitHubFailureStage.CREDENTIALS_FAILED,
                retryable=False,
            )
        base_sha = artifact["base_sha"]
        files = artifact["files"]
        digest = artifact["digest"]
        try:
            owner, repo = repository.split("/")
        except ValueError:
            raise GitHubExecutionError("Invalid repository in artifact.", GitHubFailureStage.REPOSITORY_RESOLUTION_FAILED) from None
        try:
            verify_artifact(artifact, digest)
        except PublicationError as exc:
            raise GitHubExecutionError(str(exc), GitHubFailureStage.COMMIT_FAILED) from None
        for entry in files:
            if self._is_dangerous_file(entry["path"]):
                raise GitHubExecutionError(
                    f"Refused to publish dangerous file: {entry['path']}", GitHubFailureStage.COMMIT_FAILED
                )

        working_branch_name = f"codeforge/task-{task_id}"
        try:
            enforced_root = github_clone_root()
        except LocalExecutionError as exc:
            raise GitHubExecutionError(
                f"GitHub clone directory is misconfigured: {exc}",
                GitHubFailureStage.REPOSITORY_ACQUISITION_FAILED,
            ) from exc

        read_token = await self._git_token(GENERATION_PERMISSIONS)
        with tempfile.TemporaryDirectory(dir=enforced_root, prefix=f"publish_{task_id}_") as temp_dir:
            workspace_path = Path(temp_dir).resolve()
            git_wrapper = SafeGitWrapper(workspace_path, read_token)
            try:
                git_wrapper.clone(owner, repo)
                git_wrapper.checkout_new_branch(working_branch_name, base_sha)
                workspace = WorkspaceManager(workspace_path, enforced_root=enforced_root)
            except (GitError, WorkspaceError) as exc:
                raise GitHubExecutionError(f"Failed to acquire repository: {exc}", GitHubFailureStage.REPOSITORY_ACQUISITION_FAILED) from exc

            try:
                apply_artifact(workspace, files)
            except PublicationError as exc:
                raise GitHubExecutionError(str(exc), GitHubFailureStage.COMMIT_FAILED) from None
            if digest_of_workspace(workspace, artifact) != digest:
                raise GitHubExecutionError(
                    "The changes written for publication differ from the approved artifact.",
                    GitHubFailureStage.COMMIT_FAILED,
                )

            await authorize()
            # Only now, approved and re-authorized, does a write credential exist.
            write_client = self._client(PUBLICATION_PERMISSIONS)
            try:
                return await self._publish_remote(
                    artifact, approved_by, authorize, git_wrapper, write_client,
                    owner, repo, working_branch_name,
                )
            finally:
                await write_client.aclose()

    async def _publish_remote(
        self,
        artifact: dict[str, Any],
        approved_by: str,
        authorize: Callable[[], Awaitable[None]],
        git_wrapper: SafeGitWrapper,
        write_client: GitHubClient,
        owner: str,
        repo: str,
        working_branch_name: str,
    ) -> GitHubPublicationMetadata:
        """Branch, commit, push and pull request; runs inside publish()'s clone directory."""
        from app.execution.ownership import verify_async_ownership

        repository = artifact["repository"]
        base_sha = artifact["base_sha"]
        base_branch_name = artifact["base_branch"]
        files = artifact["files"]
        digest = artifact["digest"]
        try:
            await write_client.create_branch(
                CreateBranchRequest(owner=owner, repo=repo, branch_name=working_branch_name, base_sha=base_sha)
            )
        except GitHubError as exc:
            # Already exists from an earlier attempt: the push below only
            # succeeds if it holds this artifact's base or exact commit.
            logger.info("Branch %s creation check: %s", working_branch_name, exc)

        commit_message = artifact["commit_message"]
        try:
            await verify_async_ownership()
            commit_sha = git_wrapper.commit_files(
                [entry["path"] for entry in files],
                commit_message,
                commit_timestamp=int(artifact["created_ts"]),
            )
        except GitError as exc:
            raise GitHubExecutionError(f"Commit failed: {exc}", GitHubFailureStage.COMMIT_FAILED) from exc

        await authorize()
        git_wrapper.set_token(await self._git_token(PUBLICATION_PERMISSIONS))
        try:
            await verify_async_ownership()
            git_wrapper.push(working_branch_name)
        except GitError as exc:
            raise GitHubExecutionError(f"Push failed: {exc}", GitHubFailureStage.PUSH_FAILED) from exc

        # The reviewed body, plus a trailer made only of server-side values.
        pr_body = (
            f"{artifact['pull_request_body']}\n"
            f"Published after review: changes `{digest}` approved by @{approved_by}.\n"
        )
        await authorize()
        try:
            await verify_async_ownership()
            pr = await write_client.find_open_pull_request(owner, repo, working_branch_name)
            if pr is None:
                pr = await write_client.create_pull_request(
                    CreatePullRequestRequest(
                        owner=owner,
                        repo=repo,
                        title=artifact["pull_request_title"],
                        body=pr_body,
                        head_branch=working_branch_name,
                        base_branch=base_branch_name,
                    )
                )
        except GitHubError as exc:
            raise GitHubExecutionError(f"Pull Request creation failed: {exc}", GitHubFailureStage.PULL_REQUEST_FAILED) from exc

        return GitHubPublicationMetadata(
            repository=repository,
            base_branch=base_branch_name,
            working_branch=working_branch_name,
            commit_sha=commit_sha,
            pull_request_number=pr.number,
            pull_request_url=pr.html_url,
        )

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
