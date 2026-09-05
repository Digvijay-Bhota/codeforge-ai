from typing import Any

import app.mcp.tools.git  # noqa: F401
import app.mcp.tools.repository  # noqa: F401
import app.mcp.tools.sandbox  # noqa: F401
import mcp.types as types
from app.mcp.permissions import ToolPermission
from app.mcp.registry import registry
from mcp.server import Server


def create_mcp_server(
    name: str = "CodeForge MCP",
    permission: ToolPermission = ToolPermission.READ,
    **context_kwargs: Any
) -> Server:
    """Create an MCP Server bound to the given permission level and context."""
    server = Server(name)

    async def handle_list_tools(context: Any, request: types.ListToolsRequest) -> types.ListToolsResult:
        tools = registry.list_enabled_tools(permission)
        return types.ListToolsResult(tools=tools)

    async def handle_call_tool(context: Any, request: types.CallToolRequest) -> types.CallToolResult:
        arguments = request.params.arguments if request.params.arguments else {}
        name = request.params.name
        content = await registry.execute_tool(name, arguments, permission, **context_kwargs)
        # Assuming execute_tool returns list[types.TextContent]
        # In mcp v2/types, CallToolResult takes content
        is_error = False
        if content and isinstance(content[0], types.TextContent) and content[0].text.startswith("ERROR:"):
            is_error = True
        return types.CallToolResult(content=list(content), is_error=is_error)

    server.add_request_handler("tools/list", types.ListToolsRequest, handle_list_tools)
    server.add_request_handler("tools/call", types.CallToolRequest, handle_call_tool)

    return server
