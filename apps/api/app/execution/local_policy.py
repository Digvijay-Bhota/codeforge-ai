"""Policy gate for ``execution_target=local`` tasks.

Local execution points the coding agent — and the repository's own test
suite — at a directory on the worker host.  It is therefore disabled unless
the operator sets CODEFORGE_ALLOW_LOCAL_EXECUTION=true, and even then every
workspace must canonically resolve (following symlinks) to a directory
strictly inside WORKSPACE_ROOT.

The check is applied when a task is created and again when it executes, so a
path that is swapped for a symlink after creation is still caught.
"""

from __future__ import annotations

from pathlib import Path

from app.config import settings


class LocalExecutionError(Exception):
    """Raised when a local execution request violates policy."""


class LocalExecutionDisabledError(LocalExecutionError):
    """Raised when local execution has not been enabled by the operator."""


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


def resolve_local_workspace(workspace_path: str) -> Path:
    """Validate *workspace_path* against the local execution policy.

    Returns the canonical workspace directory.  Raises
    :class:`LocalExecutionDisabledError` when local execution is off and
    :class:`LocalExecutionError` for any other violation.
    """
    root = local_workspace_root()
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
            f"Workspace path {workspace_path!r} must resolve to a directory inside WORKSPACE_ROOT."
        )
    if not resolved.is_dir():
        raise LocalExecutionError(f"Workspace path {workspace_path!r} is not a directory.")
    return resolved
