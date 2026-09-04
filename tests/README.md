# tests/

> **Status: Phase 0**

This top-level `tests/` directory is reserved for **integration tests** that span multiple services (e.g., API + database + Redis).

Unit tests for each application live alongside their source code:

- `apps/api/tests/` — FastAPI unit and integration tests

Integration tests requiring a running Docker Compose stack will be added in Phase 1+.
