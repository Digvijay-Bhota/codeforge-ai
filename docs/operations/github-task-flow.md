# From a GitHub comment to a finished task

How an `@codeforge` comment becomes a task, who decides what at each step,
and how to find out where a task is stuck. Everything here describes the
current code; file paths are under `apps/api/app/` unless stated.

## The path

```
GitHub ──issue_comment──▶ API  POST /api/v1/github/webhooks          (api/webhooks.py)
                          1. HMAC-SHA256 of the raw body with GITHUB_WEBHOOK_SECRET, else 401
                          2. action "created" only; comments carrying a CodeForge
                             managed-comment marker (its own acknowledgements) are ignored
                          3. a line starting @codeforge / /codeforge with a known action
                          4. installation active + repository registered and authorized
                          5. commenter has write access (GitHub collaborator permission)
                          6. one transaction: webhook_deliveries row (delivery ID unique:
                             a redelivery is a "duplicate") + outbox GITHUB_COMMAND_INGESTED
                          → 202 accepted (refusals answer 200 "ignored" with a reason)

outbox ──▶ dispatcher  (outbox_dispatcher.py; claims each event FOR UPDATE SKIP LOCKED)
           GITHUB_COMMAND_INGESTED → github/command_consumer.py, one transaction:
               commenter → CodeForge user (must have signed in once), task (PENDING,
               require_plan_approval), job, task_github_links row (one per comment),
               outbox JOB_CREATED + GITHUB_STATUS_UPDATE(COMMAND_ACCEPTED)
           JOB_CREATED          → job ID pushed onto the Redis queue
           GITHUB_STATUS_UPDATE → github/status_consumer.py: the acknowledgement comment
                                  (once), check runs (pull requests only)

Redis queue ──▶ worker  process_job (worker.py)
           claim the job (lease) → every GitHub task plans first, whatever it asks:
           verify the repository with the App's installation (repository ID must match the
           signed event) → read-only token → hardened clone → analysis → planner model →
           plan validation → WAITING_APPROVAL + a pending plan approval (expires, default 24 h)

reviewer ──▶ API  POST /tasks/{id}/approve {approval_id}   (write access, checked fresh)
           approval_id must be the latest request; the task row is locked, so a second
           approval returns the same result and creates no second job → QUEUED + new job

worker ──▶ execution gate: no permitted test executor → FAILED / EXECUTION_UNAVAILABLE,
           nothing written or run.  (With an executor: code, tests, then WAITING_PUBLICATION,
           which needs its own approval of the exact changes before anything is pushed.)
```

Telemetry (`observability_events`) is written in the same transaction as the
work it describes, so it is committed with it.

## Who may do what

| Step | Decided by | Where |
|---|---|---|
| Accept a delivery | Webhook secret (HMAC of the raw body) | `github/webhooks.py:verify_signature` |
| Start a task by comment | Installation active, repository registered for it, commenter has write access, commenter signed in once | `api/webhooks.py`, `github/command_consumer.py` |
| Read a task | GitHub read access to its repository (local tasks: creator only); otherwise 404 | `api/task_access.py` |
| Approve or reject a plan or a publication | GitHub write access, asked of GitHub at decision time (not the 5-minute cache) | `api/task_access.py` (`fresh=True`) |

Permission answers are cached in Redis for five minutes. Only GitHub's actual answers are cached: when GitHub cannot be asked (an outage, a timeout), the check fails closed without caching, and a decision answers `503` ("GitHub could not confirm your write access ... nothing was changed") rather than claiming the reviewer lacks access.
| Run generated code | A permitted test executor; disabled by default | `execution/test_executor.py`, `worker.py` |
| Push / open a pull request | A separate publication approval of the exact artifact digest, re-checked before pushing | `worker.py:_publish_approved_artifact` |

The console decides nothing: every check above is in the API or worker.

## Task lifecycle

`PENDING → QUEUED → ANALYZING → PLANNING → WAITING_APPROVAL → QUEUED → CODING →
TESTING → WAITING_PUBLICATION → PUBLISHING → COMPLETED`, with `FAILED` and
`CANCELLED` reachable from every non-terminal state
(`services/task_state_machine.py`). `COMPLETED`, `FAILED` and `CANCELLED` are
final; **Retry** creates a new task (`parent_task_id`), which plans again.

- Rejecting a plan, or its approval expiring, cancels the task.
- Approving a plan never approves publication.
- With execution disabled (the default, and always in the Compose stack), an
  approved GitHub plan ends `FAILED` with `EXECUTION_UNAVAILABLE`.
- A failed task stores `failure_code` and a redacted `failure_reason`. The
  codes, and what to do about each, are listed in `docs/deployment.md`
  ("Why a task failed"). A failed plan is never retried automatically.

## Diagnosing a task (read-only)

Against the database (for the isolated stack: `./dc.sh exec postgres psql -U codeforge -d codeforge`):

```sql
-- The task, its jobs and its approvals
SELECT task_id, status, failure_code, left(failure_reason, 200), created_at, updated_at
  FROM tasks WHERE task_id = '<id>';
SELECT id, status, attempt_count, worker_id, lease_version, available_at, last_error
  FROM jobs WHERE task_id = '<id>' ORDER BY id;
SELECT id, approval_type, status, approved_by, expires_at FROM task_approvals
  WHERE task_id = '<id>' ORDER BY id;
-- Its history, and its telemetry
SELECT created_at, from_status, to_status, left(message, 120) FROM task_events
  WHERE task_id = '<id>' ORDER BY id;
SELECT created_at, event_type, component, status, error_code FROM observability_events
  WHERE task_id = '<id>' ORDER BY id;
```

- **Stuck in QUEUED/PENDING:** a job `PENDING` but nothing working on it. Is
  the worker running, and is the job ID in Redis (`LRANGE codeforge:tasks 0 -1`, the
  `TASK_QUEUE_NAME` default)? Recovery re-enqueues pending jobs after a lease period.
- **Stuck in ANALYZING/PLANNING/CODING:** a job `RUNNING`. If its worker died,
  the lease expires (`TASK_LEASE_SECONDS`, at least 300 s) and recovery returns
  or fails the job (`JOB_INTERRUPTED`, `JOB_ATTEMPTS_EXHAUSTED`).
- **WAITING_APPROVAL:** waiting for a person; nothing runs.

## Missing or stuck outbox events

```sql
SELECT id, event_type, aggregate_id, created_at, published_at,
       payload->>'attempts' AS attempts, left(payload->>'last_error', 120) AS last_error,
       payload->>'terminal_failure' AS terminal
  FROM outbox_events ORDER BY id DESC LIMIT 20;
SELECT delivery_id, event_type, repository, status, received_at
  FROM webhook_deliveries ORDER BY received_at DESC LIMIT 10;
```

- No `webhook_deliveries` row: the comment never reached the API, or was
  refused before step 6. GitHub's *Recent Deliveries* shows the response body
  with the reason; smee relays show what they forwarded.
- A delivery without an outbox event cannot happen: both are written in one
  transaction, and a failure rolls both back (GitHub can then redeliver).
- Unpublished events with growing `attempts`: the dispatcher retries them up
  to `OUTBOX_MAX_RETRIES`, then marks them terminal (`terminal_failure`). One
  failing event does not hold up the rest of its batch.
- A GitHub command whose commenter never signed in to the console fails
  terminally (`UnresolvableIdentityError` in the dispatcher log): sign in,
  then comment again.

## Tests and what they need

| Tests | Needs |
|---|---|
| `apps/api/tests` (the whole suite) | PostgreSQL (`TEST_DATABASE_URL`/`DATABASE_URL`, wiped) and Redis (`REDIS_URL`) |
| `apps/api/tests/e2e` | Same; plus `git`. No network, keys or model credit |
| `tests/security/test_docker_executor_integration.py` | Docker and `CODEFORGE_IT_DOCKER_IMAGE` (skipped without; CI builds it) |
| `apps/web` (`npm test`) | Node only |

The end-to-end tests (`tests/e2e/`) run CodeForge's real code from the signed
webhook to `EXECUTION_UNAVAILABLE`. They replace only GitHub's REST API (a
local HTTP server reached through `GITHUB_API_URL`, `tests/e2e/fake_github.py`,
which can inject 4xx/5xx/429 faults), github.com's git transport (a local bare
repository) and OpenAI (the SDK's default client on an in-memory transport).
They cover refusals, duplicates, the self-acknowledgement guard, plan
approval and its refusals, GitHub and model failures, revoked reviewers and
webhook atomicity. They are not a substitute for a live check with a real
GitHub App and model.
