"""Safe wrapper for local git operations against a cloned repository."""

import base64
import logging
import subprocess  # nosec B404
from pathlib import Path

logger = logging.getLogger(__name__)


class GitError(Exception):
    """Raised when a local git operation fails."""
    pass


class SafeGitWrapper:
    """Provides tightly controlled git subprocess calls.

    Ensures credentials are not persisted in config, restricts commands to safe
    arguments, and explicitly manages execution timeouts.
    """

    def __init__(self, workspace_root: Path, token: str) -> None:
        self.workspace_root = workspace_root
        self.token = token

        # GitHub expects Basic auth with x-access-token for HTTP git operations
        auth_str = f"x-access-token:{self.token}"
        b64_auth = base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")
        self._auth_header = f"Authorization: Basic {b64_auth}"

    def _run_git(self, args: list[str], timeout: int = 30, auth: bool = False) -> str:
        """Run a git command safely in the workspace root.

        If auth=True, the GitHub token is passed via GIT_CONFIG_* environment variables.
        This ensures the token never appears in argv, is not written to .git/config,
        and is limited strictly to the lifecycle of this subprocess execution.
        """
        env = {"GIT_TERMINAL_PROMPT": "0"}

        if auth:
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
            env["GIT_CONFIG_VALUE_0"] = self._auth_header

        try:
            # Explicitly use subprocess.run with shell=False and argument arrays.
            proc = subprocess.run(  # nosec B603 B607
                ["git", *args],
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
                env=env
            )
            return proc.stdout.strip()
        except subprocess.TimeoutExpired as exc:
            logger.error("Git command timed out: git %s", args[0])
            raise GitError(f"Git command timed out: {args[0]}") from exc
        except subprocess.CalledProcessError as exc:
            # Do not log stdout/stderr safely to avoid leaking token
            safe_stderr = exc.stderr.replace(self.token, "***") if exc.stderr else ""
            if self._auth_header in safe_stderr:
                safe_stderr = safe_stderr.replace(self._auth_header, "***")
            logger.error("Git command failed: git %s\n%s", args[0], safe_stderr)
            raise GitError(f"Git command failed: {args[0]}") from exc
        except Exception as exc:
            raise GitError(f"Unexpected git error: {exc}") from exc

    def clone(self, owner: str, repo: str) -> None:
        """Clone the repository without embedding the token in the origin URL on disk."""
        from app.execution.ownership import verify_ownership
        verify_ownership()

        url = f"https://github.com/{owner}/{repo}.git"
        logger.info("Cloning %s", url)
        self._run_git(["clone", "--quiet", url, "."], timeout=120, auth=True)

    def checkout_new_branch(self, branch_name: str, base_sha: str) -> None:
        """Create and checkout a new branch from a specific SHA."""
        from app.execution.ownership import verify_ownership
        verify_ownership()

        self._run_git(["checkout", "-b", branch_name, base_sha])

    def has_changes(self) -> bool:
        """Check if there are uncommitted changes."""
        stdout = self._run_git(["status", "--porcelain"])
        return bool(stdout.strip())

    def commit_files(self, paths: list[str], message: str) -> str:
        """Add specific tracked/untracked files and commit with a safe message."""
        from app.execution.ownership import verify_ownership
        verify_ownership()

        if not paths:
            raise GitError("No files specified for commit.")

        # Add files explicitly
        # Doing this safely: pass them as individual args after '--'
        self._run_git(["add", "--", *paths])

        verify_ownership()

        # Use a fixed identity
        self._run_git([
            "-c", "user.name=CodeForge AI",
            "-c", "user.email=codeforge@example.com",
            "commit", "-m", message
        ])
        return self._run_git(["rev-parse", "HEAD"])

    def push(self, branch_name: str) -> None:
        """Push the current branch to origin.

        Uses environment-based extraHeader to authenticate without saving the token.
        Never force pushes.
        """
        from app.execution.ownership import verify_ownership
        verify_ownership()

        # Validate that the branch name looks like our isolated task branch
        if not branch_name.startswith("codeforge/task-"):
            raise GitError(f"Refusing to push unsafe branch name: {branch_name}")

        self._run_git(["push", "--quiet", "origin", branch_name], timeout=60, auth=True)
