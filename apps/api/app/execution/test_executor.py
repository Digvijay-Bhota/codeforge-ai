"""The single entry point for running repository-controlled code.

Every test run — the orchestrator's testing stage, the coding agent's
``run_tests`` tool and each repair attempt — goes through one
:class:`TestExecutor`.  Which executor is used is decided by
``CODEFORGE_TEST_EXECUTION_MODE`` (see :class:`app.config.ExecutionMode`):

* ``disabled`` (default) — nothing runs; tasks fail closed at testing.
* ``host_unsafe`` — :class:`~app.execution.host_executor.HostTestExecutor`,
  only with APP_ENV explicitly development/test, and never a security boundary.
* ``sandbox`` — an approved isolated runtime.  None is implemented yet, so this
  mode fails closed too.  There is no fallback to the host.

Executors never receive the trusted workspace.  :func:`run_tests_in_snapshot`
builds a :class:`~app.workspace.snapshot.WorkspaceSnapshot` (``.git``,
secrets and unsafe entries excluded) and the executor runs against that, so
nothing the tests do is copied back into the workspace the worker commits
from.  The snapshot is a content boundary, not OS-level isolation.

Executors report an :class:`IsolationLevel`.  Only ``SANDBOX`` permits
automatic repair attempts.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.config import ExecutionMode, settings
from app.schemas.task import TestResult
from app.workspace.snapshot import WorkspaceSnapshot, workspace_snapshot


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

    async def run(self, snapshot: WorkspaceSnapshot) -> TestResult:
        """Run the tests in *snapshot*.  Cancelling the awaiting task must stop the run.

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

    async def run(self, snapshot: WorkspaceSnapshot) -> TestResult:
        raise ExecutionUnavailableError(self.reason)


async def run_tests_in_snapshot(executor: TestExecutor, workspace_root: Path) -> TestResult:
    """Snapshot *workspace_root* and run *executor* against the snapshot.

    The only way repository tests are run.  Raises
    :class:`~app.workspace.snapshot.SnapshotError` if the workspace cannot be
    snapshotted; there is no fallback to the live workspace.
    """
    if isinstance(executor, UnavailableTestExecutor):
        # Fail closed without the cost of building a snapshot nobody will use.
        raise ExecutionUnavailableError(executor.reason)
    async with workspace_snapshot(workspace_root) as snapshot:
        return await executor.run(snapshot)


def get_test_executor() -> TestExecutor:
    """Return the executor for the configured execution mode."""
    mode = settings.codeforge_test_execution_mode
    if mode == ExecutionMode.HOST_UNSAFE:
        # Re-checked here so a mutated or bypassed Settings object can never
        # turn host execution into a production fallback.
        if not settings.allows_host_test_execution():
            return UnavailableTestExecutor(
                "Host test execution requires APP_ENV to be explicitly set to development or test."
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
