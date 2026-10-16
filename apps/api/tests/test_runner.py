"""Unit tests for HostTestExecutor (the former TestRunner).

No LLM calls.  Uses small synthetic test files executed via subprocess.
Lifecycle and isolation-related cases live in tests/security/test_host_executor.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.execution.host_executor import HostTestExecutor
from app.execution.test_executor import run_tests_in_snapshot


@pytest.fixture(autouse=True)
def _development_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Host execution requires APP_ENV to be explicitly development or test.
    monkeypatch.setattr(settings, "app_env", "development")


@pytest.fixture()
def passing_repo(tmp_path: Path) -> Path:
    """A minimal workspace whose tests pass."""
    (tmp_path / "test_pass.py").write_text("def test_ok(): assert True\n")
    return tmp_path


@pytest.fixture()
def failing_repo(tmp_path: Path) -> Path:
    """A minimal workspace whose tests fail."""
    (tmp_path / "test_fail.py").write_text("def test_bad(): assert False\n")
    return tmp_path


# ── Basic pass/fail ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_runner_detects_passing_tests(passing_repo: Path) -> None:
    result = await run_tests_in_snapshot(HostTestExecutor(), passing_repo)
    assert result.passed is True
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_runner_detects_failing_tests(failing_repo: Path) -> None:
    result = await run_tests_in_snapshot(HostTestExecutor(), failing_repo)
    assert result.passed is False
    assert result.exit_code != 0


# ── Output capture ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_runner_captures_stdout(passing_repo: Path) -> None:
    result = await run_tests_in_snapshot(HostTestExecutor(), passing_repo)
    # pytest -q outputs a summary line
    assert isinstance(result.stdout, str)


@pytest.mark.asyncio
async def test_runner_captures_failure_output(failing_repo: Path) -> None:
    result = await run_tests_in_snapshot(HostTestExecutor(), failing_repo)
    combined = result.stdout + result.stderr
    assert "FAILED" in combined or "assert False" in combined


# ── Duration ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_runner_records_duration(passing_repo: Path) -> None:
    result = await run_tests_in_snapshot(HostTestExecutor(), passing_repo)
    assert result.duration_seconds >= 0.0


# ── Timeout ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_runner_handles_timeout(tmp_path: Path) -> None:
    """A test that sleeps longer than the timeout should be killed."""
    (tmp_path / "test_slow.py").write_text(
        "import time\ndef test_slow(): time.sleep(60)\n"
    )
    result = await run_tests_in_snapshot(HostTestExecutor(timeout_seconds=2), tmp_path)
    assert result.passed is False
    assert "timed out" in result.stderr
    assert result.exit_code == -1
