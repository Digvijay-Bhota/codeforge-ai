"""Unit tests for TestRunner.

No LLM calls.  Uses small synthetic test files executed via subprocess.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.workspace.runner import TestRunner


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


def test_runner_detects_passing_tests(passing_repo: Path) -> None:
    runner = TestRunner()
    result = runner.run(passing_repo)
    assert result.passed is True
    assert result.exit_code == 0


def test_runner_detects_failing_tests(failing_repo: Path) -> None:
    runner = TestRunner()
    result = runner.run(failing_repo)
    assert result.passed is False
    assert result.exit_code != 0


# ── Output capture ────────────────────────────────────────────────────────────


def test_runner_captures_stdout(passing_repo: Path) -> None:
    runner = TestRunner()
    result = runner.run(passing_repo)
    # pytest -q outputs a summary line
    assert isinstance(result.stdout, str)


def test_runner_captures_failure_output(failing_repo: Path) -> None:
    runner = TestRunner()
    result = runner.run(failing_repo)
    combined = result.stdout + result.stderr
    assert "FAILED" in combined or "assert False" in combined


# ── Duration ──────────────────────────────────────────────────────────────────


def test_runner_records_duration(passing_repo: Path) -> None:
    runner = TestRunner()
    result = runner.run(passing_repo)
    assert result.duration_seconds >= 0.0


# ── Timeout ───────────────────────────────────────────────────────────────────


def test_runner_handles_timeout(tmp_path: Path) -> None:
    """A test that sleeps longer than the timeout should be killed."""
    (tmp_path / "test_slow.py").write_text(
        "import time\ndef test_slow(): time.sleep(60)\n"
    )
    runner = TestRunner(timeout_seconds=2)
    result = runner.run(tmp_path)
    assert result.passed is False
    assert "timed out" in result.stderr
    assert result.exit_code == -1


# ── Custom command ────────────────────────────────────────────────────────────
