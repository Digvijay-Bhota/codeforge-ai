# Architecture — CodeForge AI

## Execution Lifecycle (Phase 3)

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
        ├─ 4. make_planner_agent()          ← Phase 3: Planning Engine
        │       └─ Output: ImplementationPlan (Pydantic model)
        │
        ├─ 5. Runner.run(planner_agent, prompt) ← Generates structured plan from RepositoryContext
        │
        ├─ 6. make_coding_agent(workspace, runner, model)
        │       └─ binds 5 tools via closures (list_files, read_file, search_files, write_file, run_tests)
        │
        ├─ 7. Runner.run(agent, prompt)     ← Prompt explicitly includes bounded RepositoryContext AND ImplementationPlan
        │       └─ agent loop:
        │            INSPECT → IDENTIFY → PLAN (adapt) → EDIT → TEST → REPORT
        │
        ├─ 8. workspace.get_modified_paths()
        │   workspace.generate_diff()
        │
        ├─ 9. TestRunner.run(workspace_root)  ← final validation (pytest)
        │
        └─ 10. TaskResult(status, changed_files, diff, test_result, ...)
```

## Phase 2: Repository Intelligence

To avoid blindly passing an entire repository into the LLM context, Phase 2 implements a deterministic, rule-based repository intelligence layer:

*   **Repository Scanner:** Computes a bounded `RepositoryMap` containing detected languages, frameworks (via `pyproject.toml`, `package.json`, etc.), test files, and important config files.
*   **Context Builder:** Employs a deterministic heuristic scoring algorithm (`rank_files`) to find up to 20 files relevant to the task description.
*   **Git Metadata:** Collects current branch and SHA via safe, controlled `subprocess` invocations, without exposing unrestricted shell access.



## Phase 3: Planning Engine

To separate the "deciding what to change" from "actually changing it," Phase 3 introduces a dedicated Planner Agent.

*   **Planner:** Understands the task and repository context, decides what should change, and outputs a strictly validated `ImplementationPlan`.
*   **Coding Agent:** Takes the output plan and implements it step-by-step using tools.
*   **Validation:** Plan steps, bounds (like maximum number of steps or text length), and path traversals are deterministically validated before passing to the Coding Agent.

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
| `Planner` | `app/agents/planner.py` | Analyzes task and context to produce ImplementationPlan |
| `PlanSchemas` | `app/schemas/plan.py` | Contains bounds and deterministic plan validation |
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
