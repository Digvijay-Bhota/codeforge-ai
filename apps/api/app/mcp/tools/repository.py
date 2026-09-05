import json
from typing import Any

from app.mcp.permissions import ToolPermission
from app.mcp.registry import MCPError, registry
from app.mcp.schemas import (
    MAX_FILE_SIZE,
    MAX_LIST_FILES,
    MAX_SEARCH_RESULTS,
    CreateFileInput,
    DeleteFileInput,
    ListFilesInput,
    MetadataInput,
    ModifyFileInput,
    ReadFileInput,
    SearchFilesInput,
)
from app.repository.scanner import RepositoryScanner
from app.workspace.manager import WorkspaceError, WorkspaceManager


# READ TOOLS
@registry.register("repository.list_files", "List files in the workspace.", ListFilesInput, ToolPermission.READ)
async def list_files(input_data: ListFilesInput, workspace: WorkspaceManager, **kwargs: Any) -> str:
    try:
        files = workspace.list_files(input_data.path)
        if len(files) > MAX_LIST_FILES:
            files = files[:MAX_LIST_FILES]
            files.append(f"... (truncated after {MAX_LIST_FILES} files)")
        return "\n".join(files) if files else "(no files found)"
    except WorkspaceError as exc:
        raise MCPError(str(exc)) from exc

@registry.register("repository.read_file", "Read file content.", ReadFileInput, ToolPermission.READ)
async def read_file(input_data: ReadFileInput, workspace: WorkspaceManager, **kwargs: Any) -> str:
    try:
        content = workspace.read_file(input_data.path)
        if len(content) > MAX_FILE_SIZE:
            content = content[:MAX_FILE_SIZE] + f"\n... (truncated after {MAX_FILE_SIZE} bytes)"
        return content
    except WorkspaceError as exc:
        raise MCPError(str(exc)) from exc

@registry.register("repository.search_files", "Search files in the workspace.", SearchFilesInput, ToolPermission.READ)
async def search_files(input_data: SearchFilesInput, workspace: WorkspaceManager, **kwargs: Any) -> str:
    try:
        results = workspace.search_files(input_data.query, input_data.path)
        if len(results) > MAX_SEARCH_RESULTS:
            results = results[:MAX_SEARCH_RESULTS]
        return json.dumps(results, indent=2)
    except WorkspaceError as exc:
        raise MCPError(str(exc)) from exc

@registry.register("repository.metadata", "Get repository metadata.", MetadataInput, ToolPermission.READ)
async def get_metadata(input_data: MetadataInput, workspace: WorkspaceManager, **kwargs: Any) -> str:
    scanner = RepositoryScanner(workspace)
    repo_map = scanner.scan()
    return repo_map.model_dump_json(indent=2)


# WRITE TOOLS
@registry.register("repository.create_file", "Create a new file.", CreateFileInput, ToolPermission.WRITE)
async def create_file(input_data: CreateFileInput, workspace: WorkspaceManager, **kwargs: Any) -> str:
    try:
        # Check if it already exists

        workspace.write_file(input_data.path, input_data.content)
        return f"File created: {input_data.path}"
    except WorkspaceError as exc:
        raise MCPError(str(exc)) from exc

@registry.register("repository.modify_file", "Modify an existing file.", ModifyFileInput, ToolPermission.WRITE)
async def modify_file(input_data: ModifyFileInput, workspace: WorkspaceManager, **kwargs: Any) -> str:
    try:
        workspace.write_file(input_data.path, input_data.content)
        return f"File modified: {input_data.path}"
    except WorkspaceError as exc:
        raise MCPError(str(exc)) from exc

@registry.register("repository.delete_file", "Delete a file.", DeleteFileInput, ToolPermission.WRITE)
async def delete_file(input_data: DeleteFileInput, workspace: WorkspaceManager, **kwargs: Any) -> str:
    try:
        workspace.delete_file(input_data.path)
        return f"File deleted: {input_data.path}"
    except WorkspaceError as exc:
        raise MCPError(str(exc)) from exc
