from collections.abc import Callable
from datetime import UTC
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
        from datetime import datetime

        from app.execution.ownership import OwnershipLostError, verify_async_ownership
        from app.observability.events import AuditEventType, EventType
        from app.observability.metrics import inc_counter, record_histogram
        from app.observability.tracing import record_audit_event, record_event

        start_time = datetime.now(UTC)
        await record_event(None, EventType.TOOL_CALL_STARTED, component="mcp", metadata={"tool": name, "arguments": arguments})
        inc_counter("codeforge_tool_calls_total", labels={"tool": name})

        try:
            await verify_async_ownership()
            tool = self.get_tool(name)

            if not has_permission(tool.permission, caller_permission):
                await record_audit_event(None, AuditEventType.AUTHORIZATION_DENIED, actor_type="AGENT", resource_type="tool", resource_id=name, result="denied", metadata={"required": tool.permission.value, "granted": caller_permission.value})
                inc_counter("codeforge_security_blocks_total", labels={"tool": name, "reason": "permission"})
                raise MCPError(f"Insufficient permissions to execute {name}. Required: {tool.permission.value}, Granted: {caller_permission.value}")

            try:
                validated_input = tool.input_schema(**arguments)
            except Exception as e:
                raise MCPError(f"Invalid input for {name}: {e}") from e

            try:
                result_str = str(await tool.handler(validated_input, **context_kwargs))
                bounded_str = self._enforce_output_bound(result_str)
                duration_ms = int((datetime.now(UTC) - start_time).total_seconds() * 1000)
                await record_event(None, EventType.TOOL_CALL_COMPLETED, component="mcp", status="success", duration_ms=duration_ms, metadata={"tool": name, "result_summary": bounded_str})
                record_histogram("tool_duration_ms", float(duration_ms), labels={"tool": name})
                return [types.TextContent(type="text", text=bounded_str)]
            except OwnershipLostError:
                await record_audit_event(None, AuditEventType.OWNERSHIP_LOST, actor_type="AGENT", resource_type="tool", resource_id=name, result="denied")
                inc_counter("codeforge_security_blocks_total", labels={"tool": name, "reason": "ownership_lost"})
                raise
            except MCPError:
                raise
            except Exception as e:
                raise MCPError(f"Error executing {name}: {e}") from e

        except MCPError as e:
            duration_ms = int((datetime.now(UTC) - start_time).total_seconds() * 1000)
            await record_event(None, EventType.TOOL_CALL_FAILED, component="mcp", status="failed", duration_ms=duration_ms, metadata={"tool": name, "error": str(e)})
            inc_counter("codeforge_tool_failures_total", labels={"tool": name})
            bounded_err = self._enforce_output_bound(f"ERROR: {e.message}")
            return [types.TextContent(type="text", text=bounded_err)]

registry = ToolRegistry()
