"""Task failure codes for workflow stages, and classification of model errors.

A failed task stores a short machine-readable ``failure_code`` next to its
human-readable ``failure_reason``.  The console explains each code; the reason
carries the detail.  GitHub workflow failures use ``GitHubFailureStage`` values
(``app.schemas.task``), and the worker writes its own job-level codes
(``EXECUTION_UNAVAILABLE``, ``JOB_INTERRUPTED``, ...).  The codes here cover the
orchestrator's stages and the model provider.

Model errors are classified from the exception the SDK raised, never from its
message text, so a changed wording cannot reclassify a failure.
"""

from __future__ import annotations

import openai

from agents.exceptions import (
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelRefusalError,
    ModelTimeoutError,
)

# ── Model provider ──────────────────────────────────────────────────────────
# The account has no credit or hit its billing limit.  Retrying cannot help
# until someone changes the account's billing.
MODEL_QUOTA_EXCEEDED = "MODEL_QUOTA_EXCEEDED"
# Temporarily rate limited; a later retry may succeed.
MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
# The API key was refused (invalid, revoked, or lacking access to the model).
MODEL_AUTHENTICATION_FAILED = "MODEL_AUTHENTICATION_FAILED"
# Network failure, timeout, or a provider-side (5xx) error.
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
# The provider rejected the request itself (unknown model, invalid request).
MODEL_REQUEST_REJECTED = "MODEL_REQUEST_REJECTED"
# The model answered, but not with a usable result (malformed or unexpected
# output, a refusal, or a plan that failed validation).
MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"

# ── Workflow stages (when no more specific code applies) ─────────────────────
ANALYSIS_FAILED = "ANALYSIS_FAILED"
PLANNING_FAILED = "PLANNING_FAILED"
CODING_FAILED = "CODING_FAILED"
CODING_TURN_LIMIT = "CODING_TURN_LIMIT"
NO_CHANGES = "NO_CHANGES"
TESTS_FAILED = "TESTS_FAILED"
TEST_INFRASTRUCTURE_FAILED = "TEST_INFRASTRUCTURE_FAILED"
# The configuration does not permit the requested run (nothing ran).
EXECUTION_UNAVAILABLE = "EXECUTION_UNAVAILABLE"
# Refused by policy before anything ran (local execution, repository access).
POLICY_REFUSED = "POLICY_REFUSED"
# GitHub could not be reached, failed (5xx), timed out or rate-limited us
# before anything ran.  A later retry may succeed; nothing is retried
# automatically.
GITHUB_UNAVAILABLE = "GITHUB_UNAVAILABLE"

# OpenAI error ``type``/``code`` values that mean "no quota left", as opposed
# to a temporary rate limit.  Both arrive as HTTP 429.
_QUOTA_ERRORS = frozenset({
    "insufficient_quota",
    "credit_balance_exhausted",
    "billing_hard_limit_reached",
    "billing_not_active",
})

# How far down the ``__cause__``/``__context__`` chain to look.
_MAX_CHAIN = 8


def _chain(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(seen) < _MAX_CHAIN:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def classify_model_error(exc: BaseException) -> str | None:
    """Return the model failure code for *exc* (or its causes), or None if it is not a model error."""
    for err in _chain(exc):
        if isinstance(err, openai.RateLimitError):
            if {getattr(err, "type", None), getattr(err, "code", None)} & _QUOTA_ERRORS:
                return MODEL_QUOTA_EXCEEDED
            return MODEL_RATE_LIMITED
        if isinstance(err, openai.AuthenticationError | openai.PermissionDeniedError):
            return MODEL_AUTHENTICATION_FAILED
        # APITimeoutError is an APIConnectionError.
        if isinstance(err, openai.APIConnectionError | ModelTimeoutError):
            return MODEL_UNAVAILABLE
        if isinstance(err, openai.APIStatusError):
            return MODEL_UNAVAILABLE if err.status_code >= 500 else MODEL_REQUEST_REJECTED
        if isinstance(err, ModelBehaviorError | ModelRefusalError | MaxTurnsExceeded):
            return MODEL_OUTPUT_INVALID
    return None
