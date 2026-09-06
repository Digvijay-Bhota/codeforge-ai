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

### Required Production Settings
```bash
APP_ENV=production
DATABASE_URL=postgresql+asyncpg://codeforge:secret@your-postgres:5432/codeforge
REDIS_URL=redis://your-redis:6379/0
OPENAI_API_KEY=sk-....
GITHUB_APP_ID=12345
GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----..."
GITHUB_WEBHOOK_SECRET=your_secret
```

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
- **Worker Crash**: If a worker node is violently killed (`SIGKILL` or OOM), the job lease safely expires within `task_lease_seconds` (default: 300s). The `reap_stale_jobs` mechanism automatically transitions the orphaned job back to `PENDING` and re-queues it for another worker.
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