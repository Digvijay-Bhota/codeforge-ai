# Architecture — CodeForge AI

## Execution Lifecycle (Phase 5)

```
POST /api/v1/tasks (TaskRequest)
        │
        ▼
  TaskService.run_task()
        │
        ├─ WorkspaceManager(root_path)      ← validates root, enforces security boundary
        │
        ▼
  Orchestrator.run(workspace, task_description)
        │
        ├─ ANALYZING ── RepositoryAnalyst    ← Phase 2: deterministic, READ-ONLY
        │       └─ build_repository_context() → RepositoryContext
        │
        ├─ PLANNING ─── PlannerAgent         ← Phase 3: no tools, structured output only
        │       ├─ Runner.run(planner, prompt+context)
        │       ├─ validate_plan(plan)        ← deterministic validation
        │       └─ ImplementationPlan
        │
        ├─ CODING ───── CodingAgent          ← Phase 1/4: WRITE MCP tools (local shim)
        │       ├─ Runner.run(agent, prompt+context+plan)
        │       └─ CodingResult (changes_made, changed_paths, diff, message)
        │
        ├─ TESTING ──── TestRunner           ← Phase 1: controlled subprocess
        │       └─ TestResult (passed, exit_code, stdout, stderr, duration)
        │
        └─ COMPLETED / FAILED (at any stage)
                │
                ▼
          FinalTaskResult → mapped to TaskResult for API response
```

### Workflow State Machine

```
PENDING → ANALYZING → PLANNING → CODING → TESTING → COMPLETED
                                                    ↘ FAILED
Any non-terminal state can also transition → FAILED.
```

Invalid state transitions are explicitly rejected by `WorkflowState.transition()`.

---

## Phase 5: Multi-Agent Orchestration Foundation

Phase 5 introduces a controlled multi-agent orchestration layer that wraps
and coordinates the existing Phase 1–4 components.

### Key Principles

- **The Orchestrator controls the workflow.** Agents do not invoke other agents.
- **Agents produce typed artifacts.** Communication is via Pydantic models.
- **Stage transitions are explicit.** The Orchestrator decides which stage runs next.
- **No implicit swarm architecture.** No uncontrolled recursive agent calls.
- **Orchestration is currently synchronous.** No background workers, queues, or Redis.

### Stage Responsibilities

| Stage | Agent/Component | Permission | Artifact Produced |
|---|---|---|---|
| ANALYZING | `RepositoryAnalyst` | READ-only | `RepositoryContext` |
| PLANNING | `PlannerAgent` | No tools | `ImplementationPlan` |
| CODING | `CodingAgent` | WRITE (MCP local shim) | `CodingResult` |
| TESTING | `TestRunner` | Controlled subprocess | `TestResult` |
| Orchestrator | `Orchestrator` | Workflow control only | `FinalTaskResult` |

### Stage Failure Handling

Every stage has explicit failure handling. Failures stop the workflow immediately:

```
Analyst failure     → FAILED (no plan produced)
Planner failure     → FAILED (no coding)
Plan validation     → FAILED (no coding)
Coding failure      → FAILED (no testing)
No changes made     → FAILED (not COMPLETED)
Test failure        → FAILED (not COMPLETED)
```

---

## Phase 2: Repository Intelligence

To avoid blindly passing an entire repository into the LLM context, Phase 2 implements a deterministic, rule-based repository intelligence layer:

*   **Repository Scanner:** Computes a bounded `RepositoryMap` containing detected languages, frameworks (via `pyproject.toml`, `package.json`, etc.), test files, and important config files.
*   **Context Builder:** Employs a deterministic heuristic scoring algorithm (`rank_files`) to find up to 20 files relevant to the task description.
*   **Git Metadata:** Collects current branch and SHA via safe, controlled `subprocess` invocations, without exposing unrestricted shell access.

---

## Phase 3: Planning Engine

To separate the "deciding what to change" from "actually changing it," Phase 3 introduces a dedicated Planner Agent.

*   **Planner:** Understands the task and repository context, decides what should change, and outputs a strictly validated `ImplementationPlan`.
*   **Coding Agent:** Takes the output plan and implements it step-by-step using tools.
*   **Validation:** Plan steps, bounds (like maximum number of steps or text length), and path traversals are deterministically validated before passing to the Coding Agent.

---

## Phase 4: MCP Tool Layer

Phase 4 introduces a formal Model Context Protocol (MCP) server integration to securely govern agent-tool interactions.

*   **Tool Registry:** Maps standard tool names to verified Python handler functions.
*   **Permission Model:** Three explicit tiers (`READ`, `WRITE`, `DANGEROUS`). The agent executes under a declared permission tier.
*   **Disabled/Dangerous Ops:** The sandbox execution layer is abstracted but strictly disabled in Phase 4. No `git push`, PR creation, or arbitrary shell execution tools are enabled.
*   **Integration:** The agent accesses `WorkspaceManager` and `GitScanner` indirectly via the MCP registry, fully respecting all existing path traversal and filesystem limitations.

---

## Security Boundaries

| Boundary | Enforcement |
|----------|-------------|
| Path traversal | `WorkspaceManager._resolve()` calls `Path.resolve()` and `relative_to(root)` — raises `WorkspaceError` |
| File size | `MAX_FILE_SIZE = 512 KB` — enforced on read and write |
| Shell access | The Coding Agent only has controlled filesystem/test tools. No generic `shell` execution |
| Test execution | `TestRunner` uses `subprocess.run` with `capture_output=True`, timeout, and scrubbed `env` |
| Context isolation | `.git`, `.venv`, `node_modules` are automatically ignored from scans and searches |
| Analyst isolation | `RepositoryAnalyst` is read-only. No write methods are called. |
| Stack trace suppression | Orchestrator bounds all failure messages. No raw exceptions reach API consumers. |

---

## Key Components

| Component | Location | Responsibility |
|-----------|----------|----------------|
| `Orchestrator` | `app/orchestration/orchestrator.py` | Workflow control, state transitions |
| `WorkflowState` | `app/orchestration/models.py` | Internal mutable state for one task |
| `FinalTaskResult` | `app/orchestration/models.py` | Typed final result returned to API |
| `CodingResult` | `app/orchestration/models.py` | Typed artifact from Coding Agent |
| `RepositoryAnalyst` | `app/orchestration/analyst.py` | Read-only repo analysis (wraps Phase 2) |
| Stages | `app/orchestration/stages.py` | Per-stage execution helpers |
| `RepositoryScanner` | `app/repository/scanner.py` | Extracts repo map and metadata |
| `ContextBuilder` | `app/repository/context.py` | Ranks relevant files, formats bounded LLM context |
| `Planner` | `app/agents/planner.py` | Analyzes task and context to produce ImplementationPlan |
| `PlanSchemas` | `app/schemas/plan.py` | Contains bounds and deterministic plan validation |
| `WorkspaceManager` | `app/workspace/manager.py` | Sandboxed file I/O, change tracking |
| `TestRunner` | `app/workspace/runner.py` | Controlled pytest execution |
| `make_coding_agent` | `app/agents/coding_agent.py` | Agent factory; binds MCP tools |
| `TaskService` | `app/services/task_service.py` | API adapter; delegates to Orchestrator |

---

## Known Limitations / Future Work

- **No auth**: The `/api/v1/tasks` endpoint is unauthenticated
- **Synchronous**: The endpoint blocks while the orchestrator runs (no background workers yet)
- **No retries**: The orchestrator performs a single attempt with no automatic retry
- **Local workspace only**: Caller must provide a local filesystem path
- **Direct MCP transport**: Coding Agent uses a local function-tool shim, not a network MCP client
- **No distributed execution**: No Redis, Celery, queues, or Kubernetes
- **Not yet implemented**: Issue Analyst, Security Agent, Review Agent, GitHub PR, RAG/embeddings, event bus

## Phase 6A: GitHub Integration Foundation

Phase 6A establishes a strictly typed, isolated integration boundary for interacting with the GitHub API. It ensures that GitHub logic does not bleed into the core Orchestrator or agents.

### Key Principles
- **Isolation:** All GitHub API logic lives in `app/github/`.
- **Authentication:** GitHub tokens are configured via server-side settings/environment variables. Tokens are never accepted in task payloads, never serialized in responses, and never logged.
- **Typed Artifacts:** Repositories, branches, commits, and pull requests are explicitly validated Pydantic models.
- **Permissions:** A dedicated `GitHubPermission` model (`READ` vs `WRITE`) protects GitHub operations.
- **Security Boundaries:**
  - Repository owner/name and branch names are strictly validated via regex to prevent path traversal and malformed inputs.
  - No arbitrary HTTP requests or user-supplied URLs are allowed. The base API URL is fixed in configuration.
  - No shell or subprocess is used in the integration layer.
  - Exceptions are explicitly mapped to typed errors (`GitHubAuthenticationError`, `GitHubRateLimitError`, etc.) to avoid leaking raw response bodies or stack traces.

### Deferred to Phase 6B+
CodeForge AI cannot yet autonomously complete a full GitHub pull request lifecycle. The following features are intentionally deferred:
- Automated commit and push flows (Phase 6B).
- Automated PR creation and webhook integration.
- GitHub App installation flows.
- Exposing GitHub WRITE tools to the Coding Agent (MCP integration).

## Phase 6B: GitHub Execution Workflow

Phase 6B builds on the foundation of 6A to introduce GitHub as a direct execution target.

### Architecture
- **Execution Target:** The `TaskRequest` now supports an `execution_target` (`local` or `github`). Local workflow is preserved exactly as before for backwards compatibility.
- **GitHub Execution Service:** A dedicated `GitHubExecutionService` manages the lifecycle of a GitHub task, completely decoupling it from the Phase 5 Orchestrator.
- **Safe Git Wrapper:** Operations requiring local git clones (since CodeForge runs via file-system workspace modifications) are managed by `SafeGitWrapper`, which uses explicit timeouts, prevents token persistence in `.git/config`, and prevents arbitrary shell execution.

### Workflow Sequence
1. Validate `owner/repo` against the `GitHubClient`.
2. Determine and resolve the base branch SHA via API.
3. Create a unique task branch (`codeforge/task-<uuid>`) via GitHub API.
4. Safely acquire (clone) the repository into an isolated, temporary, self-cleaning workspace directory.
5. Execute the Phase 5 Orchestrator locally.
6. Verify modifications with local git status.
7. Safely commit changes with a deterministic identity and message.
8. Push the isolated branch to origin (explicitly rejecting force-pushes or pushing to default branches).
9. Create a structured Pull Request containing the task context, execution results, and agent output, ensuring proper output bounds.
10. Return a rich `GitHubPublicationMetadata` artifact indicating success.

## Phase 6C: GitHub App & Webhook Foundation

Phase 6C transitions CodeForge from a script-based agent into a continuous GitHub App integration.

### Architecture

- **GitHub App Authentication (`app_auth.py`):** Securely generates JWTs using the configured `GITHUB_APP_PRIVATE_KEY` and uses them to exchange for short-lived Installation Access Tokens.
- **Webhook Endpoint (`webhooks.py`):** Exposes `POST /api/v1/github/webhooks`.
- **Signature Validation:** All payloads are strictly verified via HMAC SHA-256 against `GITHUB_WEBHOOK_SECRET` before parsing.
- **Event Parsing & Models (`webhook_models.py`):** Normalizes specific payloads (`issues`, `issue_comment`, `pull_request`) into bounded, typed internal models.
- **Installation Authorization (`authorization.py`):** Enforces a strict security boundary preventing arbitrary installations from consuming execution cycles.
- **Command Parser (`command_parser.py`):** Deterministically parses explicit agent commands (e.g., `/codeforge implement`) from issue comments, rejecting arbitrary or shell-like input.
- **Event Mapping (`event_mapper.py`):** Maps authorized GitHub events into legacy `TaskRequest` intents targeting the GitHub execution engine.
- **Delivery Idempotency (`idempotency.py`):** Implements a bounded, LRU in-memory store using `X-GitHub-Delivery` to prevent duplicate webhook processing.
- **Execution Policy:** Webhooks are treated as observational. Agent execution is strictly gated behind explicit `/codeforge` commands to prevent runaway loops or unauthorized mutations.

### Security Constraints
- Secrets (Private Key, Webhook Secret, Installation Tokens) are strictly isolated and never exposed in API responses, logs, exceptions, task results, or subprocess arguments.
- Webhook bodies and extracted string content are size-bounded to prevent memory exhaustion and prompt injection.

### Current Limitations (Development Only)
- **Persistent Installation Storage:** Installations are currently handled in-memory; future phases will require database persistence for robust tenant authorization.
- **Distributed Idempotency:** The delivery store is in-memory and will not persist across container restarts. Production requires a Redis-backed store.
- **Background Execution:** The webhook currently executes synchronously and blocks the HTTP response. A future message queue (e.g., Redis/Celery) is required to decouple webhook reception from long-running agent tasks.
