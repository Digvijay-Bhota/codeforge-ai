# Phase 1 Architecture — CodeForge AI Coding Agent

## Execution Lifecycle

```
POST /api/v1/tasks (TaskRequest)
        │
        ▼
  TaskService.run_task()
        │
        ├─ 1. WorkspaceManager(root_path)   ← validates root exists
        │
        ├─ 2. make_coding_agent(workspace, runner, model)
        │       └─ binds 5 tools via closures:
        │            list_files, read_file, search_files, write_file, run_tests
        │
        ├─ 3. Runner.run(agent, description)  ← OpenAI Agents SDK
        │       └─ agent loop:
        │            INSPECT → PLAN → EDIT → TEST → REPORT
        │
        ├─ 4. workspace.get_modified_paths()
        │   workspace.generate_diff()
        │
        ├─ 5. TestRunner.run(workspace_root)  ← final validation
        │
        └─ 6. TaskResult(status, changed_files, diff, test_result, ...)
```

## Security Boundaries

| Boundary | Enforcement |
|----------|-------------|
| Path traversal | `WorkspaceManager._resolve()` calls `Path.resolve()` and `relative_to(root)` — raises `WorkspaceError` before any I/O |
| File size | `MAX_FILE_SIZE = 512 KB` — enforced in `read_file()` |
| Shell access | None. The agent only has the 5 workspace tools. No `subprocess`, no `eval`, no `exec` accessible to the LLM |
| Test execution | `TestRunner` uses `subprocess.run` with `capture_output=True` and a 120s timeout. No `shell=True` |
| Secrets | `OPENAI_API_KEY` loaded from env/`.env` only — never in code or committed |

## Key Components

| Component | Location | Responsibility |
|-----------|----------|----------------|
| `WorkspaceManager` | `app/workspace/manager.py` | Sandboxed file I/O, change tracking, diff generation |
| `TestRunner` | `app/workspace/runner.py` | Controlled subprocess test execution |
| `make_coding_agent` | `app/agents/coding_agent.py` | Agent factory; binds tools to workspace |
| `TaskService` | `app/services/task_service.py` | Full task orchestration |
| `POST /api/v1/tasks` | `app/api/tasks.py` | HTTP endpoint |
| Schemas | `app/schemas/task.py` | `TaskRequest`, `TaskResult`, etc. |

## Available Agent Tools

| Tool | Signature | Effect |
|------|-----------|--------|
| `list_files` | `(path=".")` | Lists all files under path (workspace-relative) |
| `read_file` | `(path)` | Reads file content (512 KB limit) |
| `search_files` | `(query, path=".")` | Grep-like search with line numbers |
| `write_file` | `(path, content)` | Writes complete file, snapshots original |
| `run_tests` | `()` | Runs pytest, returns stdout/stderr/pass/fail |

## Test Strategy

- **Unit tests** (`test_workspace_manager.py`, `test_runner.py`, `test_task_schemas.py`): Pure Python, no LLM, no network
- **API tests** (`test_task_api.py`): FastAPI TestClient + AsyncMock — no LLM
- **Integration tests** (`integration/test_agent_workflow.py`): Real OpenAI calls, skipped without `OPENAI_API_KEY`, marked `@pytest.mark.integration`

Run unit tests only:
```bash
pytest -m "not integration"
```

Run integration tests:
```bash
OPENAI_API_KEY=sk-... pytest apps/api/tests/integration -v -m integration
```

## Known Limitations (Phase 1)

- **No auth**: The `/api/v1/tasks` endpoint is unauthenticated
- **Synchronous**: The endpoint blocks while the agent runs (no background job queue)
- **Single agent**: No multi-agent orchestration
- **No GitHub integration**: Workspaces are local paths only; no clone/push
- **No RAG**: File inspection is text-based; no semantic indexing
- **Local workspace only**: Caller must provide a local filesystem path
- **Single test framework**: TestRunner defaults to pytest; other runners need explicit `test_command`
- **No sandbox isolation**: Agent runs in the same filesystem as the server process (workspace boundary is enforced in software, not containers)

## Deferred to Later Phases

- GitHub App / PR creation (Phase 2)
- Database persistence of task results (Phase 2)
- Background task queue / async execution (Phase 2)
- Authentication / authorization (Phase 2)
- Repository cloning / sandboxed execution environment (Phase 3)
- RAG / semantic search (Phase 3)
- MCP tool integration (Phase 3)
- Multi-agent orchestration (Phase 4)
