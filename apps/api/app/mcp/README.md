# CodeForge MCP Tool Layer

This package provides a secure Model Context Protocol (MCP) tool layer for CodeForge agents.

## Architecture

The MCP layer is built using the official `mcp` Python SDK (`mcp.server.Server`) and acts as a strict security boundary for repository and environment access.

1. **Transport**: `server.py` implements the MCP protocol bindings.
2. **Registry**: `registry.py` handles dynamic tool registration, permission verification, and schema validation.
3. **Permissions**: `permissions.py` defines the RBAC model (`READ`, `WRITE`, `DANGEROUS`).
4. **Implementation**: Tools in `tools/` wrap the secure `WorkspaceManager` and `GitScanner`.

## Current Tool Inventory

### READ Tools
- `repository.list_files`: List files in the workspace (bounded).
- `repository.read_file`: Read file contents (size limited).
- `repository.search_files`: Full-text search across the workspace.
- `repository.metadata`: Get structural repository map.
- `git.status`: Safe inspection of Git branches and dirty states.

### WRITE Tools
- `repository.create_file`: Safely create a file inside the workspace.
- `repository.modify_file`: Safely modify a file inside the workspace.
- `repository.delete_file`: Safely delete a file inside the workspace.

### DANGEROUS Tools (Disabled)
- `sandbox.execute`: Stubs for future isolated command execution.

## Phase 4 Limitations
In Phase 4, this layer explicitly **DOES NOT** provide:
- `git push` or arbitrary repository mutation history
- GitHub PR creation or API access
- Deployment capabilities
- Arbitrary host shell execution
- Production Docker sandbox execution

All shell/OS operations are strictly disallowed, and agents have no bypass around the `WorkspaceManager`.
