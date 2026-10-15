# CodeForge AI

> **Status:** Phases 0–10B.4.2 are merged. The end-to-end loop works for GitHub repositories: an `@codeforge` comment creates a task, which then runs analysis, planning, optional plan approval, coding, tests, commit, push and a pull request, with GitHub check-run and comment status. Self-review, isolated (container) execution and non-pytest test runners are not implemented yet.

CodeForge AI is an AI software engineering agent platform. The long-term goal is a system that can autonomously understand a repository, plan implementations, write code, run tests, review its own changes, and open pull requests — with full observability, evaluation, and human-in-the-loop controls.

---

## Capabilities

| Capability | Status |
|---|---|
| Connect to GitHub repositories | ✅ GitHub App auth, installation tokens, typed client, webhooks (Phases 6A–6C, 10B.2–10B.3) |
| Understand repository structure & code | ✅ Heuristic repository scanner and analyst (Phases 2, 5). No embeddings or RAG |
| Analyse software-engineering tasks/issues | ✅ `@codeforge fix\|implement\|review\|explain\|analyze` comments become tasks (Phases 10B.3–10B.4) |
| Generate implementation plans | ✅ Planner agent plus deterministic plan validation (Phase 3) |
| Human approval gates | ✅ Optional plan approval with expiry, approve/reject/retry API (Phase 10A) |
| Modify code inside an isolated sandbox | ⚠️ Partial: edits are path-confined to a temporary clone (`WorkspaceManager`), but there is no process or container isolation and `sandbox.execute` is disabled |
| Run tests and validation | ⚠️ Partial: pytest only, run as a host subprocess with a timeout and a filtered environment |
| Review its own changes | ❌ Not implemented |
| Create pull requests | ✅ Branch, commit, push and PR creation (Phase 6B) |
| MCP-based tool integrations | ✅ MCP tool registry and server with permission levels (Phase 4) |
| Observability, evaluation & auditability | ✅ Task events, timeline, metrics, evaluation endpoints (Phase 8) |
| Durable async execution | ✅ PostgreSQL jobs with leases, outbox dispatcher, Redis queue, worker and reaper (Phases 7, 9, 10B.4) |

---

## How a Task Runs

1. A task is created by `POST /api/v1/tasks` or by an `@codeforge …` comment on an issue or pull request.
2. The task, its job and an outbox event are written in one transaction. The dispatcher queues the job and a worker claims it under a lease.
3. The orchestrator (`apps/api/app/orchestration/`) runs the stages in order: **analyzing → planning → coding → testing**. Any stage failure ends the task as failed, with no automatic retries.
4. If plan approval is required, the worker stops after planning and waits for `POST /api/v1/tasks/{id}/approve`.
5. For GitHub tasks, a successful run is committed to `codeforge/task-<id>`, pushed, and opened as a pull request. Check-run and comment status are kept up to date.

---

## Repository Layout

```
codeforge-ai/
├── apps/api/          ← FastAPI API, worker, dispatcher and all implemented code
│   ├── app/agents/          planner and coding agents (OpenAI Agents SDK)
│   ├── app/orchestration/   stage orchestrator
│   ├── app/workspace/       path-confined file access and test runner
│   ├── app/mcp/             MCP tool registry and server
│   ├── app/github/          GitHub App, client, webhooks, command and status consumers
│   └── alembic/             database migrations
├── agents/ mcp/ sandbox/ indexing/ evaluation/ infra/   ← placeholder directories (README only)
└── docs/              ← Architecture, security, decisions, deployment
```

---

## Technology Stack

| Layer | Technology |
|---|---|
| Language | Python 3.12 |
| API framework | FastAPI |
| Validation | Pydantic v2 |
| Configuration | pydantic-settings |
| Database | PostgreSQL 16 |
| Cache / broker | Redis 7 |
| Containers | Docker / Docker Compose |
| Testing | pytest + httpx |
| Linting | Ruff |
| Type-checking | mypy |
| CI | GitHub Actions |

---

## Local Development

### Prerequisites

- Python 3.12+
- Docker & Docker Compose

### Run with Docker Compose

```bash
# Copy the example environment file
cp .env.example .env

# Start all services
docker compose up --build
```

API will be available at `http://localhost:8000`.

### Run the API locally (without Docker)

```bash
cd apps/api

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

---

## Testing

About 90 of the tests are integration tests that need PostgreSQL. Locally they use `postgresql+asyncpg://codeforge:codeforge@localhost:5433/codeforge` (the Docker Compose `postgres` service) unless `TEST_DATABASE_URL` or `DATABASE_URL` is set.

```bash
# Start PostgreSQL for the integration tests
docker compose up -d postgres

# Install dependencies (if not already done)
pip install -r apps/api/requirements.txt

# Run tests from the repository root
pytest
```

---

## Linting & Type-Checking

```bash
# Lint
ruff check apps/api/app apps/api/tests

# Type-check
mypy apps/api/app
```

---

## Phase History

| Phase | Delivered |
|---|---|
| 0 | Monorepo foundation, FastAPI skeleton, Docker Compose, CI |
| 1 | Coding agent runtime with controlled workspace tools |
| 2 | Repository intelligence (scanner, git context) |
| 3 | Planning engine and plan validation |
| 4 | MCP tool layer |
| 5 | Multi-agent orchestration |
| 6A–6C | GitHub integration, GitHub execution target, GitHub App and webhooks |
| 7 | Persistence and async task lifecycle |
| 8 | Observability, evaluation and auditability |
| 9 | Production reliability and deployment hardening |
| 10A | Product API and approval core |
| 10B.1 | Identity and authentication |
| 10B.2.1–10B.2.3 | Installation tokens, typed GitHub client, checks and comments |
| 10B.3 | Webhook command ingestion |
| 10B.4.1–10B.4.2 | Command orchestration with durable outbox, check-run lifecycle and comment status |

---

## Authentication & Authorization (Phase 10B.1)

- **GitHub OAuth Login**: Interactive developer login starts at `GET /api/v1/auth/github/login` and exchanges codes at `GET /api/v1/auth/github/callback`. Single-use, cryptographically secure state parameters prevent CSRF and open redirects.
- **CodeForge Session JWTs**: HMAC-SHA256 signed session tokens authenticate developer requests via `Authorization: Bearer <token>`.
- **Repository Authorization**: Developers can only view or manage tasks for repositories where they have valid collaborator permissions (read/write/admin) under an active GitHub App installation.
- **Audit & Identity Integrity**: Tasks track `creator_id` referencing durable users, while approvals and rejections strictly capture the authenticated GitHub identity.

---

## Security

- No secrets are committed to this repository.
- `.env` is in `.gitignore`. Use `.env.example` as a template.
- JWT secrets must be at least 32 characters in production and not contain default placeholder text.

---

## Contributing

See [`docs/decisions/README.md`](docs/decisions/README.md) for architectural decision records.

## Deployment & Production Operations
See [docs/deployment.md](docs/deployment.md) for full operational runbooks.
