from collections.abc import Callable
from typing import Any, NamedTuple

from pydantic import BaseModel

import mcp.types as types
from app.mcp.permissions import ToolPermission, has_permission
from app.mcp.schemas import MAX_TOOL_OUTPUT_BYTES


class MCPError(Exception):
    """Structured error for MCP tool failures."""
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

class RegisteredTool(NamedTuple):
    name: str
    description: str
    input_schema: type[BaseModel]
    permission: ToolPermission
    handler: Callable[..., Any]
    enabled: bool

class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, name: str, description: str, input_schema: type[BaseModel], permission: ToolPermission, enabled: bool = True) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
            if name in self._tools:
                raise ValueError(f"Tool {name} is already registered.")
            self._tools[name] = RegisteredTool(name, description, input_schema, permission, handler, enabled)
            return handler
        return decorator

    def get_tool(self, name: str) -> RegisteredTool:
        if name not in self._tools:
            raise MCPError(f"Tool {name} not found in registry.")
        tool = self._tools[name]
        if not tool.enabled:
            raise MCPError(f"Tool {name} is disabled.")
        return tool

    def list_enabled_tools(self, caller_permission: ToolPermission) -> list[types.Tool]:
        mcp_tools = []
        for name, tool in self._tools.items():
            if tool.enabled and has_permission(tool.permission, caller_permission):
                mcp_tools.append(
                    types.Tool(
                        name=name,
                        description=tool.description,
                        input_schema=tool.input_schema.model_json_schema(),
                    )
                )
        return mcp_tools

    def _enforce_output_bound(self, text: str) -> str:
        """Deterministically truncate text to MAX_TOOL_OUTPUT_BYTES safely."""

        output_bytes = text.encode('utf-8')
        if len(output_bytes) <= MAX_TOOL_OUTPUT_BYTES:
            return text

        marker = f"\n... (output truncated, exceeded {MAX_TOOL_OUTPUT_BYTES} bytes limit)"
        marker_bytes = marker.encode('utf-8')

        if len(marker_bytes) >= MAX_TOOL_OUTPUT_BYTES:
            return marker_bytes[:MAX_TOOL_OUTPUT_BYTES].decode('utf-8', errors='ignore')

        allowed_bytes = MAX_TOOL_OUTPUT_BYTES - len(marker_bytes)
        # truncate and decode ignoring errors so we don't leave partial multibyte chars
        truncated_text = output_bytes[:allowed_bytes].decode('utf-8', errors='ignore')

        return truncated_text + marker

    async def execute_tool(self, name: str, arguments: dict[str, Any], caller_permission: ToolPermission, **context_kwargs: Any) -> list[types.TextContent]:
        from app.execution.ownership import OwnershipLostError, verify_async_ownership
        try:
            await verify_async_ownership()
            tool = self.get_tool(name)

            if not has_permission(tool.permission, caller_permission):
                raise MCPError(f"Insufficient permissions to execute {name}. Required: {tool.permission.value}, Granted: {caller_permission.value}")

            try:
                validated_input = tool.input_schema(**arguments)
            except Exception as e:
                raise MCPError(f"Invalid input for {name}: {e}") from e

            try:
                result_str = str(await tool.handler(validated_input, **context_kwargs))
                bounded_str = self._enforce_output_bound(result_str)
                return [types.TextContent(type="text", text=bounded_str)]
            except OwnershipLostError:
                raise
            except MCPError:
                raise
            except Exception as e:
                raise MCPError(f"Error executing {name}: {e}") from e

        except MCPError as e:
            bounded_err = self._enforce_output_bound(f"ERROR: {e.message}")
            return [types.TextContent(type="text", text=bounded_err)]

registry = ToolRegistry()
