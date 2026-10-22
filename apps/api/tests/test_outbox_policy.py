"""The outbox retry policy: classification, delays and when to give up (app/outbox_policy.py)."""

from __future__ import annotations

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.exc import IntegrityError, OperationalError

from app.config import settings
from app.github.exceptions import (
    GitHubAuthorizationError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubTimeoutError,
    GitHubUpstreamError,
    GitHubValidationError,
    InactiveUserError,
    MalformedCommandEventError,
)
from app.github.repository_access import RepositoryAccessError
from app.outbox_policy import (
    ERROR_TEXT_LIMIT,
    MAX_RETRY_AFTER_SECONDS,
    Failure,
    FailureCategory,
    PermanentDeliveryError,
    classify,
    decide,
    retry_delay,
)

T, R, P, U = (FailureCategory.TRANSIENT, FailureCategory.RATE_LIMITED, FailureCategory.PERMANENT,
              FailureCategory.UNKNOWN)  # fmt: skip


@pytest.mark.parametrize(
    ("exc", "category"),
    [
        (GitHubUpstreamError("502", status_code=502, retryable=True), T),
        (GitHubTimeoutError("timed out", retryable=True), T),
        (RepositoryAccessError("GitHub unavailable", retryable=True), T),
        (OperationalError("SELECT 1", {}, Exception("server closed the connection")), T),
        (RedisConnectionError("Connection refused"), T),
        (RedisTimeoutError("Timeout reading from socket"), T),
        (ConnectionResetError("reset"), T),
        (TimeoutError(), T),
        (GitHubRateLimitError("limit", status_code=403, retry_after=60), R),
        (GitHubAuthorizationError("forbidden", status_code=403), P),
        (GitHubNotFoundError("gone", status_code=404), P),
        (GitHubValidationError("invalid", status_code=422), P),
        (RepositoryAccessError("not authorized for this installation"), P),
        (MalformedCommandEventError("bad payload"), P),
        (InactiveUserError("inactive"), P),
        (PermanentDeliveryError("unknown type"), P),
        (KeyError("job_id"), U),
        (IntegrityError("INSERT", {}, Exception("duplicate key")), U),
        (ValueError("bug"), U),
    ],
    ids=lambda v: type(v).__name__ if isinstance(v, BaseException) else str(v),
)
def test_classification(exc: BaseException, category: FailureCategory) -> None:
    assert classify(exc).category is category


def test_the_rate_limit_wait_is_kept() -> None:
    assert classify(GitHubRateLimitError("limit", retry_after=120)).retry_after == 120
    assert classify(GitHubRateLimitError("limit")).retry_after is None


def test_the_stored_error_is_redacted_and_bounded() -> None:
    token = "ghs_" + "a" * 36
    failure = classify(RuntimeError(f"push to https://x-access-token:{token}@github.com/o/r failed " + "x" * 5000))
    assert token not in failure.error and failure.error.startswith("RuntimeError: ")
    assert len(failure.error) <= ERROR_TEXT_LIMIT


@pytest.mark.parametrize("attempts", range(1, 15))
def test_delays_double_up_to_the_maximum_with_jitter(attempts: int) -> None:
    initial, maximum = settings.outbox_retry_initial_delay_seconds, settings.outbox_retry_max_delay_seconds
    base = min(maximum, initial * 2 ** (attempts - 1))
    assert retry_delay(attempts, rng=lambda: 0.0) == pytest.approx(base / 2)
    assert retry_delay(attempts, rng=lambda: 1.0) == pytest.approx(base)


def test_the_defaults_are_the_documented_policy() -> None:
    assert settings.outbox_retry_initial_delay_seconds == 2.0
    assert settings.outbox_retry_max_delay_seconds == 300.0
    assert settings.outbox_retry_max_age_seconds == 24 * 3600
    assert settings.outbox_max_retries == 3


def test_a_rate_limit_waits_at_least_as_long_as_github_asks_but_at_most_an_hour() -> None:
    assert retry_delay(1, retry_after=600, rng=lambda: 1.0) == 600
    assert retry_delay(1, retry_after=10 * 3600, rng=lambda: 1.0) == MAX_RETRY_AFTER_SECONDS
    assert retry_delay(12, retry_after=1, rng=lambda: 1.0) == settings.outbox_retry_max_delay_seconds


def test_permanent_failures_are_given_up_at_once() -> None:
    decision = decide(Failure(P, "x"), attempts=1, seconds_since_first_failure=0)
    assert decision.give_up and decision.delay is None


def test_unclassified_failures_get_a_few_attempts() -> None:
    limit = settings.outbox_max_retries
    assert not decide(Failure(U, "x"), attempts=limit - 1, seconds_since_first_failure=1).give_up
    assert decide(Failure(U, "x"), attempts=limit, seconds_since_first_failure=1).give_up


@pytest.mark.parametrize("category", [T, R])
def test_transient_failures_are_retried_until_the_window_has_passed(category: FailureCategory) -> None:
    window = settings.outbox_retry_max_age_seconds
    many = decide(Failure(category, "x"), attempts=500, seconds_since_first_failure=window - 1, rng=lambda: 1.0)
    assert not many.give_up and many.delay == settings.outbox_retry_max_delay_seconds
    assert decide(Failure(category, "x"), attempts=2, seconds_since_first_failure=window).give_up
