# Outbox delivery, retries and recovery

Some side effects must happen *after* the transaction that decided them, and
must not be lost if a process dies or GitHub is down. They are written as
outbox events in that transaction (`outbox_events`), and the dispatcher
(`apps/api/app/outbox_dispatcher.py`, the `dispatcher` service) delivers them.

| Event | Delivery does | If it can never be delivered |
|---|---|---|
| `GITHUB_COMMAND_INGESTED` | turns an `@codeforge` comment into a task, its job and the GitHub status event (one transaction) | the command is not acted on; the comment author sees no acknowledgement |
| `JOB_CREATED` | puts the job on the Redis queue | the worker's recovery pass re-enqueues pending jobs anyway |
| `GITHUB_STATUS_UPDATE` | posts or updates the acknowledgement comment and the `CodeForge AI` check run (pull requests) | GitHub shows a missing or stale status; the task itself is unaffected |

## Guarantees

* **Delivery is at least once.** An event is retried until it succeeds,
  fails permanently, or runs out of retries. It can be attempted more than
  once, for example when a dispatcher dies after the side effect but before
  recording it.
* **Processing is idempotent**, so a repeated attempt does not repeat the
  effect:
  * a command creates its task once, keyed by repository and comment;
  * a job runs once: the worker claims it with a lease, and a second enqueue
    of a running or finished job is refused;
  * the acknowledgement comment is found by its hidden marker and updated, not
    posted again;
  * the check run is found on GitHub before one is created (see below).
* **One event at a time per row.** Dispatchers claim events with
  `SELECT … FOR UPDATE SKIP LOCKED`, again just before handling each one, so
  two dispatchers never handle the same event at once.
* **A failure blocks nothing else.** A failed event is rescheduled; other
  events are delivered in the meantime.
* **A status that could not be delivered never changes the task.** It is
  retried, or recorded as failed. It is never reported as delivered.

### Check runs and comments without duplicates

GitHub does not make a check run's `external_id` unique, and a create request
can succeed on GitHub while its response is lost (a 502 from GitHub's edge, a
timeout, a dropped connection). Two rules prevent duplicates:

1. **The GitHub client never repeats a creating `POST` after an ambiguous
   failure.** That covers check runs, comments, branches and pull requests,
   after a 5xx, a read timeout or a broken connection. It raises a retryable
   error instead, and the outbox retries the whole operation later. It does
   repeat a `POST` that certainly never reached GitHub (connection refused,
   connect timeout) or that GitHub rejected (rate limit). Reads and updates
   (`GET`, `PATCH`) are retried as before.
2. **The status consumer reconciles before creating.** If the task has no
   recorded check run, it first lists `CodeForge AI` runs on the commit by this
   GitHub App (`GET /repos/{owner}/{repo}/commits/{sha}/check-runs?check_name=…&app_id=…&filter=all`).
   If one carries the task ID as `external_id`, it adopts that run (the oldest,
   if there are several), records it, and updates it. An adoption is logged and
   audited as `GITHUB_CHECK_RUN_ADOPTED`.

   The acknowledgement comment is found the same way, by its marker. The
   search reads the thread from 10 minutes before the task was created, up to
   10 pages of 100 comments, so a long thread is not read from its start.

Publication (branch, push, pull request) already reconciled on its own retries:
an existing branch is reused, and an open pull request for the branch is found
before one is created.

## Retry policy

Every failed attempt is classified (`apps/api/app/outbox_policy.py`):

| Category | Examples | Retried |
|---|---|---|
| `transient` | GitHub 5xx, timeouts, network errors; database connection errors; Redis unreachable | yes, until the retry window has passed |
| `rate_limited` | GitHub's rate limit | yes, never before GitHub's `retry-after` (at most an hour) |
| `permanent` | GitHub 401, 403, 404, 409, 422; repository no longer authorized; malformed event; commenter cannot be resolved or is inactive; unknown event type | no: failed at once |
| `unknown` | anything else (most likely a defect) | `OUTBOX_MAX_RETRIES` attempts in all |

Delays, with the defaults:

| Setting | Default | Meaning |
|---|---|---|
| `OUTBOX_RETRY_INITIAL_DELAY_SECONDS` | `2` | delay after the first failure |
| (multiplier) | `2` | each further delay doubles |
| `OUTBOX_RETRY_MAX_DELAY_SECONDS` | `300` | the delay never exceeds 5 minutes |
| (jitter) | ×0.5–1.0 | each delay is between half and all of the above, so events that failed together do not retry together |
| `OUTBOX_RETRY_MAX_AGE_SECONDS` | `86400` | transient and rate-limited failures are retried for 24 hours from the first failure |
| `OUTBOX_MAX_RETRIES` | `3` | attempts for `unknown` failures |

So the delays run about 1–2 s, 2–4 s, 4–8 s and so on, reaching 2.5–5 minutes
from the eighth retry, and a GitHub outage of up to 24 hours loses nothing.
Before this policy there was no delay and only three attempts, so an outage of
about 15 seconds lost pending status updates.

The schedule is kept in the database, so it survives dispatcher restarts:

| Column | Meaning |
|---|---|
| `attempts` | failed attempts since creation or the last requeue |
| `next_attempt_at` | not attempted before this time |
| `first_failed_at` | start of the retry window |
| `last_failed_at`, `last_error`, `last_error_category` | the most recent failure; the error is redacted and at most 1,000 characters, never payload contents |
| `failed_at` | the event was given up |
| `published_at` | the event was delivered |

## Inspecting the queue

All read-only:

```bash
# Inside the API or dispatcher container (it has the database credentials):
docker compose exec dispatcher python -m app.outbox_admin status
docker compose exec dispatcher python -m app.outbox_admin list --failed
docker compose exec dispatcher python -m app.outbox_admin list --scheduled --limit 20
# --json for machine-readable output.  For the isolated test stack: ./dc.sh exec dispatcher ...
```

`status` reports:

* events due now and events scheduled for a retry;
* failed events, by type and category;
* the age of the oldest pending event;
* when the next retry is due.

`list` shows events by id, type and the id they concern (a task, a job or a
webhook delivery), with attempts, category, times and the last error. For
status events it also shows whether the task's check run is already recorded.
If it is, a retry updates that run.

The tool never prints payloads, repository names or credentials, and it
deletes nothing.

Equivalent SQL, if the tool is unavailable:

```sql
SELECT count(*) FILTER (WHERE published_at IS NULL AND failed_at IS NULL
                         AND (next_attempt_at IS NULL OR next_attempt_at <= now())) AS due_now,
       count(*) FILTER (WHERE published_at IS NULL AND failed_at IS NULL AND next_attempt_at > now()) AS scheduled,
       count(*) FILTER (WHERE failed_at IS NOT NULL) AS failed,
       now() - min(created_at) FILTER (WHERE published_at IS NULL AND failed_at IS NULL) AS oldest_pending_age
  FROM outbox_events;
SELECT id, event_type, aggregate_id, attempts, last_error_category, next_attempt_at, failed_at,
       left(last_error, 160) AS last_error
  FROM outbox_events WHERE failed_at IS NOT NULL OR next_attempt_at > now()
 ORDER BY id DESC LIMIT 20;
```

Metrics:

* `codeforge_outbox_retries_scheduled_total{event_type,category}`
* `codeforge_outbox_events_failed_total{event_type,category}`

## Recovering a failed event

1. Find out why it failed: `list --failed`, then the dispatcher log for that
   event id. The category says what kind of fix is needed:
   * `permanent`: something must change first, for example the App's
     permissions, the repository's authorization, or the commenter signing in;
   * `transient`: the outage outlasted the 24-hour window;
   * `unknown`: probably a defect, so fix it first.
2. Fix the cause.
3. Requeue:

   ```bash
   docker compose exec dispatcher python -m app.outbox_admin requeue 42 --by alice --reason "checks permission restored"
   ```

   The event becomes due at once with a fresh retry budget. Its last error is
   kept until a new attempt replaces it. The requeue is audited
   (`OUTBOX_EVENT_REQUEUED`, with who and why). Only failed events can be
   requeued; a delivered or pending event is left unchanged.

Requeueing a status event is safe at any time: it reuses the existing check
run and acknowledgement comment. Requeueing a command creates its task only if
none exists yet for that comment.

Never delete outbox rows, or reset `published_at`, to "retry" an event: that
bypasses the idempotency checks above and loses the record.

## Migration

`e1a2b3c4d5f6` adds the columns above. Events that had been given up under the
old scheme (published, with `payload.terminal_failure`) become failed events,
so they show in `list --failed` and can be requeued:

* those that ran out of attempts get category `transient`;
* the others get `permanent`.

Payloads are not changed. A downgrade marks failed events published again,
which was the old scheme's terminal state.
