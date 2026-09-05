from typing import Any

from app.mcp.permissions import ToolPermission
from app.mcp.registry import registry
from app.mcp.schemas import SandboxExecuteInput


@registry.register("sandbox.execute", "Execute commands in a secure sandbox.", SandboxExecuteInput, ToolPermission.DANGEROUS, enabled=False)
async def execute(input_data: SandboxExecuteInput, **kwargs: Any) -> str:
    # Phase 4 keeps sandbox execution disabled.
    # Do not execute arbitrary host shell commands here.
    return "ERROR: Sandbox execution is not enabled in this environment."
