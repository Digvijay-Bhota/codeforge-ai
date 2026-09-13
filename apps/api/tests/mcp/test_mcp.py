import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

import app.mcp.tools.git  # noqa: F401
import app.mcp.tools.repository  # noqa: F401
from app.mcp.permissions import ToolPermission, has_permission
from app.mcp.registry import MCPError, ToolRegistry, registry
from app.workspace.manager import WorkspaceManager


# Test Registry
def test_registry_basics():
    test_reg = ToolRegistry()
    class DummyInput(BaseModel):
        pass

    @test_reg.register("test.tool", "desc", DummyInput, ToolPermission.READ)
    async def handler(inp, **kwargs): return "ok"

    assert test_reg.get_tool("test.tool").name == "test.tool"

    with pytest.raises(ValueError):
        @test_reg.register("test.tool", "desc2", DummyInput, ToolPermission.READ)
        async def handler2(inp, **kwargs): return "ok2"

    with pytest.raises(MCPError, match="not found"):
        test_reg.get_tool("unknown")

def test_registry_disabled():
    test_reg = ToolRegistry()
    class DummyInput(BaseModel):
        pass

    @test_reg.register("test.disabled", "desc", DummyInput, ToolPermission.READ, enabled=False)
    async def handler(inp, **kwargs): return "ok"

    with pytest.raises(MCPError, match="is disabled"):
        test_reg.get_tool("test.disabled")

# Test Permissions
def test_permissions_logic():
    assert has_permission(ToolPermission.READ, ToolPermission.READ)
    assert has_permission(ToolPermission.READ, ToolPermission.WRITE)
    assert has_permission(ToolPermission.READ, ToolPermission.DANGEROUS)
    assert not has_permission(ToolPermission.WRITE, ToolPermission.READ)
    assert has_permission(ToolPermission.WRITE, ToolPermission.WRITE)
    assert not has_permission(ToolPermission.DANGEROUS, ToolPermission.WRITE)

@pytest.mark.asyncio
async def test_permission_enforcement():
    # Execute read tool with read permission
    res = await registry.execute_tool("repository.list_files", {"path": "."}, ToolPermission.READ, workspace=MagicMock())
    # Should just return the mock's failure or ok, but shouldn't be a permission error
    assert "Insufficient permissions" not in res[0].text

    # Execute write tool with read permission
    res = await registry.execute_tool("repository.modify_file", {"path": "a.txt", "content": "b"}, ToolPermission.READ, workspace=MagicMock())
    assert "ERROR: Insufficient permissions" in res[0].text

@pytest.mark.asyncio
async def test_repo_tools(tmp_path: Path):
    workspace = WorkspaceManager(tmp_path)
    (tmp_path / "test.txt").write_text("hello")

    # Read file
    res = await registry.execute_tool("repository.read_file", {"path": "test.txt"}, ToolPermission.READ, workspace=workspace)
    assert "hello" in res[0].text

    # Write file
    res = await registry.execute_tool("repository.create_file", {"path": "new.txt", "content": "world"}, ToolPermission.WRITE, workspace=workspace)
    assert "File created" in res[0].text
    assert (tmp_path / "new.txt").read_text() == "world"

    # Absolute path
    res = await registry.execute_tool("repository.read_file", {"path": "/etc/passwd"}, ToolPermission.READ, workspace=workspace)
    assert "ERROR:" in res[0].text

    # Traversal
    res = await registry.execute_tool("repository.read_file", {"path": "../secret"}, ToolPermission.READ, workspace=workspace)
    assert "ERROR:" in res[0].text

@pytest.mark.asyncio
async def test_mcp_server_exposure():
    from app.mcp.server import create_mcp_server
    server = create_mcp_server(permission=ToolPermission.READ)
    handler = server._request_handlers["tools/list"].handler
    tools = await handler(None, None)
    tool_names = [t.name for t in tools.tools]

    assert "repository.list_files" in tool_names
    assert "repository.read_file" in tool_names
    assert "repository.modify_file" not in tool_names  # WRITE tool hidden
    assert "sandbox.execute" not in tool_names         # DANGEROUS / disabled hidden

def test_coding_agent_integration():
    from app.agents.coding_agent import make_coding_agent
    from app.workspace.runner import TestRunner

    workspace = WorkspaceManager(Path("."))
    runner = TestRunner()

    # Agent receives WRITE permission by default from _make_tools
    agent = make_coding_agent(workspace, runner)

    # We can inspect the tools list
    tool_names = [t.name for t in agent.tools]
    assert "read_file" in tool_names
    assert "modify_file" in tool_names
    assert "run_tests" in tool_names
    assert "execute" not in tool_names
    assert "sandbox.execute" not in tool_names



@pytest.mark.asyncio
async def test_central_output_truncation_comprehensive():
    from app.mcp.schemas import MAX_TOOL_OUTPUT_BYTES
    test_reg = ToolRegistry()
    class DummyInput(BaseModel):
        pass

    @test_reg.register("test.normal", "desc", DummyInput, ToolPermission.READ)
    async def handler_normal(inp, **kwargs):
        return "Normal Output"

    @test_reg.register("test.ascii_oversize", "desc", DummyInput, ToolPermission.READ)
    async def handler_ascii(inp, **kwargs):
        return "A" * (MAX_TOOL_OUTPUT_BYTES + 500)

    @test_reg.register("test.utf8_oversize", "desc", DummyInput, ToolPermission.READ)
    async def handler_utf8(inp, **kwargs):
        # 🦊 is 4 bytes. Repeat it enough to easily exceed.
        return "🦊" * (MAX_TOOL_OUTPUT_BYTES // 2 + 100)

    @test_reg.register("test.error_oversize", "desc", DummyInput, ToolPermission.READ)
    async def handler_error(inp, **kwargs):
        raise MCPError("E" * (MAX_TOOL_OUTPUT_BYTES + 500))

    # 1. Normal output unchanged
    res = await test_reg.execute_tool("test.normal", {}, ToolPermission.READ)
    assert res[0].text == "Normal Output"

    # 2. ASCII oversized output bounded safely
    res_ascii = await test_reg.execute_tool("test.ascii_oversize", {}, ToolPermission.READ)
    text_ascii = res_ascii[0].text
    assert "truncated" in text_ascii
    assert len(text_ascii.encode("utf-8")) <= MAX_TOOL_OUTPUT_BYTES

    # 3. Multibyte UTF-8 oversized output bounded safely
    res_utf8 = await test_reg.execute_tool("test.utf8_oversize", {}, ToolPermission.READ)
    text_utf8 = res_utf8[0].text
    assert "truncated" in text_utf8
    assert len(text_utf8.encode("utf-8")) <= MAX_TOOL_OUTPUT_BYTES
    # Ensure it didn't leave a garbled half-character at the split
    text_utf8.encode("utf-8").decode("utf-8")

    # 4. Truncation is deterministic
    res_utf8_2 = await test_reg.execute_tool("test.utf8_oversize", {}, ToolPermission.READ)
    assert text_utf8 == res_utf8_2[0].text

    # 5. Error bounding
    res_err = await test_reg.execute_tool("test.error_oversize", {}, ToolPermission.READ)
    text_err = res_err[0].text
    assert text_err.startswith("ERROR: ")
    assert "truncated" in text_err
    assert len(text_err.encode("utf-8")) <= MAX_TOOL_OUTPUT_BYTES
    assert "Traceback" not in text_err


@pytest.mark.asyncio
async def test_git_status_logical_bounding(tmp_path: Path):
    from app.mcp.schemas import MAX_GIT_STATUS_FILES, GitStatusInput
    from app.mcp.tools.git import git_status
    from app.repository.models import GitMetadata

    # Create a mock WorkspaceManager to satisfy type hinting, though we will mock scanner
    workspace = WorkspaceManager(tmp_path)

    excessive_modified = [f"mod_{i}.py" for i in range(MAX_GIT_STATUS_FILES + 10)]
    excessive_untracked = [f"new_{i}.py" for i in range(MAX_GIT_STATUS_FILES + 5)]

    class MockScanner:
        def __init__(self, ws): pass
        def scan(self):
            return GitMetadata(
                is_available=True,
                is_dirty=True,
                modified_files=excessive_modified.copy(),
                untracked_files=excessive_untracked.copy()
            )

    with patch("app.mcp.tools.git.GitScanner", MockScanner):
        res = await git_status(GitStatusInput(), workspace=workspace)
        # Parse JSON output
        parsed = json.loads(res)

        assert len(parsed["modified_files"]) == MAX_GIT_STATUS_FILES
        assert "... (truncated after" in parsed["modified_files"][-1]

        assert len(parsed["untracked_files"]) == MAX_GIT_STATUS_FILES
        assert "... (truncated after" in parsed["untracked_files"][-1]
