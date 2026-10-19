"""Phase 10B.5 Stage 1D — the production gVisor executor against real runsc.

Executors come from the production factory (``get_test_executor`` with
``CODEFORGE_TEST_EXECUTION_MODE=gvisor``) or, where a test needs non-default
limits, from the production :class:`GVisorTestExecutor` and
:class:`GVisorExecutorConfig`; never from a test-only subclass.  Runs in the
``gvisor-spike`` workflow after runsc is installed from a pinned,
checksum-verified release and the runtimes in ``runtimes.json`` are
registered.  With ``CODEFORGE_IT_GVISOR_REQUIRED=1`` anything missing fails
the run; the workflow also fails on any skip or a short test count.

The host-observation and workload helpers are shared with the feasibility
spike (``test_gvisor_spike.py``).  Guest tasks are counted by unique PID and
checked against the workload's own processes: gVisor's /proc listing can
repeat an entry.
"""

from __future__ import annotations

import asyncio
import json
import subprocess  # nosec B404 - drives the local docker CLI in tests
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_gvisor_spike as spike
from test_gvisor_spike import (
    GVISOR,
    IMAGE,
    MIB,
    NPROC_LIMIT,
    PYTEST_INI,
    RUNSC,
    _assert_sandbox_gone,
    _check_nproc_probe,
    _docker,
    _docker_bin,
    _events,
    _info,
    _jobs,
    _managed,
    _nproc_workload,
    _num,
    _observe,
    _peak,
    _probe_ws,
    _probes,
    _require_runtime,
    _running,
    _stat,
    _wait_for_new,
    record,
)

from app.config import ExecutionMode, settings
from app.execution import gvisor_executor
from app.execution.docker_executor import DEADLINE_EXIT, NAME_PREFIX, build_create_args
from app.execution.gvisor_executor import GVisorExecutorConfig, GVisorTestExecutor
from app.execution.gvisor_runtime import (
    GUARD_REFUSAL,
    HOST_PIDS_MAX_PEAK_FRACTION,
    MIN_HOST_PIDS,
    GVisorRuntimeRequirement,
)
from app.execution.ownership import OwnershipLostError, run_until_ownership_lost
from app.execution.test_executor import (
    ExecutionUnavailableError,
    IsolationLevel,
    TestExecutor,
    get_test_executor,
    run_tests_in_snapshot,
)
from app.workspace.snapshot import workspace_snapshot

pytestmark = spike.pytestmark
SENTINEL = "CODEFORGE-REPOSITORY-CODE-RAN"
REQ = GVisorRuntimeRequirement(name=GVISOR, path=RUNSC)


@pytest.fixture
def no_leftovers() -> Iterator[set[str]]:
    before = _managed()
    yield before
    assert _managed() - before == set(), "test container was not removed"


@pytest.fixture
def gvisor_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Production settings for gvisor mode (the defaults: NPROC 64, host PIDs 256)."""
    _require_runtime(GVISOR)
    monkeypatch.setattr(settings, "codeforge_test_execution_mode", ExecutionMode.GVISOR)
    monkeypatch.setattr(settings, "codeforge_docker_test_image", IMAGE)
    monkeypatch.setattr(settings, "codeforge_docker_timeout_seconds", 120)
    monkeypatch.setattr(settings, "codeforge_gvisor_runtime", GVISOR)
    monkeypatch.setattr(settings, "codeforge_gvisor_runtime_path", RUNSC)
    yield


def _factory() -> GVisorTestExecutor:
    executor = get_test_executor()
    assert type(executor) is GVisorTestExecutor, executor
    assert executor.isolation is IsolationLevel.GVISOR
    return executor


def _executor(**overrides: Any) -> GVisorTestExecutor:
    values: dict[str, Any] = {"image": IMAGE, "timeout_seconds": 120, "runtime": REQ, "nproc_limit": NPROC_LIMIT}
    values.update(overrides)
    return GVisorTestExecutor(GVisorExecutorConfig(**values))


def _ws(root: Path, name: str, files: dict[str, str]) -> Path:
    ws = root / name
    ws.mkdir()
    for rel, body in {"pytest.ini": PYTEST_INI, **files}.items():
        (ws / rel).write_text(body)
    return ws


def _sentinel_ws(root: Path, name: str = "sentinel") -> Path:
    """The earliest repository code (conftest import) announces itself on both streams."""
    announce = f"import sys\nfor s in (sys.stdout, sys.stderr):\n    s.write({SENTINEL + chr(10)!r})\n    s.flush()\n"
    return _ws(root, name, {"conftest.py": announce, "test_ok.py": "def test_ok():\n    assert True\n"})


def _nproc_host(obs: spike.Observed) -> dict[str, Any]:
    pids_max = _num(obs.samples[0]["pids.max"])
    peak = _peak(obs.samples, "pids.peak") or _peak(obs.samples, "pids.current") or 0
    return {"pids_max": pids_max, "pids_peak": peak, "headroom": (pids_max or 0) - peak,
            "peak_fraction": round(peak / pids_max, 3) if pids_max else None}  # fmt: skip


def _nproc_ulimit(host_config: dict[str, Any]) -> list[dict[str, Any]]:
    return [u for u in host_config.get("Ulimits") or [] if u.get("Name") == "nproc"]


# ── 1-2. Results through the production factory ────────────────────────────────


@pytest.mark.asyncio
async def test_factory_executor_runs_a_passing_suite_under_verified_gvisor(
    gvisor_mode: None, tmp_path: Path, no_leftovers: set[str]
) -> None:
    executor = _factory()
    ws = _ws(tmp_path, "pass", {"test_ok.py": "import time\n\ndef test_ok():\n    time.sleep(2)\n"})
    obs = await _observe(executor, ws, processes=True)
    procs = {p["cmdline"] for s in obs.samples for p in s.get("processes", [])}
    record("executor_pass", runtime=obs.runtime, ulimits=obs.host_config.get("Ulimits"),
           pids_limit=obs.host_config.get("PidsLimit"), passed=obs.result.passed,
           host_processes=sorted(c[:60] for c in procs))  # fmt: skip
    assert obs.result.passed and obs.result.exit_code == 0 and "1 passed" in obs.result.stdout
    assert obs.runtime == GVISOR
    assert _nproc_ulimit(obs.host_config) == [{"Name": "nproc", "Soft": 64, "Hard": 64}]
    assert obs.host_config.get("PidsLimit") == MIN_HOST_PIDS
    assert any("runsc" in c for c in procs) and not any("pytest" in c for c in procs)


@pytest.mark.asyncio
async def test_factory_executor_reports_failing_tests(gvisor_mode: None, tmp_path: Path, no_leftovers: set[str]) -> None:
    ws = _ws(tmp_path, "fail", {"test_bad.py": "def test_bad():\n    assert 1 == 2\n"})
    result = await run_tests_in_snapshot(_factory(), ws)
    record("executor_fail", exit_code=result.exit_code, passed=result.passed)
    assert not result.passed and result.exit_code == 1 and "1 failed" in result.stdout


# ── 3-5, 7. NPROC inside the sandbox and host PIDs headroom ────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["forks", "threads"])
async def test_production_nproc_limit_bounds_guest_tasks_with_host_headroom(
    gvisor_mode: None, tmp_path: Path, no_leftovers: set[str], kind: str
) -> None:
    obs = await _observe(_factory(), _probe_ws(tmp_path, _nproc_workload(kind), kind))
    done = next((p for p in _probes(obs.result.stdout) if p.get("stage") == "done"), None)
    host = _nproc_host(obs)
    record("executor_nproc_" + kind, done=done, host=host, ulimits=obs.host_config.get("Ulimits"),
           exit_code=obs.result.exit_code, stderr=obs.result.stderr[-1000:])  # fmt: skip
    assert obs.result.passed and done is not None, obs.result.stdout[-800:] + obs.result.stderr[-800:]
    assert _nproc_ulimit(obs.host_config) == [{"Name": "nproc", "Soft": NPROC_LIMIT, "Hard": NPROC_LIMIT}]
    _check_nproc_probe(done, kind)
    # Host layer: the configured limit is applied, and NPROC (a clean EAGAIN) was
    # reached with the measured headroom still available.
    assert host["pids_max"] == MIN_HOST_PIDS
    assert host["pids_peak"] <= HOST_PIDS_MAX_PEAK_FRACTION * MIN_HOST_PIDS, host


# ── 6. Two concurrent sandboxes ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_production_nproc_is_per_sandbox(gvisor_mode: None, tmp_path: Path, no_leftovers: set[str]) -> None:
    """A holds its 64 tasks while B runs; B still reaches exactly its own limit."""
    a_ws = _probe_ws(tmp_path, _nproc_workload("forks", hold=25), "a")
    b_ws = _probe_ws(tmp_path, _nproc_workload("forks", hold=1.5), "b")
    async with _jobs() as jobs:
        job_a = asyncio.create_task(run_tests_in_snapshot(_factory(), a_ws))
        jobs.append(job_a)
        [container_a] = await _wait_for_new(no_leftovers)
        await asyncio.sleep(6)  # A reaches its limit within ~2 s of starting
        result_b = await run_tests_in_snapshot(_factory(), b_ws)
        a_still_running = container_a in _running(_managed())
        result_a = await job_a
    a_done = next(p for p in _probes(result_a.stdout) if p.get("stage") == "done")
    b_done = next(p for p in _probes(result_b.stdout) if p.get("stage") == "done")
    record("executor_two_sandboxes", a_done=a_done, b_done=b_done, a_running_after_b=a_still_running)
    assert result_a.passed and result_b.passed
    _check_nproc_probe(a_done, "forks")
    _check_nproc_probe(b_done, "forks")
    # Proven overlap: B reached and released its limit while A was holding its own.
    assert a_done["t_at_limit"] < b_done["t_at_limit"] < b_done["t_release"] < a_done["t_release"]
    assert a_still_running


# ── 8. Memory, CPU, timeout ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_limit(gvisor_mode: None, tmp_path: Path, no_leftovers: set[str]) -> None:
    body = (
        "import json, time\n\ndef test_allocate():\n    blocks = []\n    for i in range(12):\n"
        "        blocks.append(bytearray(64 * 1024 * 1024))\n"
        "        print('PROBE ' + json.dumps({'allocated_mib': 64 * (i + 1)}), flush=True)\n        time.sleep(0.3)\n"
    )
    executor = _executor(memory_mb=320, workspace_tmpfs_mb=64, tmp_tmpfs_mb=32)
    obs = await _observe(executor, _probe_ws(tmp_path, body))
    probes = _probes(obs.result.stdout)
    record("executor_memory", exit_code=obs.result.exit_code, last=probes[-1] if probes else None,
           memory_max=obs.samples[0]["memory.max"], peak=_peak(obs.samples, "memory.current"))  # fmt: skip
    assert _num(obs.samples[0]["memory.max"]) == 320 * MIB and obs.samples[0]["memory.swap.max"] == "0"
    assert (_peak(obs.samples, "memory.current") or 0) <= 320 * MIB
    assert not obs.result.passed
    assert not probes or probes[-1]["allocated_mib"] < 768


@pytest.mark.asyncio
async def test_cpu_limit(gvisor_mode: None, tmp_path: Path, no_leftovers: set[str]) -> None:
    body = (
        "import os, time\n\ndef test_burn():\n    children = []\n    for _ in range(3):\n"
        "        pid = os.fork()\n        if pid == 0:\n            end = time.monotonic() + 8\n"
        "            while time.monotonic() < end:\n                pass\n            os._exit(0)\n"
        "        children.append(pid)\n    for pid in children:\n        os.waitpid(pid, 0)\n"
    )
    obs = await _observe(_executor(cpus=0.5), _probe_ws(tmp_path, body), every=0.5)
    usage = [(s["t"], u) for s in obs.samples if (u := _stat(s["cpu.stat"], "usage_usec")) is not None]
    rate = max(
        ((u2 - u1) / 1e6 / (t2 - t1) for i, (t1, u1) in enumerate(usage) for t2, u2 in usage[i + 1 :] if t2 - t1 >= 3),
        default=None,
    )
    record("executor_cpu", cpu_max=obs.samples[0]["cpu.max"], busiest_rate_cpus=rate)
    assert obs.result.passed and obs.samples[0]["cpu.max"] == "50000 100000"
    assert rate is not None and 0.2 < rate <= 0.65, rate


@pytest.mark.asyncio
async def test_timeout_removes_the_container_and_sandbox(
    gvisor_mode: None, tmp_path: Path, no_leftovers: set[str]
) -> None:
    obs = await _observe(_executor(timeout_seconds=5), _probe_ws(tmp_path, "import time\n\ndef test_hang():\n    time.sleep(300)\n"))
    leftovers = await _assert_sandbox_gone(obs.container)
    record("executor_timeout", exit_code=obs.result.exit_code, leftovers=leftovers)
    assert not obs.result.passed and obs.result.exit_code == -1 and "timed out after 5s" in obs.result.stderr
    assert obs.container not in _managed() and leftovers == []


# ── 9. Stage 1C controls inside the production sandbox ─────────────────────────


@pytest.mark.asyncio
async def test_stage_1c_controls_hold_in_the_production_sandbox(
    gvisor_mode: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_leftovers: set[str]
) -> None:
    monkeypatch.setenv("CODEFORGE_IT_WORKER_SECRET", "worker-secret-sentinel")
    ws = tmp_path / "trusted-workspace"
    ws.mkdir()
    (ws / "pytest.ini").write_text(PYTEST_INI)
    (ws / "test_posture.py").write_text(
        f"""
import errno, os, resource, socket, subprocess

HOST_WORKSPACE = {str(ws)!r}

def _status(key):
    return open("/proc/self/status").read().split(key + ":")[1].split()[0]

def test_non_root_without_capabilities_or_new_privileges():
    assert os.getuid() == os.geteuid() == os.getgid() == 65534
    for key in ("CapEff", "CapPrm", "CapBnd", "CapAmb"):
        assert int(_status(key), 16) == 0, key
    assert _status("NoNewPrivs") == "1"

def test_seccomp_profile_applies():
    assert _status("Seccomp") == "2"
    try:
        os.unshare(os.CLONE_NEWUSER)
    except PermissionError:
        pass
    else:
        raise AssertionError("unshare(CLONE_NEWUSER) was allowed")

def test_nproc_limit_is_applied():
    assert resource.getrlimit(resource.RLIMIT_NPROC) == ({NPROC_LIMIT}, {NPROC_LIMIT})

def test_gvisor_marker_present():
    assert os.path.isfile("/proc/gvisor/kernel_is_gvisor")

def test_no_network():
    assert [n for _, n in socket.if_nameindex()] == ["lo"]
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=3)
    except OSError:
        pass
    else:
        raise AssertionError("network is reachable")

def test_read_only_root_and_noexec_tmp():
    for path in ("/etc/codeforge", "/usr/codeforge"):
        try:
            open(path, "w")
        except OSError as exc:
            assert exc.errno in (errno.EROFS, errno.EACCES)
        else:
            raise AssertionError(path)
    open("/workspace/written-by-tests.txt", "w").write("x")
    with open("/tmp/run.sh", "w") as fh:
        fh.write("#!/bin/sh\\nexit 0\\n")
    os.chmod("/tmp/run.sh", 0o755)
    try:
        subprocess.run(["/tmp/run.sh"], check=False)
    except PermissionError:
        pass
    else:
        raise AssertionError("/tmp allows exec")

def test_no_socket_host_paths_or_secrets():
    for sock in ("/var/run/docker.sock", "/run/docker.sock"):
        assert not os.path.exists(sock)
    assert not os.path.exists(HOST_WORKSPACE)
    assert HOST_WORKSPACE not in open("/proc/self/mountinfo").read()
    assert "docker.sock" not in open("/proc/self/mountinfo").read()
    assert "worker-secret-sentinel" not in repr(dict(os.environ))
"""
    )
    result = await run_tests_in_snapshot(_factory(), ws)
    record("executor_posture", passed=result.passed, stdout=result.stdout[-1500:], stderr=result.stderr[-800:])
    assert result.passed and "7 passed" in result.stdout, result.stdout + result.stderr
    # No write-back to the trusted workspace.
    assert sorted(p.name for p in ws.iterdir()) == ["pytest.ini", "test_posture.py"]


# ── 10. Cancellation, lease loss, orphans, cleanup ─────────────────────────────

_HANG = "import time\n\ndef test_hang():\n    time.sleep(300)\n"


@pytest.mark.asyncio
async def test_cancellation_removes_the_container_and_sandbox(
    gvisor_mode: None, tmp_path: Path, no_leftovers: set[str]
) -> None:
    async with _jobs() as jobs:
        task = asyncio.create_task(run_tests_in_snapshot(_factory(), _probe_ws(tmp_path, _HANG)))
        jobs.append(task)
        [container] = await _wait_for_new(no_leftovers)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    leftovers = await _assert_sandbox_gone(container)
    record("executor_cancel", leftovers=leftovers)
    assert container not in _managed() and leftovers == []


@pytest.mark.asyncio
async def test_lease_loss_stops_only_that_jobs_sandbox(gvisor_mode: None, tmp_path: Path, no_leftovers: set[str]) -> None:
    slow = "import time\n\ndef test_slow():\n    time.sleep(8)\n"
    lost_a, lost_b = asyncio.Event(), asyncio.Event()
    async with _jobs() as jobs:
        job_a = asyncio.create_task(run_until_ownership_lost(
            run_tests_in_snapshot(_factory(), _probe_ws(tmp_path, _HANG, "a")), lost_a))  # fmt: skip
        jobs.append(job_a)
        [container_a] = await _wait_for_new(no_leftovers)
        job_b = asyncio.create_task(run_until_ownership_lost(
            run_tests_in_snapshot(_factory(), _probe_ws(tmp_path, slow, "b")), lost_b))  # fmt: skip
        jobs.append(job_b)
        [container_b] = await _wait_for_new(no_leftovers, count=2) - {container_a}
        lost_a.set()
        with pytest.raises(OwnershipLostError):
            await job_a
        leftovers = await _assert_sandbox_gone(container_a)
        assert container_a not in _managed() and leftovers == []
        assert container_b in _running(_managed())
        assert (await job_b).passed
    record("executor_lease_loss", leftovers=leftovers)


@pytest.mark.asyncio
async def test_orphaned_production_container_stops_at_its_own_deadline(
    gvisor_mode: None, tmp_path: Path, no_leftovers: set[str]
) -> None:
    """After a worker crash nothing removes the container: it must stop itself."""
    config = GVisorExecutorConfig(image=IMAGE, timeout_seconds=2, container_grace_seconds=1, runtime=REQ,
                                  nproc_limit=NPROC_LIMIT)  # fmt: skip
    body = "import subprocess, time\n\ndef test_hang():\n    subprocess.Popen(['sleep', '300'], start_new_session=True)\n    time.sleep(300)\n"
    async with workspace_snapshot(_probe_ws(tmp_path, body)) as snapshot:
        archive = snapshot.archive_path.read_bytes()
    name = f"{NAME_PREFIX}orphan-{uuid.uuid4().hex}"
    try:
        _docker(*build_create_args(config, name))
        start = time.monotonic()
        proc = subprocess.run(  # nosec B603 - fixed binary
            [_docker_bin(), "start", "--attach", "--interactive", name],
            input=archive, capture_output=True, timeout=120, check=False,
        )  # fmt: skip
        state = _docker("inspect", "-f", "{{.State.Status}} {{.State.ExitCode}} {{.HostConfig.Runtime}}", name).split()
        record("executor_orphan", state=state, elapsed=round(time.monotonic() - start, 1))
        assert state == ["exited", str(DEADLINE_EXIT), GVISOR]
        assert b"container deadline exceeded" in proc.stderr
    finally:
        _docker("rm", "--force", name)


# ── 11-12. Refusals: no repository code ever runs ──────────────────────────────


async def _refused(executor: TestExecutor, ws: Path) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    since = time.time()
    with pytest.raises(ExecutionUnavailableError) as exc:
        await run_tests_in_snapshot(executor, ws)
    return str(exc.value), _events(since, "create"), _events(since, "start")


_REGISTRATION_REFUSALS = [
    ("codeforge-gvisor-unregistered", RUNSC, "is not registered"),
    ("codeforge-gvisor-impostor", RUNSC, "path is"),
    (GVISOR, "/usr/local/sbin/runsc", "path is '/usr/local/bin/runsc', expected '/usr/local/sbin/runsc'"),
    ("codeforge-gvisor-no-seccomp", RUNSC, "--oci-seccomp is missing"),
    ("codeforge-gvisor-no-network-none", RUNSC, "--network=none is missing"),
    ("codeforge-gvisor-no-marker", RUNSC, "--gvisor-marker-file is missing"),
    ("codeforge-gvisor-directfs", RUNSC, "--directfs=false is missing"),
    ("codeforge-gvisor-host-network", RUNSC, "(network)"),
    ("codeforge-gvisor-debug", RUNSC, "(debugging)"),
    ("codeforge-gvisor-flag-override", RUNSC, "(flag override)"),
    ("codeforge-gvisor-testonly", RUNSC, "(test-only)"),
    ("codeforge-gvisor-host-uds", RUNSC, "(host access)"),
    ("codeforge-gvisor-unknown-flag", RUNSC, "(unrecognized)"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("runtime", "path", "expected"), _REGISTRATION_REFUSALS,
                         ids=[f"{r}@{p}" for r, p, _ in _REGISTRATION_REFUSALS])  # fmt: skip
async def test_factory_refuses_unverified_runtimes_before_creating_anything(
    gvisor_mode: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_leftovers: set[str],
    runtime: str, path: str, expected: str,
) -> None:
    if runtime != "codeforge-gvisor-unregistered":
        _require_runtime(runtime)
    monkeypatch.setattr(settings, "codeforge_gvisor_runtime", runtime)
    monkeypatch.setattr(settings, "codeforge_gvisor_runtime_path", path)
    message, created, started = await _refused(_factory(), _sentinel_ws(tmp_path))
    record("executor_refused_registration", runtime=runtime, path=path, error=message, creates=len(created))
    assert "gVisor runtime verification failed" in message and expected in message
    assert created == [] and started == []
    assert SENTINEL not in message


@pytest.mark.asyncio
async def test_container_created_under_another_runtime_is_never_started(
    gvisor_mode: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_leftovers: set[str]
) -> None:
    """If argument construction ever selected runc, the post-create check refuses."""
    monkeypatch.setattr(GVisorExecutorConfig, "runtime_args", lambda self: ["--runtime", "runc"])
    message, created, started = await _refused(_factory(), _sentinel_ws(tmp_path))
    record("executor_refused_mismatch", error=message, creates=len(created), starts=len(started))
    assert "Runtime is 'runc', expected 'codeforge-gvisor'" in message
    assert len(created) == 1 and started == []


@pytest.mark.asyncio
async def test_missing_marker_prevents_loading_the_workspace(
    gvisor_mode: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_leftovers: set[str]
) -> None:
    """Real gVisor without --gvisor-marker-file, with only that host finding waived.

    The host check would normally refuse this registration first; waiving that
    single finding shows the in-sandbox guard refuses on its own.
    """
    runtime = "codeforge-gvisor-no-marker"
    _require_runtime(runtime)
    real = gvisor_executor.daemon_runtime_violations

    def waive_marker_arg(info: Any, req: GVisorRuntimeRequirement) -> list[str]:
        return [p for p in real(info, req) if "--gvisor-marker-file" not in p]

    monkeypatch.setattr(gvisor_executor, "daemon_runtime_violations", waive_marker_arg)
    monkeypatch.setattr(settings, "codeforge_gvisor_runtime", runtime)
    message, created, started = await _refused(_factory(), _sentinel_ws(tmp_path))
    record("executor_missing_marker", error=message, creates=len(created), starts=len(started))
    assert "marker was missing" in message and "the workspace was not loaded" in message
    assert GUARD_REFUSAL in message
    assert SENTINEL not in message
    assert len(created) == 1 and len(started) == 1


def test_registered_runtimes_are_visible_to_the_daemon(gvisor_mode: None) -> None:
    """Guard against the refusal tests passing for the wrong reason (e.g. no daemon)."""
    runtimes = _info().get("Runtimes") or {}
    record("executor_runtimes", registered=sorted(runtimes))
    for name in (GVISOR, "codeforge-gvisor-impostor", "codeforge-gvisor-no-marker", "codeforge-gvisor-debug"):
        assert name in runtimes, name
    assert json.dumps(runtimes[GVISOR].get("runtimeArgs")) == json.dumps(
        ["--network=none", "--oci-seccomp", "--directfs=false", "--gvisor-marker-file"]
    )
