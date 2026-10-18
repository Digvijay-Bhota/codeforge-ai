"""Phase 10B.5 Stage 1C — DockerTestExecutor against a real Docker daemon.

Runs only when ``CODEFORGE_IT_DOCKER_IMAGE`` names an image built from
``sandbox/docker`` (an image ID or name@sha256 digest) and the docker CLI can
reach a daemon; otherwise every test is skipped.  With
``CODEFORGE_IT_DOCKER_REQUIRED=1`` (CI) a missing image or daemon is a failure
instead of a skip, so these tests cannot silently stop running.

    docker build -t codeforge-test-runner:dev sandbox/docker
    CODEFORGE_IT_DOCKER_IMAGE=$(docker image inspect -f '{{.Id}}' codeforge-test-runner:dev) \\
        pytest apps/api/tests/security/test_docker_executor_integration.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess  # nosec B404 - queries the local docker daemon in tests
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from app.execution.docker_executor import (
    DEADLINE_EXIT,
    MANAGED_LABEL_KEY,
    MANAGED_LABEL_VALUE,
    NAME_PREFIX,
    DockerExecutorConfig,
    DockerTestExecutor,
    build_create_args,
)
from app.execution.ownership import OwnershipLostError, run_until_ownership_lost
from app.execution.test_executor import ExecutionUnavailableError, run_tests_in_snapshot
from app.workspace.snapshot import workspace_snapshot

IMAGE = os.environ.get("CODEFORGE_IT_DOCKER_IMAGE", "")
REQUIRED = os.environ.get("CODEFORGE_IT_DOCKER_REQUIRED") == "1"


def _unavailable_reason() -> str | None:
    if not IMAGE:
        return "CODEFORGE_IT_DOCKER_IMAGE is not set"
    docker = shutil.which("docker")
    if docker is None:
        return "docker CLI not found"
    probe = subprocess.run(  # nosec B603 - fixed arguments
        [docker, "image", "inspect", IMAGE], capture_output=True, timeout=60, check=False
    )
    if probe.returncode != 0:
        return f"image {IMAGE} is not available: {probe.stderr.decode(errors='replace')[:200]}"
    return None


_REASON = _unavailable_reason()
if _REASON and REQUIRED:
    raise RuntimeError(f"Docker integration tests are required but cannot run: {_REASON}")
pytestmark = pytest.mark.skipif(_REASON is not None, reason=_REASON or "")


def _docker(*args: str) -> str:
    docker = shutil.which("docker")
    assert docker
    return subprocess.run(  # nosec B603 - fixed arguments
        [docker, *args], capture_output=True, text=True, timeout=60, check=True
    ).stdout


def _managed() -> set[str]:
    out = _docker("ps", "-aq", "--no-trunc", "--filter", f"label={MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}")
    return set(out.split())


def _running(ids: set[str]) -> set[str]:
    return {i for i in ids if _docker("inspect", "-f", "{{.State.Running}}", i).strip() == "true"}


async def _wait_for_new(before: set[str], count: int = 1, timeout: float = 120) -> set[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        new = _running(_managed() - before)
        if len(new) >= count:
            return new
        await asyncio.sleep(0.2)
    raise AssertionError("test container never started")


def _executor(**overrides: Any) -> DockerTestExecutor:
    values: dict[str, Any] = {"image": IMAGE, "timeout_seconds": 120}
    values.update(overrides)
    return DockerTestExecutor(DockerExecutorConfig(**values))


def _ws(root: Path, body: str, name: str = "ws") -> Path:
    ws = root / name
    ws.mkdir()
    (ws / "test_it.py").write_text(body)
    return ws


@contextlib.asynccontextmanager
async def _jobs() -> AsyncIterator[list[asyncio.Task[Any]]]:
    """Tasks started inside are cancelled and awaited on exit, even if a test
    assertion fails, so the executor's cleanup always gets to run."""
    tasks: list[asyncio.Task[Any]] = []
    try:
        yield tasks
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
def no_leftovers() -> Any:
    before = _managed()
    yield before
    assert _managed() - before == set(), "test container was not removed"


# ── Results and cleanup ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_passing_and_failing_suites(tmp_path: Path, no_leftovers: set[str]) -> None:
    ok = await run_tests_in_snapshot(
        _executor(), _ws(tmp_path, "def test_ok():\n    assert True\n", "ok")
    )
    assert ok.passed and ok.exit_code == 0 and "1 passed" in ok.stdout
    bad = await run_tests_in_snapshot(
        _executor(), _ws(tmp_path, "def test_bad():\n    assert 1 == 2\n", "bad")
    )
    assert not bad.passed and bad.exit_code == 1 and "1 failed" in bad.stdout


@pytest.mark.asyncio
async def test_container_security_posture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_leftovers: set[str]
) -> None:
    monkeypatch.setenv("CODEFORGE_IT_WORKER_SECRET", "worker-secret-sentinel")
    ws = tmp_path / "trusted-workspace"
    ws.mkdir()
    (ws / "test_posture.py").write_text(
        f"""
import errno, os, socket

HOST_WORKSPACE = {str(ws)!r}

def _status(key):
    return open("/proc/self/status").read().split(key + ":")[1].split()[0]

def test_runs_unprivileged():
    assert os.getuid() == 65534 and os.getgid() == 65534
    assert int(_status("CapEff"), 16) == 0
    assert int(_status("CapPrm"), 16) == 0
    assert int(_status("CapBnd"), 16) == 0
    assert _status("NoNewPrivs") == "1"
    assert _status("Seccomp") == "2"

def test_has_no_network():
    assert sorted(os.listdir("/sys/class/net")) == ["lo"]
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=3)
    except OSError:
        pass
    else:
        raise AssertionError("network is reachable")

def test_root_filesystem_is_read_only():
    for path in ("/etc/codeforge", "/usr/codeforge", "/codeforge"):
        try:
            open(path, "w")
        except OSError as exc:
            assert exc.errno in (errno.EROFS, errno.EACCES)
        else:
            raise AssertionError(path + " is writable")

def test_only_scoped_paths_are_writable():
    open("/workspace/generated.txt", "w").write("from the container")
    open("/tmp/scratch", "w").write("x")

def test_no_docker_socket_host_paths_or_secrets():
    for sock in ("/var/run/docker.sock", "/run/docker.sock"):
        assert not os.path.exists(sock)
    assert not os.path.exists(HOST_WORKSPACE)
    mounts = open("/proc/self/mountinfo").read()
    assert HOST_WORKSPACE not in mounts and "docker.sock" not in mounts
    assert "worker-secret-sentinel" not in repr(dict(os.environ))
    assert "CODEFORGE_IT_WORKER_SECRET" not in os.environ

def test_limits_are_applied():
    assert open("/sys/fs/cgroup/pids.max").read().strip() == "64"
    assert open("/sys/fs/cgroup/memory.max").read().strip() == str(1024 * 1024 * 1024)
    assert open("/sys/fs/cgroup/memory.swap.max").read().strip() == "0"
    quota, period = open("/sys/fs/cgroup/cpu.max").read().split()
    assert int(quota) / int(period) == 0.5
"""
    )
    result = await run_tests_in_snapshot(
        _executor(pids_limit=64, memory_mb=1024, cpus=0.5), ws
    )
    assert result.passed, result.stdout + result.stderr
    assert "6 passed" in result.stdout
    # Nothing the tests wrote came back to the trusted workspace.
    assert sorted(p.name for p in ws.iterdir()) == ["test_posture.py"]


@pytest.mark.asyncio
async def test_pids_limit_bounds_process_creation(tmp_path: Path, no_leftovers: set[str]) -> None:
    body = """
import threading, time

def test_spawn_until_refused():
    stop = threading.Event()
    started = 0
    try:
        for _ in range(500):
            threading.Thread(target=stop.wait, daemon=True).start()
            started += 1
    except RuntimeError:
        pass
    finally:
        stop.set()
    print("STARTED", started)
    assert started < 64
"""
    result = await run_tests_in_snapshot(_executor(pids_limit=64), _ws(tmp_path, body))
    assert result.passed, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_memory_limit_kills_the_suite(tmp_path: Path, no_leftovers: set[str]) -> None:
    body = """
def test_allocate():
    blocks = [bytearray(64 * 1024 * 1024) for _ in range(12)]
    assert len(blocks) == 12
"""
    result = await run_tests_in_snapshot(
        _executor(memory_mb=320, workspace_tmpfs_mb=64, tmp_tmpfs_mb=32), _ws(tmp_path, body)
    )
    assert not result.passed
    assert result.exit_code == 137 or "memory limit" in result.stderr


# ── Timeout, cancellation and lease loss ───────────────────────────────────────


@pytest.mark.asyncio
async def test_timeout_stops_and_removes_the_container(
    tmp_path: Path, no_leftovers: set[str]
) -> None:
    body = "import time\n\ndef test_hang():\n    time.sleep(300)\n"
    start = time.monotonic()
    result = await run_tests_in_snapshot(_executor(timeout_seconds=5), _ws(tmp_path, body))
    assert time.monotonic() - start < 60
    assert not result.passed and result.exit_code == -1
    assert "timed out after 5s" in result.stderr


@pytest.mark.asyncio
async def test_cancellation_stops_and_removes_the_container(
    tmp_path: Path, no_leftovers: set[str]
) -> None:
    body = "import time\n\ndef test_hang():\n    time.sleep(300)\n"
    async with _jobs() as jobs:
        task = asyncio.create_task(run_tests_in_snapshot(_executor(), _ws(tmp_path, body)))
        jobs.append(task)
        await _wait_for_new(no_leftovers)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_lease_loss_stops_only_that_jobs_container(
    tmp_path: Path, no_leftovers: set[str]
) -> None:
    hang = "import time\n\ndef test_hang():\n    time.sleep(300)\n"
    slow_pass = "import time\n\ndef test_slow():\n    time.sleep(10)\n"
    lost_a, lost_b = asyncio.Event(), asyncio.Event()

    async with _jobs() as jobs:
        job_a = asyncio.create_task(
            run_until_ownership_lost(
                run_tests_in_snapshot(_executor(), _ws(tmp_path, hang, "a")), lost_a
            )
        )
        jobs.append(job_a)
        [container_a] = await _wait_for_new(no_leftovers)
        job_b = asyncio.create_task(
            run_until_ownership_lost(
                run_tests_in_snapshot(_executor(), _ws(tmp_path, slow_pass, "b")), lost_b
            )
        )
        jobs.append(job_b)
        both = await _wait_for_new(no_leftovers, count=2)
        [container_b] = both - {container_a}

        lost_a.set()
        with pytest.raises(OwnershipLostError):
            await job_a
        remaining = _managed()
        assert container_a not in remaining
        assert container_b in _running(remaining)
        assert not job_b.done()

        result_b = await job_b
        assert result_b.passed


@pytest.mark.asyncio
async def test_orphaned_container_stops_at_its_own_deadline(
    tmp_path: Path, no_leftovers: set[str]
) -> None:
    """If the worker dies mid-run, nothing removes the container: it must stop itself."""
    config = DockerExecutorConfig(image=IMAGE, timeout_seconds=2, container_grace_seconds=1)
    name = f"{NAME_PREFIX}orphan-{uuid.uuid4().hex}"
    body = (
        "import subprocess, time\n\n"
        "def test_hang():\n"
        "    subprocess.Popen(['sleep', '300'], start_new_session=True)\n"
        "    time.sleep(300)\n"
    )
    async with workspace_snapshot(_ws(tmp_path, body)) as snapshot:
        archive = snapshot.archive_path.read_bytes()
    docker = shutil.which("docker")
    assert docker
    try:
        _docker(*build_create_args(config, name))
        start = time.monotonic()
        # Started directly: no executor supervises it, as after a worker crash.
        proc = subprocess.run(  # nosec B603 - fixed arguments
            [docker, "start", "--attach", "--interactive", name],
            input=archive, capture_output=True, timeout=120, check=False,
        )
        assert time.monotonic() - start < 60
        state = _docker("inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", name).split()
        assert state == ["exited", str(DEADLINE_EXIT)]
        assert b"container deadline exceeded" in proc.stderr
    finally:
        _docker("rm", "--force", name)


# ── Fail closed with the real CLI ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unreachable_daemon_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCKER_HOST", f"unix://{tmp_path}/no-daemon.sock")
    with pytest.raises(ExecutionUnavailableError, match="unavailable"):
        await run_tests_in_snapshot(_executor(), _ws(tmp_path, "def test_ok(): pass\n"))


@pytest.mark.asyncio
async def test_missing_image_is_unavailable_and_never_pulled(
    tmp_path: Path, no_leftovers: set[str]
) -> None:
    missing = "sha256:" + "0" * 64
    with pytest.raises(ExecutionUnavailableError, match="refused to create"):
        await run_tests_in_snapshot(
            _executor(image=missing), _ws(tmp_path, "def test_ok(): pass\n")
        )
