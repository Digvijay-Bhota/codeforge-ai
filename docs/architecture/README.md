# Architecture — CodeForge AI

## Execution Lifecycle (Phase 2)

```
POST /api/v1/tasks (TaskRequest)
        │
        ▼
  TaskService.run_task()
        │
        ├─ 1. WorkspaceManager(root_path)   ← validates root exists and enforces security boundary
        │
        ├─ 2. RepositoryScanner(workspace)  ← Phase 2: Intelligence
        │       ├─ GitMetadata (branch, dirty, sha)
        │       └─ RepositoryMap (languages, frameworks, important files, source/test discovery)
        │
        ├─ 3. ContextBuilder (rank_files)   ← Phase 2: Bounded context prediction
        │       └─ RepositoryContext (map + git + relevant files)
        │
        ├─ 4. make_coding_agent(workspace, runner, model)
        │       └─ binds 5 tools via closures (list_files, read_file, search_files, write_file, run_tests)
        │
        ├─ 5. Runner.run(agent, prompt)     ← Prompt explicitly includes bounded RepositoryContext
        │       └─ agent loop:
        │            INSPECT → PLAN → EDIT → TEST → REPORT
        │
        ├─ 6. workspace.get_modified_paths()
        │   workspace.generate_diff()
        │
        ├─ 7. TestRunner.run(workspace_root)  ← final validation (pytest)
        │
        └─ 8. TaskResult(status, changed_files, diff, test_result, ...)
```

## Phase 2: Repository Intelligence

To avoid blindly passing an entire repository into the LLM context, Phase 2 implements a deterministic, rule-based repository intelligence layer:

*   **Repository Scanner:** Computes a bounded `RepositoryMap` containing detected languages, frameworks (via `pyproject.toml`, `package.json`, etc.), test files, and important config files.
*   **Context Builder:** Employs a deterministic heuristic scoring algorithm (`rank_files`) to find up to 20 files relevant to the task description.
*   **Git Metadata:** Collects current branch and SHA via safe, controlled `subprocess` invocations, without exposing unrestricted shell access.

## Security Boundaries

| Boundary | Enforcement |
|----------|-------------|
| Path traversal | `WorkspaceManager._resolve()` calls `Path.resolve()` and `relative_to(root)` — raises `WorkspaceError` |
| File size | `MAX_FILE_SIZE = 512 KB` — enforced on read and write |
| Shell access | The agent only has 5 controlled filesystem/test tools. No generic `shell` execution |
| Test execution | `TestRunner` uses `subprocess.run` with `capture_output=True`, timeout, and scrubbed `env` |
| Context isolation | `.git`, `.venv`, `node_modules` are automatically ignored from scans and searches |

## Key Components

| Component | Location | Responsibility |
|-----------|----------|----------------|
| `RepositoryScanner` | `app/repository/scanner.py` | Extracts repo map and metadata |
| `ContextBuilder` | `app/repository/context.py` | Ranks relevant files, formats bounded LLM context |
| `WorkspaceManager` | `app/workspace/manager.py` | Sandboxed file I/O, change tracking |
| `TestRunner` | `app/workspace/runner.py` | Controlled pytest execution |
| `make_coding_agent` | `app/agents/coding_agent.py` | Agent factory; binds tools |
| `TaskService` | `app/services/task_service.py` | Full task orchestration |

## Known Limitations

- **No auth**: The `/api/v1/tasks` endpoint is unauthenticated
- **Synchronous**: The endpoint blocks while the agent runs
- **Single agent**: No multi-agent orchestration yet (Phase 2 focuses on single-agent intelligence)
- **Local workspace only**: Caller must provide a local filesystem path
- **Single test framework**: TestRunner only supports pytest in Phase 1/2
