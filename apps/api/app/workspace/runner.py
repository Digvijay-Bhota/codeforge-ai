"""Controlled test-runner for the coding agent.

Executes the repository test suite inside the workspace using a subprocess.
The agent cannot specify arbitrary shell commands — only the test invocation
(defaulting to pytest) is executed.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

from app.schemas.task import TestResult

logger = logging.getLogger(__name__)

# Maximum seconds to allow a test run before killing it.
DEFAULT_TIMEOUT: int = 120


class TestRunner:
    """Runs the repository test suite in a controlled subprocess."""

    def __init__(self, timeout_seconds: int = DEFAULT_TIMEOUT) -> None:
        self.timeout_seconds = timeout_seconds

    def run(
        self,
        workspace_root: Path,
        command: list[str] | None = None,
    ) -> TestResult:
        """Execute tests and return a structured :class:`TestResult`.

        Args:
            workspace_root: Absolute path to the workspace to test.
            command: Override the test command.  Defaults to
                     ``[sys.executable, "-m", "pytest", "--tb=short", "-q"]``.
        """
        cmd = command or [sys.executable, "-m", "pytest", "--tb=short", "-q"]
        logger.info("TestRunner: starting %s in %s", cmd, workspace_root)
        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=workspace_root,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
            duration = time.monotonic() - start
            passed = proc.returncode == 0
            logger.info(
                "TestRunner: finished in %.2fs exit_code=%d passed=%s",
                duration,
                proc.returncode,
                passed,
            )
            return TestResult(
                passed=passed,
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                duration_seconds=round(duration, 3),
            )
        except subprocess.TimeoutExpired:
            duration = time.monotonic() - start
            logger.warning("TestRunner: timed out after %ds", self.timeout_seconds)
            return TestResult(
                passed=False,
                exit_code=-1,
                stdout="",
                stderr=f"Test execution timed out after {self.timeout_seconds}s",
                duration_seconds=round(duration, 3),
            )
