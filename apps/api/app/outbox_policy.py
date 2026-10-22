"""How a failed outbox event is classified, and when it is attempted again.

Outbox events carry side effects that must outlast the transaction that
recorded them: enqueueing a job, turning an @codeforge comment into a task,
and mirroring a task's status to GitHub (acknowledgement comment, check run).
Delivery is at least once: an event is retried until it succeeds, fails
permanently, or runs out of retries, and every consumer is idempotent.

A failure is one of:

* ``transient``: GitHub 5xx, timeouts, network and database connection
  errors, Redis errors.  Retried with exponential backoff and jitter until
  ``outbox_retry_max_age_seconds`` have passed since the first failure.
* ``rate_limited``: GitHub's rate limit.  Retried like transient, but never
  before the time GitHub asked for (``retry-after``, at most an hour).
* ``permanent``: GitHub refused the request (401, 403, 404, 409, 422), the
  repository is no longer authorized, the event is malformed, its actor
  cannot be resolved, or its type is unknown.  Never retried.
* ``unknown``: anything else, most likely a defect.  Retried
  ``outbox_max_retries`` times in all, then given up.

The delay before attempt n+1 (n failed attempts so far) is
``min(max_delay, initial_delay * 2 ** (n - 1))``, multiplied by a random
factor in [0.5, 1.0] so that events failing together do not retry together.
With the defaults (2 s, doubling, at most 5 minutes) the delays are about
1-2, 2-4, 4-8 ... seconds, reaching 2.5-5 minutes from the eighth retry.

An event that is given up keeps its state (``failed_at``, attempts, the last
redacted error) for diagnosis; ``python -m app.outbox_admin`` reports the
queue and requeues a failed event.  Nothing about the event's task changes:
a status update that could not be delivered never marks a task as anything.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from app.config import settings
from app.observability.redaction import redact_secrets

MULTIPLIER = 2.0
JITTER_LOW = 0.5
MAX_RETRY_AFTER_SECONDS = 3600.0
ERROR_TEXT_LIMIT = 1000


class FailureCategory(StrEnum):
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Failure:
    category: FailureCategory
    # "<ExceptionType>: <message>", redacted and bounded.
    error: str
    # Seconds GitHub asked to wait (rate limits only).
    retry_after: float | None = None


def describe(exc: BaseException) -> str:
    """The exception as stored and logged: type and message, credentials redacted, bounded."""
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return redact_secrets(text)[:ERROR_TEXT_LIMIT]


def _transient_infrastructure(exc: BaseException) -> bool:
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError
    from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

    if isinstance(exc, OperationalError | InterfaceError | RedisConnectionError | RedisTimeoutError):
        return True
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    return isinstance(exc, TimeoutError | ConnectionError | OSError)


def classify(exc: BaseException) -> Failure:
    """The category of *exc* as an outbox delivery failure."""
    from app.github.exceptions import CommandConsumerError, GitHubError, GitHubRateLimitError
    from app.github.repository_access import RepositoryAccessError

    error = describe(exc)
    if isinstance(exc, GitHubRateLimitError):
        retry_after = float(exc.retry_after) if exc.retry_after is not None else None
        return Failure(FailureCategory.RATE_LIMITED, error, retry_after)
    if isinstance(exc, CommandConsumerError | PermanentDeliveryError):
        return Failure(FailureCategory.PERMANENT, error)
    if isinstance(exc, GitHubError | RepositoryAccessError):
        category = FailureCategory.TRANSIENT if exc.retryable else FailureCategory.PERMANENT
        return Failure(category, error)
    if _transient_infrastructure(exc):
        return Failure(FailureCategory.TRANSIENT, error)
    return Failure(FailureCategory.UNKNOWN, error)


class PermanentDeliveryError(Exception):
    """The event can never be delivered as it is (malformed, unknown type)."""


def retry_delay(attempts: int, *, retry_after: float | None = None,
                rng: Callable[[], float] = random.random) -> float:  # fmt: skip
    """Seconds to wait before the next attempt, after *attempts* failed ones (1 or more)."""
    initial = settings.outbox_retry_initial_delay_seconds
    maximum = max(settings.outbox_retry_max_delay_seconds, initial)
    base = min(maximum, initial * MULTIPLIER ** max(0, attempts - 1))
    delay = base * (JITTER_LOW + (1.0 - JITTER_LOW) * rng())  # nosec B311 - jitter, not security
    if retry_after is not None:
        delay = max(delay, min(retry_after, MAX_RETRY_AFTER_SECONDS))
    return delay


@dataclass(frozen=True)
class Decision:
    """What to do with an event after a failed attempt."""

    give_up: bool
    # Seconds until the next attempt, when not giving up.
    delay: float | None
    reason: str


def decide(failure: Failure, *, attempts: int, seconds_since_first_failure: float,
           rng: Callable[[], float] = random.random) -> Decision:  # fmt: skip
    """Give up or retry, after *attempts* failed attempts in all (this one included)."""
    if failure.category is FailureCategory.PERMANENT:
        return Decision(True, None, "permanent failure")
    if failure.category is FailureCategory.UNKNOWN and attempts >= settings.outbox_max_retries:
        return Decision(True, None, f"unclassified failure, {attempts} attempts")
    if seconds_since_first_failure >= settings.outbox_retry_max_age_seconds:
        return Decision(True, None, f"retries exhausted after {int(seconds_since_first_failure)} s")
    delay = retry_delay(attempts, retry_after=failure.retry_after, rng=rng)
    return Decision(False, delay, f"retry in {delay:.1f} s")
