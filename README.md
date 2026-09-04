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

```bash
cd apps/api

# Install dependencies (if not already done)
pip install -r requirements.txt

# Run tests from the repository root
cd ../..
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

## Security

- No secrets are committed to this repository.
- `.env` is in `.gitignore`. Use `.env.example` as a template.
- No autonomous code execution, GitHub write access, or automatic merging is implemented in this phase.

---

## Contributing

See [`docs/decisions/README.md`](docs/decisions/README.md) for architectural decision records.
