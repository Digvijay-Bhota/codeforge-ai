"""Policy gate for ``execution_target=local`` tasks and host-side workspaces.

Local execution points the coding agent — and the repository's own test
suite — at a directory on the worker host.  Three controls apply, and all of
them fail closed:

* **Operator opt-in.**  Disabled unless CODEFORGE_ALLOW_LOCAL_EXECUTION=true
  and WORKSPACE_ROOT is set.
* **Trusted identity.**  Only accounts whose immutable GitHub user ID is in
  CODEFORGE_LOCAL_EXECUTION_GITHUB_IDS may create, retry, approve or execute a
  local task.  The list is empty by default, and logging in through GitHub
  OAuth never adds anyone to it.  The same list decides whose tasks may run
  tests on the worker host under host_unsafe (see
  :func:`require_trusted_host_identity`).
* **Owner namespace.**  A trusted account's workspaces must canonically
  resolve (following symlinks) to a directory strictly inside its own
  ``WORKSPACE_ROOT/<github user id>/`` directory, so one account can never
  point a task at another account's workspace.

The checks run when a task is created, retried or approved and again when the
worker executes it, so a path swapped for a symlink after creation, or an
account removed from the list, is still caught.

GitHub tasks clone into a separate root (:func:`github_clone_root`) that must
not overlap WORKSPACE_ROOT, so local tasks cannot reach those clones either.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.repositories.user_repository import UserRepository

logger = logging.getLogger(__name__)


class LocalExecutionError(Exception):
    """Raised when a local execution request violates policy."""


class LocalExecutionDisabledError(LocalExecutionError):
    """Raised when local execution has not been enabled by the operator."""


class LocalExecutionForbiddenError(LocalExecutionError):
    """Raised when the requesting identity may not run code on the worker host."""


def local_workspace_root() -> Path:
    """Return the canonical WORKSPACE_ROOT, or raise if local execution is not allowed."""
    if not settings.codeforge_allow_local_execution:
        raise LocalExecutionDisabledError(
            "Local execution is disabled on this server. "
            "Set CODEFORGE_ALLOW_LOCAL_EXECUTION=true and WORKSPACE_ROOT to enable it."
        )
    if not settings.workspace_root:
        raise LocalExecutionError("WORKSPACE_ROOT must be configured for local execution.")
    root = Path(settings.workspace_root)
    if not root.is_absolute():
        raise LocalExecutionError("WORKSPACE_ROOT must be an absolute path.")
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise LocalExecutionError("WORKSPACE_ROOT does not exist.") from None
    if not resolved.is_dir():
        raise LocalExecutionError("WORKSPACE_ROOT is not a directory.")
    return resolved


def require_trusted_host_identity(github_user_id: int | None) -> int:
    """Return *github_user_id* if it is trusted to run code on the worker host.

    Raises :class:`LocalExecutionForbiddenError` for an unknown identity, an
    empty allow-list or any ID not in CODEFORGE_LOCAL_EXECUTION_GITHUB_IDS.
    """
    trusted = frozenset(settings.codeforge_local_execution_github_ids)
    if not trusted:
        logger.warning("Host execution refused: CODEFORGE_LOCAL_EXECUTION_GITHUB_IDS is empty")
    if github_user_id is None or github_user_id not in trusted:
        raise LocalExecutionForbiddenError(
            "This account is not permitted to run code on the worker host."
        )
    return github_user_id


async def host_identity_for_user(session: AsyncSession, user_id: str | None) -> int | None:
    """The immutable GitHub user ID of the active CodeForge user *user_id*.

    None when the ID is missing, the user does not exist or is inactive, or no
    GitHub identity is linked: callers treat that as untrusted.
    """
    if not user_id:
        return None
    user_repo = UserRepository(session)
    user = await user_repo.get_user(user_id)
    if not user or not user.is_active:
        return None
    identity = await user_repo.get_github_identity(user_id)
    return identity.github_user_id if identity else None


async def require_trusted_user(session: AsyncSession, user_id: str | None) -> int:
    """Resolve *user_id* to a trusted GitHub user ID, or raise :class:`LocalExecutionForbiddenError`."""
    return require_trusted_host_identity(await host_identity_for_user(session, user_id))


def owner_workspace_root(github_user_id: int) -> Path:
    """Return ``WORKSPACE_ROOT/<github_user_id>``: the only place this account's workspaces may be.

    It must be an existing real directory (not a symlink), so it cannot alias
    another account's directory.
    """
    root = local_workspace_root()
    expected = root / str(github_user_id)
    try:
        resolved = expected.resolve(strict=True)
    except (OSError, RuntimeError):
        raise LocalExecutionError(
            "No local workspace directory exists for this account under WORKSPACE_ROOT."
        ) from None
    if resolved != expected or not resolved.is_dir():
        raise LocalExecutionError(
            "This account's local workspace directory must be a real directory directly "
            "under WORKSPACE_ROOT."
        )
    return resolved


def resolve_local_workspace(workspace_path: str, *, owner_github_id: int) -> Path:
    """Validate *workspace_path* against the local execution policy for its owner.

    Returns the canonical workspace directory.  Raises
    :class:`LocalExecutionDisabledError` when local execution is off and
    :class:`LocalExecutionError` for any other violation.  Callers must have
    authorized *owner_github_id* with :func:`require_trusted_host_identity`.
    """
    root = owner_workspace_root(owner_github_id)
    if not workspace_path:
        raise LocalExecutionError("workspace_path is required for local execution.")
    candidate = Path(workspace_path)
    if not candidate.is_absolute():
        raise LocalExecutionError("workspace_path must be an absolute path.")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise LocalExecutionError(f"Workspace path {workspace_path!r} does not exist.") from None
    if resolved == root or not resolved.is_relative_to(root):
        raise LocalExecutionError(
            f"Workspace path {workspace_path!r} must resolve to a directory inside "
            "WORKSPACE_ROOT/<your GitHub user id>."
        )
    if not resolved.is_dir():
        raise LocalExecutionError(f"Workspace path {workspace_path!r} is not a directory.")
    return resolved


def github_clone_root() -> Path:
    """Return the canonical directory GitHub tasks clone into.

    CODEFORGE_GITHUB_CLONE_ROOT, or the system temporary directory.  It must
    exist, and must not contain or lie inside WORKSPACE_ROOT (compared after
    resolving symlinks), otherwise a local task could reach a GitHub clone.
    """
    configured = settings.codeforge_github_clone_root or tempfile.gettempdir()
    root = Path(configured)
    if not root.is_absolute():
        raise LocalExecutionError("CODEFORGE_GITHUB_CLONE_ROOT must be an absolute path.")
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise LocalExecutionError("The GitHub clone directory does not exist.") from None
    if not resolved.is_dir():
        raise LocalExecutionError("The GitHub clone directory is not a directory.")
    if settings.workspace_root:
        workspace = Path(settings.workspace_root).resolve()
        if workspace.is_relative_to(resolved) or resolved.is_relative_to(workspace):
            raise LocalExecutionError(
                "The GitHub clone directory overlaps WORKSPACE_ROOT; set "
                "CODEFORGE_GITHUB_CLONE_ROOT to a directory outside it."
            )
    return resolved
