"""Development-only test execution on the worker host.

WARNING: this is NOT a security boundary.  Repository-controlled code
(pytest, conftest.py, plugins) runs under the worker's UID, in the worker's
PID namespace, with the worker's interpreter and site-packages.  It can read
``/proc/<pid>/environ`` of the worker and of later git processes, read any
file the worker can, open network connections, and leave ``setsid`` daemons
behind.  It must only be used for trusted repositories in development and
test, and it refuses to run unless APP_ENV is explicitly one of those.

What it does provide is hygiene, not isolation:

* a fixed command (pytest) — the agent cannot choose what runs;
* a fresh extraction of the workspace snapshot, deleted afterwards — the
  tests never run in, and nothing is copied back to, the workspace the worker
  commits from (though same-UID code can still reach it by absolute path);
* an environment built from an allow-list, not inherited;
* a dedicated process group, killed on timeout, on cancellation and after
  every run, so ordinary leftover children do not survive;
* bounded stdout/stderr, read concurrently so a noisy suite cannot deadlock;
* asynchronous execution, so the worker's lease heartbeat keeps running.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from pathlib import Path

from app.config import settings
from app.execution.test_executor import ExecutionUnavailableError, IsolationLevel
from app.schemas.task import TestResult
from app.workspace.snapshot import (
    WorkspaceSnapshot,
    extract_snapshot,
    private_tempdir,
    run_blocking,
)

logger = logging.getLogger(__name__)

# Maximum seconds to allow a test run before killing it.
DEFAULT_TIMEOUT: int = 120
# Per-stream cap on captured output.
MAX_OUTPUT_BYTES: int = 64 * 1024
# How long to wait for output pipes to close once the process group is dead.
_DRAIN_SECONDS: float = 5.0
_READ_CHUNK: int = 64 * 1024
# The only variables passed through from the worker's environment.
_ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")


class _BoundedBuffer:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    async def drain(self, stream: asyncio.StreamReader) -> None:
        # Keep reading past the limit so the child never blocks on a full pipe.
        while chunk := await stream.read(_READ_CHUNK):
            room = self.limit - len(self.data)
            if room > 0:
                self.data += chunk[:room]
            if len(chunk) > room:
                self.truncated = True

    def text(self) -> str:
        out = self.data.decode("utf-8", errors="replace")
        if self.truncated:
            out += f"\n... (output truncated at {self.limit} bytes)"
        return out


def _extract(archive_path: Path, target: Path) -> None:
    with open(archive_path, "rb") as archive:
        extract_snapshot(archive, target)


def _kill_process_group(pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


class HostTestExecutor:
    """Runs pytest on a snapshot as a host subprocess (development/test only)."""

    def __init__(
        self, timeout_seconds: int = DEFAULT_TIMEOUT, max_output_bytes: int = MAX_OUTPUT_BYTES
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes

    @property
    def isolation(self) -> IsolationLevel:
        return IsolationLevel.NONE

    async def run(self, snapshot: WorkspaceSnapshot) -> TestResult:
        if not settings.allows_host_test_execution():
            raise ExecutionUnavailableError(
                "Host test execution requires APP_ENV to be explicitly set to development or test."
            )
        with private_tempdir("codeforge-tests-") as work_dir:
            workspace_root = Path(work_dir)
            await run_blocking(_extract, snapshot.archive_path, workspace_root)
            return await self._run_pytest(workspace_root)

    async def _run_pytest(self, workspace_root: Path) -> TestResult:
        cmd = [sys.executable, "-m", "pytest", "--tb=short", "-q", "-p", "no:cacheprovider"]
        env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
        env["PYTHONPATH"] = str(workspace_root)
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        logger.info("HostTestExecutor: starting %s in %s", cmd, workspace_root)
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=workspace_root,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # own process group, so it can be killed as a unit
        )
        if proc.stdout is None or proc.stderr is None:  # pragma: no cover - PIPE requested
            _kill_process_group(proc.pid)
            raise ExecutionUnavailableError("Test process output pipes are unavailable.")
        out = _BoundedBuffer(self.max_output_bytes)
        err = _BoundedBuffer(self.max_output_bytes)
        readers = [
            asyncio.create_task(out.drain(proc.stdout)),
            asyncio.create_task(err.drain(proc.stderr)),
        ]

        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=self.timeout_seconds)
        except TimeoutError:
            timed_out = True
        except asyncio.CancelledError:
            _kill_process_group(proc.pid)
            for reader in readers:
                reader.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=_DRAIN_SECONDS)
            logger.warning("HostTestExecutor: cancelled; process group killed")
            raise
        finally:
            # Also after a normal exit: stray background children in the group.
            _kill_process_group(proc.pid)

        # Children that escaped the group (setsid) can hold the pipes open.
        _, pending = await asyncio.wait(readers, timeout=_DRAIN_SECONDS)
        for reader in pending:
            reader.cancel()
        exit_code = await proc.wait()
        duration = round(time.monotonic() - start, 3)

        if timed_out:
            logger.warning("HostTestExecutor: timed out after %ds", self.timeout_seconds)
            stderr = err.text()
            message = f"Test execution timed out after {self.timeout_seconds}s"
            return TestResult(
                passed=False,
                exit_code=-1,
                stdout=out.text(),
                stderr=f"{stderr}\n{message}" if stderr else message,
                duration_seconds=duration,
            )

        logger.info(
            "HostTestExecutor: finished in %.2fs exit_code=%d passed=%s",
            duration,
            exit_code,
            exit_code == 0,
        )
        return TestResult(
            passed=exit_code == 0,
            exit_code=exit_code,
            stdout=out.text(),
            stderr=err.text(),
            duration_seconds=duration,
        )
