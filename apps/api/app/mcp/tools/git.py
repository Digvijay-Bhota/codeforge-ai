from typing import Any

from app.mcp.permissions import ToolPermission
from app.mcp.registry import registry
from app.mcp.schemas import MAX_GIT_STATUS_FILES, GitStatusInput
from app.repository.scanner import GitScanner
from app.workspace.manager import WorkspaceManager


@registry.register("git.status", "Get Git branch, commit, and dirty status.", GitStatusInput, ToolPermission.READ)
async def git_status(input_data: GitStatusInput, workspace: WorkspaceManager, **kwargs: Any) -> str:
    scanner = GitScanner(workspace)
    meta = scanner.scan()
    if not meta:
        return "No Git metadata found."

    # Enforce logical bounds before returning
    if len(meta.modified_files) > MAX_GIT_STATUS_FILES:
        meta.modified_files = meta.modified_files[: MAX_GIT_STATUS_FILES - 1]
        meta.modified_files.append(f"... (truncated after {MAX_GIT_STATUS_FILES - 1} files)")

    if len(meta.untracked_files) > MAX_GIT_STATUS_FILES:
        meta.untracked_files = meta.untracked_files[: MAX_GIT_STATUS_FILES - 1]
        meta.untracked_files.append(f"... (truncated after {MAX_GIT_STATUS_FILES - 1} files)")

    return meta.model_dump_json(indent=2)
