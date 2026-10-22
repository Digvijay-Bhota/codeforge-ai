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

## Activity history and the overview

`GET /api/v1/tasks/{id}/activity` returns what was recorded about one task,
oldest first, and nothing else (`app/services/task_overview.py`):

| Item | Source | Actor |
|---|---|---|
| `created` | `tasks.created_at`, `task_github_links` | the commenter, else the creator's GitHub login |
| `status` | `task_events` (every transition goes through `TaskStateMachine`) | none recorded |
| `approval_requested` | `task_approvals.requested_at` | none |
| `approval_decided` | `task_approvals.responded_at`, `status` | `approved_by`; none for `EXPIRED` |
| `retried` | tasks whose `parent_task_id` is this task | none |

Items are ordered by when they were recorded. A decision and the status
change it causes are written in one transaction: approving or rejecting stamps
both with the transaction's `now()`, and an expiry stamps the decision with
the clock a moment later. So each decision is placed just before the change
it caused (`WAITING_APPROVAL → QUEUED` or `→ CANCELLED`, `WAITING_PUBLICATION →
PUBLISHING` or `→ CANCELLED`) when that change was recorded within a minute of
it, and keeps its own recorded time. An expiry recorded after the task had
already moved on stays where it was recorded.

Messages and decision comments are redacted. Retries are listed only when the
viewer could read them (same repository and target; creator-only for local
tasks). The response marks history as **partial** when a task has no events
although it has left `PENDING`, or when its first event does not start from
`PENDING`; such tasks predate the events table and nothing is back-filled. It
marks it **truncated** past 200 status changes. Read access is the task page's
(404 when unreadable).

`GET /api/v1/tasks/summary` counts tasks in the same scope as the task pages
(`app/api/task_access.py`), in SQL: local tasks and tasks without a repository
for their creator only, other tasks for anyone who can read the repository.
Repository permissions are checked for at most 50 registered repositories of
active installations (the cached GitHub check, 5 minutes); beyond that the
response sets `repositories_truncated` and those tasks are not counted. A
repository GitHub cannot answer for is left out (access checks fail closed) and
the response sets `repositories_unconfirmed`; it never names such repositories,
since the viewer may have no access to them.

Cached answers are read in one Redis round trip, so a cached overview takes a
handful of queries whatever the number of repositories (7 SQL statements for
22, against 50 before). Unknown answers are never cached, so to keep an
outage from multiplying requests:

* each overview request asks GitHub at most twice;
* after GitHub fails to answer for an installation, no overview or "All I can
  read" list asks it about that installation's repositories for 30 seconds
  (`cf_overview_github_paused:<installation>` in Redis, shared by all users
  and tabs); those repositories count as unconfirmed meanwhile;
* the console never polls while its previous request is unanswered.

In a test with five consecutive overview requests during an outage, GitHub
was asked once (10 times before). Access decisions (task pages, approvals)
never use the pause: they ask GitHub, and refuse when it does not answer.

"Needs attention" is every `WAITING_APPROVAL` and `WAITING_PUBLICATION` task,
plus `FAILED` tasks that failed in the last 14 days (`completed_at`, which the
state machine sets; `updated_at` for older rows without it) and have no retry.
The list holds 20, decisions first; `needs_attention_total` is exact.

The task list (`GET /api/v1/tasks`) lists, without `repository`, only your
tasks by default; `scope=readable` lists every task you can read, in exactly
the overview's scope (the same SQL), with the same `repositories_truncated` and
`repositories_unconfirmed` flags. A test checks that every per-status count on
the overview equals the readable list's total for that status. A `repository`
filter takes precedence over `scope`. `status` accepts several comma-separated
statuses; an unknown status or scope is a `422`.

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
SELECT id, event_type, aggregate_id, created_at, published_at, attempts, next_attempt_at,
       failed_at, last_error_category, left(last_error, 120) AS last_error
  FROM outbox_events ORDER BY id DESC LIMIT 20;
SELECT delivery_id, event_type, repository, status, received_at
  FROM webhook_deliveries ORDER BY received_at DESC LIMIT 10;
```

Or, without SQL: `python -m app.outbox_admin status` and `list --failed` /
`list --scheduled` in the dispatcher container ([outbox.md](outbox.md)).

- No `webhook_deliveries` row: the comment never reached the API, or was
  refused before step 6. GitHub's *Recent Deliveries* shows the response body
  with the reason; smee relays show what they forwarded.
- A delivery without an outbox event cannot happen: both are written in one
  transaction, and a failure rolls both back (GitHub can then redeliver).
- An event with growing `attempts` and a `next_attempt_at`: it is being
  retried with backoff (transient failures for up to 24 hours). One failing
  event does not hold up any other.
- An event with `failed_at`: given up. Its `last_error_category` says why
  (`permanent`: GitHub refused or the event is malformed; `transient`: the
  outage outlasted the retry window). Fix the cause, then requeue it
  ([outbox.md](outbox.md#recovering-a-failed-event)).
- A GitHub command whose commenter never signed in to the console fails
  permanently (`UnresolvableIdentityError`): sign in, then requeue the event or
  comment again.

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
