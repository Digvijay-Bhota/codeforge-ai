"""The single entry point for running repository-controlled code.

Every test run — the orchestrator's testing stage, the coding agent's
``run_tests`` tool and each repair attempt — goes through one
:class:`TestExecutor`.  Which executor is used is decided by
``CODEFORGE_TEST_EXECUTION_MODE`` (see :class:`app.config.ExecutionMode`):

* ``disabled`` (default) — nothing runs; tasks fail closed at testing.
* ``host_unsafe`` — :class:`~app.execution.host_executor.HostTestExecutor`,
  development/test only, and never a security boundary.
* ``sandbox`` — an approved isolated runtime.  None is implemented yet, so this
  mode fails closed too.  There is no fallback to the host.

Executors report an :class:`IsolationLevel`.  Only ``SANDBOX`` permits
automatic repair attempts.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.config import DEVELOPMENT_ENVIRONMENTS, ExecutionMode, settings
from app.schemas.task import TestResult


class IsolationLevel(str, Enum):
    """What an executor guarantees about where repository code runs."""

    # Runs with the worker's privileges, or does not run at all.
    NONE = "none"
    # Runs outside the worker's process and credential boundary.
    SANDBOX = "sandbox"


class ExecutionUnavailableError(Exception):
    """Raised when tests cannot be executed under the current configuration."""


@runtime_checkable
class TestExecutor(Protocol):
    """Runs a workspace's test suite and returns a bounded result."""

    @property
    def isolation(self) -> IsolationLevel: ...

    async def run(self, workspace_root: Path) -> TestResult:
        """Run the tests.  Cancelling the awaiting task must stop the run.

        Raises :class:`ExecutionUnavailableError` when execution is not possible.
        """
        ...


# Not a pytest test class.  Set after definition so it is not a protocol member.
TestExecutor.__test__ = False  # type: ignore[attr-defined]


class UnavailableTestExecutor:
    """Fails every run; used when no permitted executor is configured."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    @property
    def isolation(self) -> IsolationLevel:
        return IsolationLevel.NONE

    async def run(self, workspace_root: Path) -> TestResult:
        raise ExecutionUnavailableError(self.reason)


def get_test_executor() -> TestExecutor:
    """Return the executor for the configured execution mode."""
    mode = settings.codeforge_test_execution_mode
    if mode == ExecutionMode.HOST_UNSAFE:
        # Re-checked here so a mutated or bypassed Settings object can never
        # turn host execution into a production fallback.
        if settings.app_env not in DEVELOPMENT_ENVIRONMENTS:
            return UnavailableTestExecutor(
                "Host test execution is only allowed when APP_ENV is development or test."
            )
        from app.execution.host_executor import HostTestExecutor

        return HostTestExecutor()
    if mode == ExecutionMode.SANDBOX:
        return UnavailableTestExecutor(
            "Sandboxed test execution is configured but no sandbox runtime is available."
        )
    return UnavailableTestExecutor(
        "Test execution is disabled (CODEFORGE_TEST_EXECUTION_MODE=disabled)."
    )
