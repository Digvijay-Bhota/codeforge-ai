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
REDIS_URL=redis://:<redis-password>@your-redis:6379/0   # or rediss:// for TLS
OPENAI_API_KEY=sk-....
JWT_SECRET_KEY=<random, at least 32 characters>
GITHUB_APP_ID=12345
GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----..."
GITHUB_WEBHOOK_SECRET=your_secret
```

PostgreSQL and Redis must require authentication and must not be reachable from untrusted networks. Redis is security-relevant: it holds the job queue, OAuth login state and the cache of GitHub repository permissions that authorization decisions rely on, so anyone who can write to it can grant themselves repository access.

### GitHub App credentials
CodeForge reaches GitHub repositories only through GitHub App installation tokens (CF-06). There is no `GITHUB_TOKEN` setting; a `GITHUB_TOKEN` in the environment is ignored, and no operation falls back to a static or account-wide token.

- Each token request names exactly one repository (`repository_ids`) and an explicit permission set, so a token never covers the installation's other repositories or permissions. Tokens are cached in memory per installation, repository and permission set, and are not reused near expiry.
- A repository is usable only if it has an authorized row in `github_installation_repositories` for an **active** installation whose account owns the repository. CodeForge then establishes the repository's numeric ID: it requests a read-only metadata token limited to that repository name, and the repository GitHub reports granting (ID and full name) must match `GET /repos/{owner}/{repo}`. Token responses that do not report exactly one selected repository and its permissions are refused. The ID is verified on every use, never remembered, so a renamed or replaced repository is noticed at once. If any of this fails, the task fails before anything is cloned or changed.
- Tasks started by a GitHub comment are bound to the installation and repository ID in GitHub's signed webhook. Generation, publication, status updates and the commenter's permission check refuse a repository name that now resolves to a different installation or repository.
- Generation (clone, analysis, coding, tests) holds only a read-only token. A write token is minted only to publish changes a human approved (CF-05), after the approval and authorization re-checks.

The GitHub App must be granted these repository permissions (no others are needed):

| Permission | Access | Used for |
|---|---|---|
| Metadata | Read | Repository lookup (`GET /repos/{owner}/{repo}`), collaborator permission checks |
| Contents | Write | Read-only clone and branch reads during generation (tokens request read); branch creation (`POST /git/refs`) and push when publishing |
| Pull requests | Write | Finding and opening the pull request; reading pull requests for check runs (status tokens request read) |
| Checks | Write | Creating and updating the CodeForge check run |
| Issues | Write | The acknowledgement comment on issues and pull requests |

Each token requests only the subset its operation needs: generation `contents:read, metadata:read`; publication `contents:write, pull_requests:write`; status updates `checks:write, issues:write, pull_requests:read`; collaborator checks and repository lookup `metadata:read`. Sources: GitHub's [permissions required for GitHub Apps](https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps), [choosing permissions for Git access](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app) and [creating an installation access token](https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app). The Workflows permission is deliberately not requested: CodeForge never publishes files under `.github/`.

**Compatibility:** deployments that relied on `GITHUB_TOKEN` for cloning and publishing must configure the GitHub App above. GitHub tasks for repositories without an authorized, active installation now fail instead of using the static token.

### Review console (apps/web)
The review console is a static build (`apps/web`, `npm run build` → `dist/`). It must be served from the same origin as the API: `/api/` proxied to the API, every other unknown path falling back to `index.html`. It needs no CORS setting.
- Register `https://<console-host>/auth/callback` as the GitHub OAuth App callback and add it to `ALLOWED_OAUTH_REDIRECT_URIS`. The default list includes `http://localhost:3000/auth/callback` for the dev server.

### GitHub OAuth sign-in
- **Redirect URIs** must match an entry of `ALLOWED_OAUTH_REDIRECT_URIS`, or `GITHUB_OAUTH_REDIRECT_URI`, exactly: scheme, host, port and path, with no normalization (CF-08). There is no localhost wildcard, so list every local URL you use. `http://127.0.0.1:3000/...` is a different URL from `http://localhost:3000/...`.
- **Login state** is kept only in Redis, with a TTL of `OAUTH_STATE_TTL_SECONDS`. It is consumed with one atomic `GETDEL`, so a state is accepted at most once even if callbacks race (CF-13). This needs **Redis 6.2 or later**; Compose runs Redis 7. If Redis is unavailable, sign-in fails with `503` rather than falling back to an in-process store.
- Send the Content-Security-Policy from `apps/web/vite.config.ts` as a response header, plus `frame-ancestors 'none'`.
- The console holds no authority: approval and every other check happen in the API.

See `apps/web/README.md`.

### Local development with Docker Compose
`docker-compose.yml` is for **local development only**. It is not a production deployment configuration and does not replace production hardening: it runs with `APP_ENV=development`, which still accepts the public development JWT secret.

#### Local setup, step by step
1. **Create `.env` without overwriting one you already have:** `cp -n .env.example .env`. `.env` is gitignored.
2. **Set the secrets.** Generate each value separately with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and set `POSTGRES_PASSWORD`, `REDIS_PASSWORD` and `JWT_SECRET_KEY` in `.env`. The first two are required. `JWT_SECRET_KEY` signs your console sessions; if it is unset or left as the example's placeholder, the API uses its public development default, and anyone who can reach the API can forge a session.
3. **Optional: GitHub sign-in.** Create a GitHub OAuth App (GitHub → Settings → Developer settings → OAuth Apps) with homepage `http://localhost:3000` and authorization callback URL `http://localhost:3000/auth/callback`. Put its client ID and secret in `.env` as `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET`. The callback is already in the API's default `ALLOWED_OAUTH_REDIRECT_URIS`, and it must match exactly. Always open the console as `http://localhost:3000`: `127.0.0.1` is a different URL and is refused. Without these values, sign-in answers `503`.
4. **Start the stack:** `docker compose up --build -d`. On a first run, PostgreSQL initializes its data volume, which can take a minute.
5. **Check health:** `docker compose ps` should show `api`, `web`, `postgres` and `redis` as healthy and `compose-guard` as exited. Then check the endpoints:
   - `curl -fsS http://127.0.0.1:8000/health` (API liveness);
   - `curl -fsS http://127.0.0.1:8000/api/v1/ready` (API with PostgreSQL and Redis);
   - `curl -fsS http://127.0.0.1:3000/healthz` (console server);
   - `curl -fsS http://127.0.0.1:3000/api/v1/health` (the API through the console's `/api` proxy).
6. **Open the console** at `http://localhost:3000` and sign in with GitHub.

Stop with `docker compose down`. That keeps the database volume; never add `-v` unless you mean to delete your local data. The Vite dev server (`npm run dev` in `apps/web`) also uses port 3000, so stop the `web` service before using it.

#### Repository access: the GitHub App
Sign-in (the OAuth App above) and repository access (a GitHub **App**) are separate credentials. Without the App, you can sign in and use local tasks, but every repository permission check answers "none": repository tasks are hidden (404) and cannot be approved (403). The console says so on the task list.

1. **Create the App:** GitHub → Settings → Developer settings → GitHub Apps → New GitHub App, with the repository permissions in the table under "GitHub App credentials" above. Leave the webhook off for now (step 5).
2. **Generate a private key** on the App's page and download the `.pem` file.
3. **Install** the App on the account or organization that owns the repositories, and choose the repositories. Note the installation ID: it is the number at the end of the installation's settings URL.
4. **Set in `.env`:**
   - `GITHUB_APP_ID`: the numeric App ID, not the client ID.
   - `GITHUB_APP_PRIVATE_KEY`: the `.pem` contents, either double-quoted across several lines or on one line with `\n` between lines (both are accepted).

   Set both or neither. The API, worker and dispatcher refuse to start if only one is set, if the ID is not numeric, or if the key is not an unencrypted RSA PEM key. The error names the problem and never the value.
5. **Register the installation and each repository** CodeForge may use. Nothing creates these rows automatically, and only authorized rows on an active installation are usable:
   ```bash
   docker compose exec postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -h postgres -U codeforge -d codeforge' <<'SQL'
   INSERT INTO github_installations (installation_id, account_login, account_type, active)
     VALUES (12345678, 'your-org', 'Organization', true);
   INSERT INTO github_installation_repositories (installation_id, repository, authorized)
     VALUES (12345678, 'your-org/your-repo', true);
   SQL
   ```
   Use the real installation ID, the owner's login (exact case) and `owner/repo`. Run `docker compose exec api alembic upgrade head` first on a new database.
6. Restart with `docker compose up -d`, then check the configuration (below).

#### Tasks from GitHub comments
- **Webhook:** `@codeforge` comments arrive by webhook at `POST /api/v1/github/webhooks`. Set a random `GITHUB_WEBHOOK_SECRET` in `.env` and the same secret on the App, and enable the "Issue comment" event. GitHub needs a public HTTPS URL that reaches `127.0.0.1:8000`, such as a tunnel you run yourself; this stack exposes nothing publicly.
- **Who may trigger a task:** the commenter must have write access to the repository, and must have **signed in to the console once** so that a CodeForge user exists for their GitHub account. Otherwise the comment is refused.
- **Two approvals, in order.** Every GitHub task, whether started by a comment or through the API, follows this lifecycle:
  1. The worker **only plans**: it clones read-only and writes no code.
  2. The task waits in `WAITING_APPROVAL`.
  3. A reviewer with write access approves the exact plan shown (`POST /tasks/{id}/approve` with its `approval_id`), or rejects it, which cancels the task.
  4. Only an approved plan resumes, as one generation job. Rejected, expired, superseded or cancelled plans never run, and retrying a task plans again.
  5. The generated changes then wait for **publication approval** (`WAITING_PUBLICATION`) before anything is pushed. Approving the plan never approves publication.

  The worker enforces this for every GitHub task, whatever its `approval_config` says; a request's `require_plan_approval: false` is ignored for GitHub tasks.
- **The worker needs `OPENAI_API_KEY`** for the agent. The Compose stack passes it to the worker only.

#### Running tests: test execution
`CODEFORGE_TEST_EXECUTION_MODE` is not passed to the Compose services, so it stays `disabled`.
- **GitHub tasks** still plan and wait for approval. Once approved, the worker checks for a permitted test executor **before writing any code**. When there is none, the task stops as `FAILED` with code `EXECUTION_UNAVAILABLE` and a reason that names the setting; nothing ran. The console says so before you approve, and after.
- **Local tasks** fail at their testing stage.
- The `docker` and `gvisor` executors need the Docker CLI and daemon socket in the worker. Mounting the socket into a container gives that container root on the host, so this stack does not do it. Run the worker on a host prepared as in `docs/security/docker-test-executor.md` or `gvisor-test-executor.md` instead.
- `host_unsafe` runs repository code inside the worker with the worker's user ID, where it could read the worker's environment, including database and GitHub App credentials. It is not offered here.

#### Local tasks (optional)
`docker-compose.local-execution.yml` lets trusted accounts run local tasks (`execution_target=local`) against directories on this machine:
```bash
docker compose -f docker-compose.yml -f docker-compose.local-execution.yml up -d
```
- **Set in `.env`:** `CODEFORGE_HOST_WORKSPACE_ROOT` (an existing host directory, mounted at `/workspaces` in the api and worker) and `CODEFORGE_LOCAL_EXECUTION_GITHUB_IDS` (a JSON list of numeric GitHub user IDs, e.g. `[12345678]`; empty trusts nobody).
- **Workspaces** live in a real directory `<CODEFORGE_HOST_WORKSPACE_ROOT>/<github user id>/<name>` that you create.
- Signing in never grants local execution, and approving a plan does not publish anything. Tests stay disabled (see above), so local tasks stop at testing.

#### Checking the configuration
Each process logs one line at startup, for example `api configuration: github_oauth=True github_app=False github_webhooks=False local_execution=False ...`, and warns when the GitHub App is not configured. The worker also reports `agent_model_key` (`configured`, `placeholder` or `missing`) and `agent_tracing` (`CODEFORGE_OPENAI_TRACING`, `disabled` by default; see `docs/security/agent-tracing.md`). Signed-in users can read the API's view at `GET /api/v1/system/status`, which returns booleans and modes only, never values.

#### Why a task failed: failure codes
A failed task has a `failure_code` (shown in the console's task list and detail) and a `failure_reason` with the detail. Credentials in reasons (API keys, tokens, passwords in URLs, private keys) are redacted before they are stored or sent to GitHub. Failed tasks are not retried automatically; **Retry** in the console creates a new task.

| Code | Meaning | What to do |
|---|---|---|
| `REPOSITORY_ACQUISITION_FAILED` | The worker could not clone the repository. "git is not installed on this worker" means the worker image lacks `git` (rebuild it from `apps/api/Dockerfile`). | Check the reason; rebuild the image or fix repository access. |
| `REPOSITORY_RESOLUTION_FAILED`, `CREDENTIALS_FAILED` | GitHub did not return the repository or branch, or no repository-scoped App token was issued. | Check the App installation and its repository selection. |
| `GITHUB_UNAVAILABLE` | GitHub failed (5xx), timed out or rate-limited CodeForge while the worker verified the repository or obtained a token, after the client's own retries. Nothing ran. | Retry the task when GitHub is available. |
| `MODEL_QUOTA_EXCEEDED` | The model provider refused the request because the account has no credit or reached its billing limit (OpenAI `insufficient_quota`). Retrying does not help. | Add credit or raise the limit for the project behind `OPENAI_API_KEY`, then retry. |
| `MODEL_RATE_LIMITED` | Temporarily rate limited. | Retry later. |
| `MODEL_AUTHENTICATION_FAILED` | The API key was refused or lacks access to the model. | Replace `OPENAI_API_KEY` or grant the project access to `CODEFORGE_MODEL`. |
| `MODEL_UNAVAILABLE` | Network failure, timeout or a provider-side error. | Retry later. |
| `MODEL_REQUEST_REJECTED` | The provider rejected the request (for example an unknown `CODEFORGE_MODEL`). | Check `CODEFORGE_MODEL`. |
| `MODEL_OUTPUT_INVALID` | The model answered without a usable plan or result (malformed output, refusal, failed plan validation). | Retry, or rephrase the instructions. |
| `ANALYSIS_FAILED`, `PLANNING_FAILED`, `CODING_FAILED`, `CODING_TURN_LIMIT`, `NO_CHANGES`, `TESTS_FAILED`, `TEST_INFRASTRUCTURE_FAILED` | The named workflow stage failed for another reason. | See the reason. |
| `EXECUTION_UNAVAILABLE` | A plan was approved, but no permitted test executor exists. Nothing ran. | Expected while test execution is disabled. |
| `POLICY_REFUSED` | Refused before anything ran (local execution or repository access policy). | See the reason. |
| `PUBLICATION_FAILED` | Approved changes could not be published. | See the reason; retry. |
| `JOB_INTERRUPTED`, `JOB_ATTEMPTS_EXHAUSTED`, `WORKER_ERROR` | The job was interrupted, ran out of attempts, or the worker hit an unexpected error. | Check the worker log; retry. |

The model client itself retries a failed request twice (the OpenAI SDK default) before the task fails. Tasks that failed before these codes existed have no code; their reasons are still shown.

#### Container image dependencies
`apps/api/Dockerfile` builds one image for the API, worker and dispatcher. Besides the Python requirements it installs `curl` (the health check) and `git` (the worker clones repositories with it before planning). It runs as the unprivileged `codeforge` user. CI runs `git --version` inside the built worker image.

#### The `web` service
- `apps/web/Dockerfile` builds the console with `npm ci --ignore-scripts` from the committed lockfile, then serves `dist/` from an unprivileged nginx (`nginxinc/nginx-unprivileged`, uid 101, port 8080). There are no build arguments and no secrets in the image.
- `apps/web/nginx.conf`:
  - logs request paths without query strings or referrers, so OAuth codes and state are not written to its access log (see `docs/security/oauth-logging.md`);
  - forwards `/api/` to `api:8000` over the Compose network (same origin, no CORS), re-resolving the address so an API restart is followed;
  - falls back to `index.html` for client-side routes such as `/tasks/<id>` and `/auth/callback`; a missing `/assets/` file is a 404;
  - sends, on every response including errors and proxied API responses: the app's Content-Security-Policy plus `frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Cross-Origin-Opener-Policy: same-origin` and a restrictive `Permissions-Policy`. `apps/web/src/test/nginx.test.ts` keeps the CSP header identical to the bundle's own policy.
- Published only on `127.0.0.1:3000`. The container runs with a read-only filesystem (writable `/tmp` tmpfs), all capabilities dropped and `no-new-privileges`. It starts after the API is healthy. Its health check requires nginx to answer and to serve the app shell.
- The `api` service additionally receives `JWT_SECRET_KEY`, `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` from `.env`. The worker and dispatcher are unchanged.
- **Required before `docker compose up`:** `POSTGRES_PASSWORD` and `REDIS_PASSWORD`, set in a gitignored `.env` next to `docker-compose.yml`. Generate each one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. A one-shot `compose-guard` service checks both before PostgreSQL and Redis start, and refuses a value that is missing, shorter than 32 characters, contains anything other than URL-safe characters (`A-Z a-z 0-9 . _ ~ -`, because it is embedded in the services' connection URLs), has fewer than 8 distinct characters, or contains a placeholder word in any letter case (such as the old `codeforge` default, `password`, `change-me`, `example` or `secret`). These checks reject the common weak values, but they cannot prove a value is random: a predictable string that passes them is still weak, so always use the generator.
- **Network exposure:** PostgreSQL and Redis publish no host ports; the API, worker and dispatcher reach them over the internal Compose network. The API is published on `127.0.0.1:8000` and the console on `127.0.0.1:3000` only.
- **Redis authentication** is enforced by the Redis server (`requirepass`, loaded from a config file readable only by the `redis` user, never passed on a command line). Its health check authenticates, and every service's `REDIS_URL` carries the password.
- **PostgreSQL readiness** is checked by logging in with `POSTGRES_PASSWORD` over the container's network address (the image trusts loopback connections without a password, so a loopback check would prove nothing). If the configured password does not work, PostgreSQL stays unhealthy and the API, worker and dispatcher do not start.
- **Local debugging access:** `docker compose -f docker-compose.yml -f docker-compose.dev-ports.yml up` additionally publishes PostgreSQL on `127.0.0.1:5433` and Redis on `127.0.0.1:6379`, loopback only, still password-protected. The test suite's database fixture drops every table, so never point `TEST_DATABASE_URL` at a database whose data you need.
- **Existing local databases:** PostgreSQL applies `POSTGRES_PASSWORD` only when it initializes an empty data volume. An existing `postgres_data` volume keeps its old password, so PostgreSQL reports unhealthy (and the application services do not start) until you either change it interactively (`docker compose exec postgres psql -U codeforge -d codeforge`, then `\password codeforge`, which keeps the value out of shell history) or deliberately discard the local data with `docker compose down -v`.

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
1. `compose-guard` validates `POSTGRES_PASSWORD` and `REDIS_PASSWORD`; `postgres` and `redis` start only if it succeeds.
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