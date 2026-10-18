"""Phase 10B.5 Stage 1A — development-only HostTestExecutor lifecycle.

HostTestExecutor runs repository tests on the worker host under the worker's
UID.  It is NOT a security boundary (see the module docstring); these tests
pin down the containment it does provide: refusal outside development/test,
process-group termination, bounded output, timeouts, cancellation and an
event loop that keeps running while tests execute.
"""

from __future__ import annotations

import ast
import asyncio
import io
import os
import sys
import time
from pathlib import Path

import pytest

from app.config import settings
from app.execution.host_executor import HostTestExecutor
from app.execution.test_executor import ExecutionUnavailableError, run_tests_in_snapshot


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); treat it as dead.  The process can also
    # be reaped after kill(0): opening its stat file then fails with ENOENT,
    # and reading an already-open one with ESRCH.  Either way it is gone.
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(") ", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


async def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.05)
    return not _alive(pid)


async def _wait_for_file(path: Path, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        await asyncio.sleep(0.05)
    raise AssertionError(f"{path} never written")


def _spawn_child_test(ws: Path, pidfile: Path, *, then: str) -> None:
    """A test that starts a long-lived child in the same process group."""
    (ws / "test_spawn.py").write_text(
        "import subprocess, time\n"
        "def test_spawn():\n"
        "    p = subprocess.Popen(['sleep', '300'])\n"
        f"    open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        f"    {then}\n"
    )


@pytest.fixture(autouse=True)
def _development_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_env", "development")


# ── The _alive() helper ────────────────────────────────────────────────────────
# Deterministic: os.kill and this module's open() are faked, so no real
# process has to disappear at exactly the right moment.

_FAKE_PID = 4_000_000


def _fake_process(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: str = "S",
    kill_error: BaseException | None = None,
    open_error: BaseException | None = None,
    read_error: BaseException | None = None,
) -> list[str]:
    opened: list[str] = []

    def fake_kill(pid: int, sig: int) -> None:
        assert (pid, sig) == (_FAKE_PID, 0)
        if kill_error is not None:
            raise kill_error

    class FakeStat(io.StringIO):
        def read(self, size: int | None = -1) -> str:
            if read_error is not None:
                raise read_error
            return super().read(size)

    def fake_open(path: str, *args: object, **kwargs: object) -> io.StringIO:
        assert path == f"/proc/{_FAKE_PID}/stat"
        opened.append(path)
        if open_error is not None:
            raise open_error
        return FakeStat(f"{_FAKE_PID} (python3) {state} 1 1 1 0 -1 4194560")

    monkeypatch.setattr(os, "kill", fake_kill)
    # Shadows the builtin for this module only (where _alive looks it up).
    monkeypatch.setattr(sys.modules[__name__], "open", fake_open, raising=False)
    return opened


@pytest.mark.parametrize("stage", ["open", "read"])
def test_alive_treats_a_process_reaped_after_kill_as_gone(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    # kill(0) succeeds, then the process is reaped: /proc/<pid>/stat raises ESRCH.
    error = ProcessLookupError(3, "No such process")
    _fake_process(
        monkeypatch,
        open_error=error if stage == "open" else None,
        read_error=error if stage == "read" else None,
    )
    assert _alive(_FAKE_PID) is False


def test_alive_treats_a_missing_stat_file_as_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_process(monkeypatch, open_error=FileNotFoundError(2, "No such file or directory"))
    assert _alive(_FAKE_PID) is False


def test_alive_is_false_when_kill_finds_no_process(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _fake_process(monkeypatch, kill_error=ProcessLookupError(3, "No such process"))
    assert _alive(_FAKE_PID) is False
    assert opened == []


@pytest.mark.parametrize("state,alive", [("S", True), ("R", True), ("Z", False)])
def test_alive_reads_the_process_state(
    monkeypatch: pytest.MonkeyPatch, state: str, alive: bool
) -> None:
    _fake_process(monkeypatch, state=state)
    assert _alive(_FAKE_PID) is alive


def test_alive_does_not_swallow_unrelated_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_process(monkeypatch, open_error=PermissionError(13, "Permission denied"))
    with pytest.raises(PermissionError):
        _alive(_FAKE_PID)


# ── Basic behaviour ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detects_passing_and_failing_tests(tmp_path: Path) -> None:
    ok, bad = tmp_path / "ok", tmp_path / "bad"
    ok.mkdir()
    bad.mkdir()
    (ok / "test_a.py").write_text("def test_ok(): assert True\n")
    (bad / "test_a.py").write_text("def test_bad(): assert False\n")
    executor = HostTestExecutor()

    passed = await run_tests_in_snapshot(executor, ok)
    failed = await run_tests_in_snapshot(executor, bad)

    assert passed.passed is True and passed.exit_code == 0
    assert failed.passed is False and failed.exit_code != 0
    assert "assert False" in failed.stdout + failed.stderr
    assert passed.duration_seconds >= 0


@pytest.mark.asyncio
async def test_does_not_write_pytest_cache_into_workspace(tmp_path: Path) -> None:
    (tmp_path / "test_a.py").write_text("def test_ok(): assert True\n")
    await run_tests_in_snapshot(HostTestExecutor(), tmp_path)
    assert not (tmp_path / ".pytest_cache").exists()


# ── Never a production fallback ────────────────────────────────────────────────


@pytest.mark.parametrize("env", ["production", "staging", ""])
@pytest.mark.asyncio
async def test_refuses_to_run_outside_development_and_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    marker = tmp_path / "ran"
    (tmp_path / "conftest.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    (tmp_path / "test_a.py").write_text("def test_ok(): assert True\n")
    monkeypatch.setattr(settings, "app_env", env)

    with pytest.raises(ExecutionUnavailableError, match="development"):
        await run_tests_in_snapshot(HostTestExecutor(), tmp_path)
    assert not marker.exists()


# ── Environment ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_worker_environment_is_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("OPENAI_API_KEY", "GITHUB_APP_PRIVATE_KEY", "DATABASE_URL", "CODEFORGE_CANARY"):
        monkeypatch.setenv(name, f"canary-{name}")
    dump = tmp_path / "env.txt"
    (tmp_path / "test_env.py").write_text(
        "import os\n"
        f"def test_env(): open({str(dump)!r}, 'w').write(repr((dict(os.environ), os.getcwd())))\n"
    )
    result = await run_tests_in_snapshot(HostTestExecutor(), tmp_path)
    assert result.passed, result.stdout + result.stderr
    environ, cwd = ast.literal_eval(dump.read_text())
    assert "canary-" not in repr(environ)
    # PYTHONPATH and cwd point at the snapshot extraction, not the workspace.
    assert environ["PYTHONPATH"] == cwd
    assert Path(cwd) != tmp_path and tmp_path not in Path(cwd).parents


# ── Timeout and process-group termination ─────────────────────────────────────


@pytest.mark.asyncio
async def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    _spawn_child_test(tmp_path, pidfile, then="time.sleep(300)")

    start = time.monotonic()
    result = await run_tests_in_snapshot(HostTestExecutor(timeout_seconds=3), tmp_path)

    assert time.monotonic() - start < 15
    assert result.passed is False
    assert result.exit_code == -1
    assert "timed out" in result.stderr
    assert await _wait_dead(int(pidfile.read_text()))


@pytest.mark.asyncio
async def test_leftover_children_are_killed_after_normal_exit(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    _spawn_child_test(tmp_path, pidfile, then="assert True")

    result = await run_tests_in_snapshot(HostTestExecutor(), tmp_path)

    assert result.passed, result.stdout + result.stderr
    assert await _wait_dead(int(pidfile.read_text()))


@pytest.mark.asyncio
async def test_cancellation_kills_the_whole_process_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    _spawn_child_test(tmp_path, pidfile, then="time.sleep(300)")

    task = asyncio.create_task(run_tests_in_snapshot(HostTestExecutor(timeout_seconds=120), tmp_path))
    child = int(await _wait_for_file(pidfile))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await _wait_dead(child)


# ── Bounded output ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_output_is_bounded_without_deadlock(tmp_path: Path) -> None:
    # pytest captures output inside tests, so emit it at exit, after capture ends.
    (tmp_path / "test_noisy.py").write_text(
        "import atexit, os\n"
        "def _noise():\n"
        "    for fd, ch in ((1, b'O'), (2, b'E')):\n"
        "        for _ in range(32):\n"
        "            data = ch * 65536\n"
        "            while data:\n"
        "                data = data[os.write(fd, data):]\n"
        "atexit.register(_noise)\n"
        "def test_noisy(): pass\n"
    )
    executor = HostTestExecutor(timeout_seconds=60, max_output_bytes=10_000)

    result = await run_tests_in_snapshot(executor, tmp_path)

    for stream in (result.stdout, result.stderr):
        assert len(stream.encode()) <= 10_000 + 200
        assert "output truncated" in stream


@pytest.mark.asyncio
async def test_output_with_invalid_utf8_is_decoded_safely(tmp_path: Path) -> None:
    (tmp_path / "test_bytes.py").write_text(
        "import atexit, os\n"
        "atexit.register(lambda: os.write(1, b'\\xff\\xfe bad bytes'))\n"
        "def test_bytes(): pass\n"
    )
    result = await run_tests_in_snapshot(HostTestExecutor(), tmp_path)
    assert "bad bytes" in result.stdout


# ── Does not block the event loop ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_event_loop_keeps_running_during_tests(tmp_path: Path) -> None:
    """The worker heartbeat must be able to renew the lease while tests run."""
    (tmp_path / "test_slow.py").write_text("import time\ndef test_slow(): time.sleep(2)\n")
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.1)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        result = await run_tests_in_snapshot(HostTestExecutor(), tmp_path)
    finally:
        beat.cancel()
    assert result.passed
    assert ticks >= 10
