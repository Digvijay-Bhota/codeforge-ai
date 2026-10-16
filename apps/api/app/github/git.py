"""Safe wrapper for local git operations against a cloned repository.

The cloned repository is untrusted: the coding agent and the repository's own
test suite both run against it, so ``.git/config`` and ``.git/hooks`` may have
been tampered with by the time we commit and push.  Every git invocation is
therefore hardened so that nothing from the repository, the host's system or
global git configuration, or the inherited environment can run programs on
the worker or redirect authenticated traffic:

* System and global configuration are ignored and the environment is built
  from scratch (no inherited HOME, proxies or GIT_* variables).
* Hooks and ``core.fsmonitor`` are disabled by command-scope overrides, and
  commit/push additionally pass ``--no-verify``.
* Transports are restricted to https, redirects are not followed, and
  credential helpers / askpass programs are disabled.
* Before each command in an existing repository, the effective configuration
  is checked against an allow-list of the keys ``git clone`` itself writes.
  Anything else — ``url.*.insteadOf``, ``http.*``, ``include.*``,
  ``core.hooksPath`` and so on — aborts the operation.
* The GitHub token is passed only to clone and push, as an ``extraHeader``
  scoped to ``https://github.com/``, never in argv or on disk, and is redacted
  from anything we log or raise.
"""

import base64
import logging
import os
import re
import subprocess  # nosec B404
from pathlib import Path

logger = logging.getLogger(__name__)

GITHUB_URL = "https://github.com/"

# Repository-scope config keys that `git clone` writes, plus the inert local
# identity (user.name / user.email) common in real repositories.  Section and
# variable names are matched case-insensitively (git lowercases them);
# `branch.<name>` subsections are free-form.
_ALLOWED_REPOSITORY_CONFIG = re.compile(
    r"core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase"
    r"|precomposeunicode|symlinks)"
    r"|remote\.origin\.(url|fetch)"
    r"|branch\..+\.(remote|merge)"
    r"|user\.(name|email)",
    re.IGNORECASE,
)

# GitHub owner and repository names.  Anything else (`..`, `@`, `?`, `#`, `%`,
# `/`, ...) could change which URL the authenticated clone and push reach.
_GITHUB_NAME = re.compile(r"[A-Za-z0-9_.-]+")


class GitError(Exception):
    """Raised when a local git operation fails."""
    pass


class SafeGitWrapper:
    """Provides tightly controlled git subprocess calls.

    Ensures credentials are not persisted in config, restricts commands to safe
    arguments, and explicitly manages execution timeouts.
    """

    # Transports git may use.  Tests widen this to reach a local bare remote.
    ALLOWED_PROTOCOLS: tuple[str, ...] = ("https",)

    def __init__(self, workspace_root: Path, token: str) -> None:
        self.workspace_root = workspace_root
        self.token = token
        self._remote_url: str | None = None

        # GitHub expects Basic auth with x-access-token for HTTP git operations
        auth_str = f"x-access-token:{self.token}"
        self._auth_b64 = base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")
        self._auth_header = f"Authorization: Basic {self._auth_b64}"

    def _redact(self, text: str) -> str:
        for secret in (self._auth_header, self._auth_b64, self.token):
            if secret:
                text = text.replace(secret, "***")
        return text

    def _hardened_config(self, auth: bool) -> list[tuple[str, str]]:
        config = [
            ("core.hooksPath", os.devnull),
            ("core.fsmonitor", "false"),
            ("core.askPass", ""),
            ("credential.helper", ""),
            ("protocol.allow", "never"),
            *((f"protocol.{p}.allow", "always") for p in self.ALLOWED_PROTOCOLS),
            ("http.followRedirects", "false"),
            ("http.sslVerify", "true"),
            ("commit.gpgSign", "false"),
            ("submodule.recurse", "false"),
        ]
        if auth:
            # Scoped to github.com: a rewritten or redirected URL never gets it.
            config.append((f"http.{GITHUB_URL}.extraHeader", self._auth_header))
        return config

    def _env(self, auth: bool) -> dict[str, str]:
        env = {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_ALLOW_PROTOCOL": ":".join(self.ALLOWED_PROTOCOLS),
            # Never discover a repository above the workspace.
            "GIT_CEILING_DIRECTORIES": str(Path(self.workspace_root).resolve().parent),
        }
        config = self._hardened_config(auth)
        env["GIT_CONFIG_COUNT"] = str(len(config))
        for i, (key, value) in enumerate(config):
            env[f"GIT_CONFIG_KEY_{i}"] = key
            env[f"GIT_CONFIG_VALUE_{i}"] = value
        return env

    def _exec(self, args: list[str], timeout: int, auth: bool, strip: bool = True) -> str:
        try:
            # Explicitly use subprocess.run with shell=False and argument arrays.
            proc = subprocess.run(  # nosec B603 B607
                ["git", *args],
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
                env=self._env(auth),
            )
            return proc.stdout.strip() if strip else proc.stdout
        # Exceptions are re-raised `from None`: the originals carry raw stderr
        # and environment that may contain credentials.
        except subprocess.TimeoutExpired:
            logger.error("Git command timed out: git %s", args[0])
            raise GitError(f"Git command timed out: {args[0]}") from None
        except subprocess.CalledProcessError as exc:
            safe_stderr = self._redact(exc.stderr) if exc.stderr else ""
            logger.error("Git command failed: git %s\n%s", args[0], safe_stderr)
            raise GitError(f"Git command failed: {args[0]}") from None
        except Exception as exc:
            raise GitError(f"Unexpected git error: {self._redact(str(exc))}") from None

    def _verify_repository_config(self) -> None:
        """Refuse to operate if the repository carries any non-clone configuration."""
        listing = self._exec(
            ["config", "--list", "--show-scope", "--name-only", "-z"], timeout=30, auth=False
        )
        fields = listing.split("\0")
        disallowed = sorted(
            {
                name
                for scope, name in zip(fields[::2], fields[1::2], strict=False)
                if scope != "command" and not _ALLOWED_REPOSITORY_CONFIG.fullmatch(name)
            }
        )
        if disallowed:
            # Key names only; values may carry URLs or commands.
            logger.error("Repository configuration contains disallowed keys: %s", disallowed)
            raise GitError(
                f"Repository configuration contains disallowed setting(s): {', '.join(disallowed)}"
            )

    def _run_git(
        self, args: list[str], timeout: int = 30, auth: bool = False, strip: bool = True
    ) -> str:
        """Run a hardened git command in the workspace root.

        If auth=True, the GitHub token is passed via GIT_CONFIG_* environment variables.
        This ensures the token never appears in argv, is not written to .git/config,
        and is limited strictly to the lifecycle of this subprocess execution.
        """
        if args[0] != "clone":
            self._verify_repository_config()
        return self._exec(args, timeout=timeout, auth=auth, strip=strip)

    def clone(self, owner: str, repo: str) -> None:
        """Clone the repository without embedding the token in the origin URL on disk."""
        from app.execution.ownership import verify_ownership
        verify_ownership()

        for name in (owner, repo):
            if not _GITHUB_NAME.fullmatch(name) or name in (".", ".."):
                raise GitError(f"Invalid repository name: {owner!r}/{repo!r}")
        url = f"{GITHUB_URL}{owner}/{repo}.git"
        logger.info("Cloning %s", url)
        self._run_git(["clone", "--quiet", "--no-recurse-submodules", url, "."], timeout=120, auth=True)
        self._remote_url = url

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
            "commit", "--no-verify", "-m", message
        ])
        return self._run_git(["rev-parse", "HEAD"])

    def push(self, branch_name: str) -> None:
        """Push the branch to the repository it was cloned from.

        Pushes to the explicit clone URL rather than the ``origin`` remote, so
        repository config cannot substitute a different destination.  Never
        force pushes and never runs hooks.
        """
        from app.execution.ownership import verify_ownership
        verify_ownership()

        # Validate that the branch name looks like our isolated task branch
        if not branch_name.startswith("codeforge/task-"):
            raise GitError(f"Refusing to push unsafe branch name: {branch_name}")
        if self._remote_url is None:
            raise GitError("Refusing to push: repository was not cloned by this wrapper.")

        refspec = f"refs/heads/{branch_name}:refs/heads/{branch_name}"
        self._run_git(
            ["push", "--quiet", "--no-verify", self._remote_url, refspec], timeout=60, auth=True
        )


def run_readonly_git(workspace_root: Path, args: list[str], timeout: int = 5) -> str:
    """Run a read-only git query with SafeGitWrapper's hardening and no credentials.

    The repository config allow-list, disabled hooks/fsmonitor and the
    from-scratch environment all apply.  Output is returned unstripped
    (porcelain formats have significant leading whitespace).
    Raises :class:`GitError` on any failure, including tampered config.
    """
    # Empty token: read-only queries never authenticate, so no credential exists.
    wrapper = SafeGitWrapper(workspace_root, token="")  # nosec B106
    return wrapper._run_git(args, timeout=timeout, strip=False)
