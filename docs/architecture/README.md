# docs/architecture/

This directory contains architectural documentation and diagrams for CodeForge AI.

## Phase 0

The Phase 0 architecture is intentionally minimal:

```
Client
  │
  ▼
FastAPI (apps/api)
  │
  ├── GET /health
  └── GET /api/v1/health

Infrastructure (Docker Compose)
  ├── api       — FastAPI on port 8000
  ├── postgres  — PostgreSQL 16 on port 5432
  └── redis     — Redis 7 on port 6379
```

## Future Architecture

See the [root README](../../README.md) for the planned multi-component architecture.
Detailed architecture documents will be added here as each phase is designed.
