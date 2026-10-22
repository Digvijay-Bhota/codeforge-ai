# CodeForge AI

> **Status:** Phase 0 — Foundation only. No agents, MCP, sandboxing, GitHub integration, RAG, or autonomous coding are implemented yet.

CodeForge AI is an AI software engineering agent platform. The long-term goal is a system that can autonomously understand a repository, plan implementations, write code, run tests, review its own changes, and open pull requests — with full observability, evaluation, and human-in-the-loop controls.

---

## Long-Term Goal

| Capability | Status |
|---|---|
| Connect to GitHub repositories | 🔜 Planned |
| Understand repository structure & code | 🔜 Planned |
| Analyse software-engineering tasks/issues | 🔜 Planned |
| Generate implementation plans | 🔜 Planned |
| Modify code inside an isolated sandbox | 🔜 Planned |
| Run tests and validation | 🔜 Planned |
| Review its own changes | 🔜 Planned |
| Create pull requests | 🔜 Planned |
| MCP-based tool integrations | 🔜 Planned |
| Observability, evaluation & human approval gates | 🔜 Planned |

---

## Phase 0 — What Is Actually Built

- **FastAPI** REST API with a clean application factory
- Health endpoints at `GET /health` and `GET /api/v1/health`
- **Pydantic Settings** configuration layer (environment-variable driven, no hard-coded secrets)
- **Docker Compose** stack — `api`, `postgres` (with persistent volume), `redis`
- **pytest** test suite covering both health endpoints
- **Ruff** linting and **mypy** type-checking
- **GitHub Actions** CI pipeline

---

## Architecture Direction

```
codeforge-ai/
├── apps/api/          ← FastAPI backend (this phase)
├── apps/web/          ← Review console: sign in, follow tasks, approve publication
├── agents/            ← Agent runtime (future)
├── mcp/               ← MCP tool servers (future)
├── sandbox/           ← Isolated code execution (future)
├── indexing/          ← Repository indexing / RAG (future)
├── evaluation/        ← Agent evaluation harness (future)
├── infra/             ← Infrastructure-as-code (future)
└── docs/              ← Architecture, security, decisions
```

The API is intentionally minimal. Each future capability lives in its own top-level package so it can be developed, tested, and deployed independently.

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
- Node.js 20+ (only to develop the console outside Docker)

### Run with Docker Compose

```bash
# Create .env (never overwrites an existing one)
cp -n .env.example .env

# Set POSTGRES_PASSWORD, REDIS_PASSWORD and JWT_SECRET_KEY in .env, each a separate value from:
python -c "import secrets; print(secrets.token_urlsafe(32))"

# Start all services
docker compose up --build -d
```

The review console is at `http://localhost:3000`, and the API at `http://localhost:8000`. Both are
bound to 127.0.0.1 only. The Compose stack is for local development only and is not a production
deployment; PostgreSQL and Redis are reachable only inside the Compose network. For GitHub sign-in,
health checks and the full procedure, see [docs/deployment.md](docs/deployment.md#local-setup-step-by-step).

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

```bash
cd apps/api

# Install dependencies (if not already done)
pip install -r requirements.txt

# Run tests from the repository root
cd ../..
pytest
```

The tests need a PostgreSQL and a Redis they may wipe: `TEST_DATABASE_URL` or
`DATABASE_URL` (default `postgresql+asyncpg://codeforge:codeforge@localhost:5433/codeforge`)
and `REDIS_URL`. Never point them at a database whose data you need.

**End-to-end workflow test.** `apps/api/tests/e2e/test_comment_to_approval.py`
takes a signed `@codeforge` issue comment through the webhook, authorization,
the outbox, the worker's planning, the approval API and the execution gate
(`EXECUTION_UNAVAILABLE`), using CodeForge's real code throughout. Only the
external services are replaced: GitHub's REST API by a local HTTP server
(`tests/e2e/fake_github.py`), github.com's git transport by a local repository,
and OpenAI by an in-memory client. It needs no API keys or network, and
spends no model credit. CI runs it as its own step:

```bash
pytest apps/api/tests/e2e
```

It does not replace a live check with a real GitHub App and model.

---

## Linting & Type-Checking

```bash
# Lint
ruff check apps/api/app apps/api/tests

# Type-check
mypy apps/api/app
```

---

## Roadmap

| Phase | Focus |
|---|---|
| **Phase 0** ✅ | Monorepo foundation, FastAPI skeleton, Docker Compose, CI |
| Phase 1 | Database models, Alembic migrations, SQLAlchemy session management |
| Phase 2 | GitHub App integration (read-only), repository indexing |
| Phase 3 | Agent runtime, task planning, sandboxed code execution |
| Phase 4 | MCP tool integrations |
| Phase 5 | Observability, evaluation, human approval gates |
| Phase 6 | Pull request creation, autonomous coding loop |

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
