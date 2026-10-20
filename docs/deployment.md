# Deployment & Operations Guide (Phase 9)

## Architecture Overview
The CodeForge AI system is composed of several independent services operating over a shared PostgreSQL database and Redis cluster.
- **API (FastAPI)**: Serves synchronous HTTP requests, queues tasks, handles webhooks.
- **Worker (Asyncio)**: Polls Redis for jobs, executes long-running agentic coding tasks.
- **Outbox Dispatcher (Asyncio)**: Relays durable outbox events from PostgreSQL to Redis.
- **PostgreSQL**: The single source of truth for all application state.
- **Redis**: The transient message broker for queueing and task distribution.

## Environment Variables
The application enforces strict separation between environments through the `APP_ENV` variable.
- `APP_ENV=production`: Fails closed if critical secrets are missing (e.g. `OPENAI_API_KEY`). Requires an explicit `DATABASE_URL`.
- `APP_ENV=development` / `test`: Relaxes credential checks for localized testing.
- `JWT_SECRET_KEY` (session-token signing key) must be a random value of at least 32 characters in every environment except an *explicit* `APP_ENV=development` or `APP_ENV=test`. The public default from `.env.example` and placeholder values are refused at startup when `APP_ENV` is `production`, `staging`, `prod`, empty, unset or anything else, because anyone could forge session tokens with them. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

### Required Production Settings
```bash
APP_ENV=production
DATABASE_URL=postgresql+asyncpg://codeforge:secret@your-postgres:5432/codeforge
REDIS_URL=redis://your-redis:6379/0
OPENAI_API_KEY=sk-....
JWT_SECRET_KEY=<random, at least 32 characters>
GITHUB_APP_ID=12345
GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----..."
GITHUB_WEBHOOK_SECRET=your_secret
```

### Local Execution and Host Trust
`execution_target=local` points the coding agent, and the repository's tests, at a directory on the worker host. It is off by default and should stay off in production. Every control below fails closed:
- `CODEFORGE_ALLOW_LOCAL_EXECUTION=true` and an absolute, existing `WORKSPACE_ROOT` are both required.
- `CODEFORGE_LOCAL_EXECUTION_GITHUB_IDS` must list the immutable numeric GitHub user IDs of the accounts trusted to run code on the worker host, as a JSON list (for example `[12345678]`). Use IDs, not logins, because logins can be renamed and reused. The list is empty by default. Any account can log in through GitHub OAuth, but login never grants this. Malformed values stop startup.
- Only listed accounts may create, retry, approve or execute local tasks. The check runs at the API and again in the worker before anything touches the filesystem, so a task inserted around the API, or created by an account later removed from the list, does not run.
- Each listed account's workspaces must resolve, after following symlinks, strictly inside its own `WORKSPACE_ROOT/<github user id>/` directory. That must be a real directory (not a symlink) created by the operator. A local task is visible and modifiable only by its creator; a task without a recorded creator belongs to no one.
- Under `CODEFORGE_TEST_EXECUTION_MODE=host_unsafe` (still only with an explicit `APP_ENV=development` or `test`), only tasks created by listed accounts run their tests, including GitHub tasks. `docker` and `gvisor` modes are unaffected.
- GitHub tasks clone into `CODEFORGE_GITHUB_CLONE_ROOT` (default: the system temp directory). It must not contain or lie inside `WORKSPACE_ROOT`, compared after resolving symlinks. Startup refuses overlapping explicit values, and a GitHub task refuses to run, before touching GitHub, if the effective clone root overlaps.

**Compatibility:** local-execution deployments must now set `CODEFORGE_LOCAL_EXECUTION_GITHUB_IDS` and move each workspace into its owner's `WORKSPACE_ROOT/<github user id>/` directory. Local tasks created before this change keep their old paths, so they are refused at execution; retry them with a `workspace_path` inside the new directory. If `WORKSPACE_ROOT` was under the system temp directory, set `CODEFORGE_GITHUB_CLONE_ROOT` outside it.

### Coding-Agent Budgets
- `CODEFORGE_CODING_MAX_TURNS` (default `25`, range 1–100): maximum model turns for each coding-agent run, including each repair attempt. A turn is one model call plus the tool calls it requests. When the limit is reached the task fails with a message naming the limit, and nothing is committed.
- `CODEFORGE_MAX_REPAIR_ATTEMPTS` (default `0`, range 0–5): after a failed test run, the coding agent gets the failing output and the current diff and may try again, up to this many times. Tests are re-run after each attempt; the loop stops as soon as they pass, when an attempt makes no changes, or when the budget is used up. A task can make up to `CODEFORGE_CODING_MAX_TURNS × (1 + CODEFORGE_MAX_REPAIR_ATTEMPTS)` model calls.

> **Security:** a repository's tests (including `conftest.py`) are untrusted code. `CODEFORGE_TEST_EXECUTION_MODE` decides where they run: `disabled` (default; tasks fail at testing), `host_unsafe` (on the worker host with the worker's permissions — development/test only), `docker` (a hardened throwaway container; see [`security/docker-test-executor.md`](security/docker-test-executor.md) for the controls, deployment requirements and remaining limitations — it is a container on a shared kernel, not complete isolation), or `gvisor` (the same container under a verified gVisor runtime with a guest task limit; see [`security/gvisor-test-executor.md`](security/gvisor-test-executor.md) — it needs a Docker daemon with runsc registered exactly as documented, preferably on a dedicated execution host reached over TLS, and cannot run in the current Compose worker). Repair attempts re-run the tests and are only accepted with `sandbox` mode, which is not implemented yet, so they stay disabled in every other mode, including `gvisor`.

## Startup Order & Dependencies
The Docker Compose topology dictates the correct boot order:
1. `postgres` and `redis` start first.
2. `api`, `worker`, and `dispatcher` declare `depends_on: service_healthy` for the database and redis.
3. The API runs `uvicorn app.main:app` which mounts the `/health` and `/ready` endpoints.
4. The background services (`worker`, `dispatcher`) execute directly via `python -m app.worker` and `python -m app.outbox_dispatcher`.

## Migrations
Alembic migrations strictly precede application rollout. Do not run migrations automatically in the API startup.
1. Deploy the new image.
2. Execute the migration container/script:
   `alembic upgrade head`
3. Restart/deploy the API, Worker, and Dispatcher containers.

## Health and Readiness
- **Liveness (`/health`)**: Confirms the API container process is executing (`200 OK`).
- **Readiness (`/ready`)**: Verifies connection pools by pinging PostgreSQL (`SELECT 1`) and Redis. If either is degraded, the API returns `503 Service Unavailable` without leaking connection metadata.

## Graceful Shutdown
All CodeForge AI processes natively trap `SIGINT` and `SIGTERM`:
- **API**: Uvicorn bleeds active connections gracefully and disposes DB/Redis connections.
- **Worker**: Ceases dequeueing new jobs, allows the active mutation to cleanly finish, and flushes observability state before exiting.
- **Dispatcher**: Finishes its active database transaction loop safely before exiting.

## Failure Recovery
- **Worker Crash**: If a worker node is violently killed (`SIGKILL` or OOM), the job lease safely expires within `task_lease_seconds` (default and minimum: 300s; synchronous git calls can block a worker's heartbeat for up to 180s, so shorter leases could expire under healthy workers and are rejected at startup). The `reap_stale_jobs` mechanism automatically transitions the orphaned job back to `PENDING` and re-queues it for another worker.
- **Database Partition**: Asyncpg enforces `pool_pre_ping`. Stale socket exceptions trigger automatic recycling.
- **Redis Partition**: Dispatcher traps `ConnectionError`, preventing Postgres outbox marks from advancing. Dispatcher inherently retries the unacknowledged queue delivery continuously until Redis restores.

## Rollback Procedure
1. Re-deploy the previous functional Docker image tag.
2. The Database schema is designed for backward-compatible non-destructive roll-forwards. If a schema downgrade is strictly required, execute `alembic downgrade -1` manually using the prior codebase tag.

## Backup and Restore
CodeForge AI does **not** currently implement automated backups natively.
You are responsible for implementing a PostgreSQL backup strategy (e.g., `pg_dump` cron jobs, logical replication, or physical WAL archiving via tools like pgBackRest or AWS RDS snapshots).

- **Logical Backups**: Run `pg_dump -U codeforge -F c codeforge > backup.dump`.
- **Restore Verification**: Use `pg_restore` on a staging cluster to verify consistency.
- **Redis**: Redis is used strictly for transient queueing. It does not require backups. If Redis is wiped and restarted, the `outbox_dispatcher` will automatically repopulate unacknowledged events from Postgres.