"""Phase 10B.5 Stage 1D — gVisor CI feasibility spike (real runsc, real Docker).

Not part of the default suite (``testpaths`` is ``apps/api/tests``).  The
``gvisor-spike`` workflow runs it on a disposable runner after installing a
pinned, checksum-verified runsc and registering the runtimes in
``runtimes.json``:

    CODEFORGE_IT_GVISOR_REQUIRED=1 CODEFORGE_IT_DOCKER_IMAGE=sha256:<id> \\
        pytest sandbox/gvisor/spike

With ``CODEFORGE_IT_GVISOR_REQUIRED=1`` a missing image, daemon or runtime
registration fails the run instead of skipping.  Without it, tests whose
runtime is not registered are skipped, so locally only the runc-based negative
tests run.

Containers are created by Stage 1C's ``DockerTestExecutor`` lifecycle (create,
verify, start, always remove) with ``--runtime`` added and the gVisor checks
from ``app.execution.gvisor_runtime`` layered on.  Limits are judged from the
host's cgroup for the sandbox, not from ``docker inspect`` or from inside the
sandbox.  Every test appends what it observed to ``$CODEFORGE_IT_GVISOR_EVIDENCE``
(JSON lines).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess  # nosec B404 - drives the local docker CLI in tests
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.execution.docker_executor import (
    BOOTSTRAP,
    DEADLINE_EXIT,
    MANAGED_LABEL_KEY,
    MANAGED_LABEL_VALUE,
    NAME_PREFIX,
    DockerExecutorConfig,
    DockerTestExecutor,
    _DockerCLI,
    _summary,
    build_create_args,
    container_violations,
)
from app.execution.gvisor_runtime import (
    FEATURES_KEY,
    GUARD_REFUSAL,
    MARKER_PATH,
    RUNTIME_GUARD_EXIT,
    GVisorRuntimeRequirement,
    container_runtime_violations,
    daemon_runtime_violations,
    guarded_bootstrap,
    runtime_features_violations,
)
from app.execution.ownership import OwnershipLostError, run_until_ownership_lost
from app.execution.test_executor import ExecutionUnavailableError, run_tests_in_snapshot
from app.schemas.task import TestResult
from app.workspace.snapshot import WorkspaceSnapshot, workspace_snapshot

IMAGE = os.environ.get("CODEFORGE_IT_DOCKER_IMAGE", "")
REQUIRED = os.environ.get("CODEFORGE_IT_GVISOR_REQUIRED") == "1"
RUNSC = os.environ.get("CODEFORGE_IT_GVISOR_RUNSC", "/usr/local/bin/runsc")
EVIDENCE = os.environ.get("CODEFORGE_IT_GVISOR_EVIDENCE", "")

GVISOR = "codeforge-gvisor"
IMPOSTOR = "codeforge-gvisor-impostor"
NO_MARKER = "codeforge-gvisor-no-marker"
NO_SECCOMP = "codeforge-gvisor-no-seccomp"
DIRECTFS = "codeforge-gvisor-directfs"
UNREGISTERED = "codeforge-gvisor-unregistered"
REQ = GVisorRuntimeRequirement(name=GVISOR, path=RUNSC)
SENTINEL = "CODEFORGE-SENTINEL-EXECUTED"
MIB = 1024 * 1024
# Workspaces carry this so their prints reach the result's stdout (probe data).
PYTEST_INI = "[pytest]\naddopts = -s\n"


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
    raise RuntimeError(f"gVisor spike tests are required but cannot run: {_REASON}")
pytestmark = pytest.mark.skipif(_REASON is not None, reason=_REASON or "")


# ── Docker and host helpers ────────────────────────────────────────────────────


def _docker_bin() -> str:
    docker = shutil.which("docker")
    assert docker
    return docker


def _docker(*args: str, timeout: float = 120) -> str:
    return subprocess.run(  # nosec B603 - fixed binary, test-controlled arguments
        [_docker_bin(), *args], capture_output=True, text=True, timeout=timeout, check=True
    ).stdout


def _info() -> dict[str, Any]:
    data = json.loads(_docker("info", "--format", "{{json .}}"))
    assert isinstance(data, dict)
    return data


def _require_runtime(name: str) -> dict[str, Any]:
    entry = (_info().get("Runtimes") or {}).get(name)
    if entry is None:
        message = f"runtime {name!r} is not registered with the Docker daemon"
        if REQUIRED:
            pytest.fail(message)
        pytest.skip(message)
    return dict(entry)


def record(kind: str, **data: Any) -> None:
    line = json.dumps({"kind": kind, **data}, default=str, sort_keys=True)
    print(f"EVIDENCE {line}")
    if EVIDENCE:
        with open(EVIDENCE, "a") as fh:
            fh.write(line + "\n")


def _managed() -> set[str]:
    out = _docker("ps", "-aq", "--no-trunc", "--filter", f"label={MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}")
    return set(out.split())


def _running(ids: set[str]) -> set[str]:
    running = set()
    for cid in ids:
        with contextlib.suppress(subprocess.CalledProcessError):
            if _docker("inspect", "-f", "{{.State.Running}}", cid).strip() == "true":
                running.add(cid)
    return running


async def _wait_for_new(before: set[str], count: int = 1, timeout: float = 120) -> set[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        new = _running(_managed() - before)
        if len(new) >= count:
            return new
        await asyncio.sleep(0.1)
    raise AssertionError("test container never started")


def _events(since: float, event: str) -> list[dict[str, Any]]:
    """Container *event*s for executor-managed containers since *since* (until now)."""
    out = _docker(
        "events", "--since", f"{since:.3f}", "--until", f"{time.time():.3f}",
        "--filter", "type=container", "--filter", f"event={event}",
        "--filter", f"label={MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}", "--format", "{{json .}}",
    )  # fmt: skip
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _read(path: Path | str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _cmdline(pid: str) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode(errors="replace").strip()[:300]


def _proc_uid(pid: str) -> int | None:
    """Real UID of a host process."""
    for line in (_read(f"/proc/{pid}/status") or "").splitlines():
        if line.startswith("Uid:"):
            return int(line.split()[1])
    return None


def _host_cgroup(container: str) -> Path:
    """The host cgroup (v2) of the container's main process."""
    pid = _docker("inspect", "-f", "{{.State.Pid}}", container).strip()
    assert pid.isdigit() and int(pid) > 0, f"container has no host pid: {pid!r}"
    for line in (_read(f"/proc/{pid}/cgroup") or "").splitlines():
        if line.startswith("0::"):
            return Path("/sys/fs/cgroup") / line[3:].lstrip("/")
    raise AssertionError(f"no cgroup v2 entry for pid {pid}")


_CGROUP_FILES = (
    "pids.max", "pids.current", "pids.peak", "memory.max", "memory.swap.max",
    "memory.current", "memory.peak", "memory.events", "cpu.max", "cpu.stat",
)  # fmt: skip


def _cgroup_state(cg: Path, processes: bool = False) -> dict[str, Any]:
    state: dict[str, Any] = {"t": time.monotonic(), "path": str(cg)}
    for name in _CGROUP_FILES:
        state[name] = _read(cg / name)
    threads = 0
    procs: list[dict[str, Any]] = []
    for dirpath, _, _ in os.walk(cg):
        threads += len((_read(Path(dirpath) / "cgroup.threads") or "").split())
        if processes:
            for pid in (_read(Path(dirpath) / "cgroup.procs") or "").split():
                procs.append({"pid": int(pid), "uid": _proc_uid(pid), "cgroup": os.path.relpath(dirpath, cg),
                              "cmdline": _cmdline(pid)})  # fmt: skip
    state["host_threads"] = threads
    if processes:
        state["processes"] = procs
    return state


def _num(value: str | int | None) -> int | None:
    if isinstance(value, int):
        return value
    return int(value) if value is not None and value.isdigit() else None


def _stat(text: str | None, key: str) -> int | None:
    for line in (text or "").splitlines():
        name, _, value = line.partition(" ")
        if name == key:
            return int(value)
    return None


def _host_processes_mentioning(token: str) -> list[str]:
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and token in (cmd := _cmdline(entry.name)):
            found.append(f"{entry.name}: {cmd}")
    return found


# ── Workspaces ─────────────────────────────────────────────────────────────────


def _ws(root: Path, files: dict[str, str], name: str = "ws") -> Path:
    ws = root / name
    ws.mkdir()
    for rel, body in files.items():
        (ws / rel).write_text(body)
    return ws


def _probe_ws(root: Path, body: str, name: str = "ws") -> Path:
    return _ws(root, {"pytest.ini": PYTEST_INI, "test_probe.py": body}, name)


def _sentinel_ws(root: Path, name: str = "sentinel") -> Path:
    """The earliest repository code pytest runs (conftest import) announces itself."""
    return _ws(
        root,
        {
            "pytest.ini": PYTEST_INI,
            "conftest.py": f"import sys\nsys.stdout.write({SENTINEL + chr(10)!r})\nsys.stdout.flush()\n",
            "test_ok.py": "def test_ok():\n    assert True\n",
        },
        name,
    )


def _probes(stdout: str) -> list[dict[str, Any]]:
    return [json.loads(line[6:]) for line in stdout.splitlines() if line.startswith("PROBE ")]


# ── The gVisor variant of the Stage 1C executor ────────────────────────────────


@dataclass(frozen=True)
class GuardedConfig(DockerExecutorConfig):
    """Stage 1C's container, with the marker guard ahead of the bootstrap."""

    def container_cmd(self) -> list[str]:
        deadline = float(self.timeout_seconds) + float(self.container_grace_seconds)
        return ["-I", "-c", guarded_bootstrap(BOOTSTRAP), repr(deadline)]


class SpikeGVisorExecutor(DockerTestExecutor):
    """Stage 1C lifecycle unchanged; adds ``--runtime`` and the gVisor checks.

    ``requirement=None`` skips the host-side checks.  Tests use that only to show
    what the in-container guard does on its own.
    """

    def __init__(
        self, config: DockerExecutorConfig, runtime: str, requirement: GVisorRuntimeRequirement | None
    ) -> None:
        super().__init__(config)
        self.runtime = runtime
        self.requirement = requirement

    async def _preflight(self, cli: _DockerCLI) -> None:
        await super()._preflight(cli)
        if self.requirement is None:
            return
        res = await cli.run("info", "--format", "{{json .}}")
        try:
            info = json.loads(res.stdout)
        except ValueError:
            info = None
        if res.returncode != 0 or not isinstance(info, dict):
            raise ExecutionUnavailableError("Docker daemon is unavailable")
        problems = daemon_runtime_violations(info, self.requirement)
        if problems:
            raise ExecutionUnavailableError("gVisor runtime verification failed: " + "; ".join(problems))

    async def _run_container(self, cli: _DockerCLI, name: str, snapshot: WorkspaceSnapshot) -> TestResult:
        args = build_create_args(self.config, name)
        args[1:1] = ["--runtime", self.runtime]
        create = await cli.run(*args)
        if create.returncode != 0:
            raise ExecutionUnavailableError(
                f"Docker refused to create the test container: {_summary(create.stderr)}"
            )
        if create.stderr.strip():
            raise ExecutionUnavailableError(
                f"Docker reported warnings creating the test container: {_summary(create.stderr)}"
            )
        inspect = await self._inspect(cli, name)
        problems = container_violations(inspect, self.config)
        if self.requirement is not None:
            problems += container_runtime_violations(inspect, self.requirement)
        if problems:
            raise ExecutionUnavailableError(
                "Docker did not apply the required test isolation: " + "; ".join(problems)
            )
        result = await self._start(cli, name, snapshot)
        if result.exit_code == RUNTIME_GUARD_EXIT:
            raise ExecutionUnavailableError(
                "The gVisor runtime marker was missing; the workspace was not loaded"
            )
        return result


def _gvisor(**overrides: Any) -> SpikeGVisorExecutor:
    values: dict[str, Any] = {"image": IMAGE, "timeout_seconds": 120}
    values.update(overrides)
    return SpikeGVisorExecutor(GuardedConfig(**values), GVISOR, REQ)


def _unverified(runtime: str, **overrides: Any) -> SpikeGVisorExecutor:
    values: dict[str, Any] = {"image": IMAGE, "timeout_seconds": 120}
    values.update(overrides)
    return SpikeGVisorExecutor(GuardedConfig(**values), runtime, None)


@dataclass
class RawRun:
    start_rc: int
    status: str
    exit_code: int
    oom_killed: bool
    runtime: str
    stdout: str
    stderr: str
    duration: float


async def _raw_run(
    config: DockerExecutorConfig, runtime: str, ws: Path, extra_create_args: tuple[str, ...] = ()
) -> RawRun:
    """Create and start one container directly (no executor), then remove it."""
    async with workspace_snapshot(ws) as snapshot:
        archive = snapshot.archive_path.read_bytes()
    name = f"{NAME_PREFIX}spike-{uuid.uuid4().hex}"
    args = build_create_args(config, name)
    args[1:1] = ["--runtime", runtime, *extra_create_args]
    try:
        _docker(*args)
        start = time.monotonic()
        proc = subprocess.run(  # nosec B603 - fixed binary
            [_docker_bin(), "start", "--attach", "--interactive", name],
            input=archive, capture_output=True, timeout=float(config.timeout_seconds) + 120, check=False,
        )  # fmt: skip
        duration = time.monotonic() - start
        state = json.loads(_docker("inspect", "--format", "{{json .State}}", name))
        host = json.loads(_docker("inspect", "--format", "{{json .HostConfig}}", name))
        return RawRun(
            start_rc=proc.returncode,
            status=str(state.get("Status")),
            exit_code=int(state.get("ExitCode", -1)),
            oom_killed=bool(state.get("OOMKilled")),
            runtime=str(host.get("Runtime")),
            stdout=proc.stdout.decode(errors="replace"),
            stderr=proc.stderr.decode(errors="replace"),
            duration=round(duration, 3),
        )
    finally:
        _docker("rm", "--force", name)


@dataclass
class Observed:
    result: TestResult
    container: str
    runtime: str
    samples: list[dict[str, Any]]


async def _observe(
    executor: DockerTestExecutor, ws: Path, every: float = 0.25, processes: bool = False
) -> Observed:
    """Run *executor* on *ws*, sampling the sandbox's host cgroup while it runs.

    The first sample always lists the host processes; with *processes* every
    sample does.
    """
    before = _managed()
    task = asyncio.create_task(run_tests_in_snapshot(executor, ws))
    try:
        deadline = time.monotonic() + 120
        new: set[str] = set()
        while not new:
            if task.done():
                raise AssertionError(f"the run ended before its container was seen: {task.result()!r}")
            if time.monotonic() > deadline:
                raise AssertionError("test container never started")
            await asyncio.sleep(0.1)
            new = _running(_managed() - before)
        [container] = new
        runtime = _docker("inspect", "-f", "{{.HostConfig.Runtime}}", container).strip()
        cg = _host_cgroup(container)
        samples = [_cgroup_state(cg, processes=True)]
        while not task.done():
            await asyncio.sleep(every)
            sample = _cgroup_state(cg, processes=processes)
            if sample["pids.current"] is not None:
                samples.append(sample)
        return Observed(await task, container, runtime, samples)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class LiveRun:
    """A container created and started directly (no executor) with its stdout streamed.

    Lets a test act on what one sandbox reports while it is still running.  The
    caller must call :meth:`remove`.
    """

    def __init__(self, name: str, proc: asyncio.subprocess.Process) -> None:
        self.name = name
        self.proc = proc
        self.container_id = _docker("inspect", "-f", "{{.Id}}", name).strip()
        self.stdout_lines: list[str] = []
        self.stderr = b""
        self.ulimits: Any = None
        self._readers = [asyncio.create_task(self._read_stdout()), asyncio.create_task(self._read_stderr())]

    @classmethod
    async def start(
        cls, config: DockerExecutorConfig, runtime: str, ws: Path, extra_create_args: tuple[str, ...] = ()
    ) -> LiveRun:
        async with workspace_snapshot(ws) as snapshot:
            archive = snapshot.archive_path.read_bytes()
        name = f"{NAME_PREFIX}spike-{uuid.uuid4().hex}"
        args = build_create_args(config, name)
        args[1:1] = ["--runtime", runtime, *extra_create_args]
        _docker(*args)
        try:
            proc = await asyncio.create_subprocess_exec(
                _docker_bin(), "start", "--attach", "--interactive", name,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )  # fmt: skip
        except BaseException:
            _docker("rm", "--force", name)
            raise
        run = cls(name, proc)
        assert proc.stdin is not None
        proc.stdin.write(archive)
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await proc.stdin.drain()
        proc.stdin.close()
        return run

    async def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        async for line in self.proc.stdout:
            self.stdout_lines.append(line.decode(errors="replace"))

    async def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        self.stderr = await self.proc.stderr.read()

    @property
    def stdout(self) -> str:
        return "".join(self.stdout_lines)

    def probe(self, stage: str) -> dict[str, Any] | None:
        return next((p for p in _probes(self.stdout) if p.get("stage") == stage), None)

    async def wait_for_probe(self, stage: str, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (found := self.probe(stage)) is not None:
                return found
            if self.proc.returncode is not None:
                break
            await asyncio.sleep(0.05)
        raise AssertionError(f"{self.name} never reported {stage!r}: {self.stdout[-500:]} {self.stderr[-500:]!r}")

    def running(self) -> bool:
        return _docker("inspect", "-f", "{{.State.Running}}", self.name).strip() == "true"

    async def finish(self, timeout: float) -> RawRun:
        start = time.monotonic()
        await asyncio.wait_for(self.proc.wait(), timeout)
        await asyncio.gather(*self._readers)
        state = json.loads(_docker("inspect", "--format", "{{json .State}}", self.name))
        host = json.loads(_docker("inspect", "--format", "{{json .HostConfig}}", self.name))
        self.ulimits = host.get("Ulimits")
        return RawRun(
            start_rc=int(self.proc.returncode or 0),
            status=str(state.get("Status")),
            exit_code=int(state.get("ExitCode", -1)),
            oom_killed=bool(state.get("OOMKilled")),
            runtime=str(host.get("Runtime")),
            stdout=self.stdout,
            stderr=self.stderr.decode(errors="replace"),
            duration=round(time.monotonic() - start, 3),
        )

    async def remove(self) -> list[str]:
        """Force-remove the container; returns host processes still naming it."""
        if self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()
        _docker("rm", "--force", self.name)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.gather(self.proc.wait(), *self._readers), 15)
        return await _assert_sandbox_gone(self.container_id)


async def _sample(run: LiveRun, every: float = 0.2) -> list[dict[str, Any]]:
    """Host cgroup readings for *run* until its attached CLI exits."""
    cg: Path | None = None
    samples: list[dict[str, Any]] = []
    while run.proc.returncode is None:
        if cg is None:
            with contextlib.suppress(AssertionError, subprocess.CalledProcessError):
                cg = _host_cgroup(run.name)
                samples.append(_cgroup_state(cg, processes=True))
        else:
            sample = _cgroup_state(cg)
            if sample["pids.current"] is not None:
                samples.append(sample)
        await asyncio.sleep(every)
    return samples


def _peak(samples: list[dict[str, Any]], key: str) -> int | None:
    values = [v for s in samples if (v := _num(s.get(key))) is not None]
    return max(values) if values else None


@contextlib.asynccontextmanager
async def _jobs() -> AsyncIterator[list[asyncio.Task[Any]]]:
    tasks: list[asyncio.Task[Any]] = []
    try:
        yield tasks
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
def no_leftovers() -> Iterator[set[str]]:
    before = _managed()
    yield before
    assert _managed() - before == set(), "test container was not removed"


# ── 1. Registration and runtime selection ──────────────────────────────────────


def test_reviewed_runtime_passes_host_verification() -> None:
    entry = _require_runtime(GVISOR)
    info = _info()
    features = json.loads((entry.get("status") or {}).get(FEATURES_KEY) or "{}")
    annotations = features.get("annotations") or {}
    record(
        "registration",
        runtime=GVISOR,
        path=entry.get("path"),
        runtime_args=entry.get("runtimeArgs"),
        entry_keys=sorted(entry),
        gvisor_annotations=sorted(k for k in annotations if k.startswith("dev.gvisor."))[:12],
        gvisor_annotation_count=sum(1 for k in annotations if k.startswith("dev.gvisor.")),
        default_runtime=info.get("DefaultRuntime"),
        server_version=info.get("ServerVersion"),
        cgroup_driver=info.get("CgroupDriver"),
        cgroup_version=info.get("CgroupVersion"),
        registered_runtimes=sorted(info.get("Runtimes") or {}),
    )
    assert daemon_runtime_violations(info, REQ) == []
    # The spike adds runtimes; it never changes the daemon's default.
    assert info.get("DefaultRuntime") == "runc"


@pytest.mark.asyncio
async def test_docker_selects_gvisor_and_the_host_sees_only_the_sandbox(
    tmp_path: Path, no_leftovers: set[str]
) -> None:
    """Host-side evidence: under gVisor no repository process exists on the host."""
    _require_runtime(GVISOR)
    hold = "import time\n\ndef test_hold():\n    time.sleep(4)\n"
    gvisor = await _observe(_gvisor(), _probe_ws(tmp_path, hold, "gvisor"), processes=True)
    control = await _observe(
        DockerTestExecutor(DockerExecutorConfig(image=IMAGE, timeout_seconds=120)),
        _probe_ws(tmp_path, hold, "runc"),
        processes=True,
    )

    def seen(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Every distinct host process over the whole run (not one early sample)."""
        unique = {(p["pid"], p["cmdline"]): p for s in samples for p in s.get("processes", [])}
        return list(unique.values())

    g_procs, c_procs = seen(gvisor.samples), seen(control.samples)
    record("selection", gvisor_runtime=gvisor.runtime, gvisor_host_processes=g_procs,
           gvisor_samples=len(gvisor.samples), control_runtime=control.runtime,
           control_host_processes=c_procs, control_samples=len(control.samples))  # fmt: skip
    assert gvisor.result.passed and control.result.passed
    assert gvisor.runtime == GVISOR and control.runtime == "runc"
    assert len(gvisor.samples) > 1 and len(control.samples) > 1
    assert g_procs and any("runsc" in p["cmdline"] or "gvisor" in p["cmdline"] for p in g_procs)
    assert not any("pytest" in p["cmdline"] for p in g_procs), g_procs
    # The control shows the probe would have seen pytest on the host under runc.
    assert any("pytest" in p["cmdline"] for p in c_procs), c_procs


@pytest.mark.asyncio
async def test_stage_1c_controls_hold_inside_gvisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_leftovers: set[str]
) -> None:
    _require_runtime(GVISOR)
    monkeypatch.setenv("CODEFORGE_IT_WORKER_SECRET", "worker-secret-sentinel")
    ws = tmp_path / "trusted-workspace"
    ws.mkdir()
    (ws / "pytest.ini").write_text(PYTEST_INI)
    (ws / "test_posture.py").write_text(
        f"""
import errno, json, os, socket, subprocess

HOST_WORKSPACE = {str(ws)!r}
MARKER = {MARKER_PATH!r}

def _status(key):
    return open("/proc/self/status").read().split(key + ":")[1].split()[0]

def test_report():
    text = open("/proc/self/status").read()
    keys = ("Uid", "Gid", "CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb", "NoNewPrivs", "Seccomp")
    print("PROBE " + json.dumps({{
        "status": {{k: _status(k) for k in keys if k + ":" in text}},
        "interfaces": [n for _, n in socket.if_nameindex()],
        "marker": os.path.isfile(MARKER),
        "release": os.uname().release,
    }}))

def test_runs_unprivileged():
    assert os.getuid() == 65534 and os.getgid() == 65534
    for key in ("CapEff", "CapPrm", "CapBnd"):
        assert int(_status(key), 16) == 0, key
    assert _status("NoNewPrivs") == "1"

def test_docker_seccomp_profile_applies_inside_the_sandbox():
    assert _status("Seccomp") == "2"
    # Docker's default profile refuses unshare without CAP_SYS_ADMIN.
    try:
        os.unshare(os.CLONE_NEWUSER)
    except PermissionError:
        pass
    else:
        raise AssertionError("unshare(CLONE_NEWUSER) was allowed")

def test_gvisor_marker_is_present():
    assert os.path.isfile(MARKER)

def test_has_no_network():
    assert [n for _, n in socket.if_nameindex()] == ["lo"]
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

def test_tmp_is_writable_but_noexec():
    open("/workspace/generated.txt", "w").write("from the container")
    script = "/tmp/run.sh"
    with open(script, "w") as fh:
        fh.write("#!/bin/sh\\nexit 0\\n")
    os.chmod(script, 0o755)
    try:
        subprocess.run([script], check=False)
    except PermissionError:
        pass
    else:
        raise AssertionError("/tmp allows exec")

def test_workspace_tmpfs_size_is_enforced():
    written = 0
    chunk = b"x" * (1024 * 1024)
    try:
        with open("/workspace/fill", "wb") as fh:
            while written < 200 * 1024 * 1024:
                fh.write(chunk)
                fh.flush()
                written += len(chunk)
    except OSError as exc:
        assert exc.errno == errno.ENOSPC, exc
    else:
        raise AssertionError("wrote %d bytes past a 64 MiB tmpfs" % written)
    finally:
        os.unlink("/workspace/fill")
    print("PROBE " + json.dumps({{"tmpfs_written_mib": written // (1024 * 1024)}}))
    assert written <= 64 * 1024 * 1024

def test_no_docker_socket_host_paths_or_secrets():
    for sock in ("/var/run/docker.sock", "/run/docker.sock"):
        assert not os.path.exists(sock)
    assert not os.path.exists(HOST_WORKSPACE)
    assert HOST_WORKSPACE not in open("/proc/self/mountinfo").read()
    assert "worker-secret-sentinel" not in repr(dict(os.environ))
"""
    )
    result = await run_tests_in_snapshot(
        _gvisor(memory_mb=1024, workspace_tmpfs_mb=64, tmp_tmpfs_mb=32), ws
    )
    record("posture", passed=result.passed, probes=_probes(result.stdout),
           stdout=result.stdout[-4000:], stderr=result.stderr[-2000:])  # fmt: skip
    assert result.passed, result.stdout + result.stderr
    assert "9 passed" in result.stdout
    assert sorted(p.name for p in ws.iterdir()) == ["pytest.ini", "test_posture.py"]


# ── 2. Resource enforcement (judged from the host cgroup) ──────────────────────

NPROC_LIMIT = 64
NPROC_ARGS = ("--ulimit", f"nproc={NPROC_LIMIT}:{NPROC_LIMIT}")
SANDBOX_UID = 65534
EAGAIN = 11
DEFAULT_PIDS_LIMIT = DockerExecutorConfig(image="sha256:" + "0" * 64).pids_limit

# Host --pids-limit alone (no RLIMIT_NPROC).  Bounded: at most 200 threads.
_HOST_LIMIT_THREADS = """
import json, threading, time

def test_threads():
    stop = threading.Event()
    threads, error = [], None
    for _ in range(200):
        thread = threading.Thread(target=stop.wait, daemon=True)
        try:
            thread.start()
        except RuntimeError as exc:
            error = repr(exc)
            break
        threads.append(thread)
    time.sleep(2)
    stop.set()
    for thread in threads:
        thread.join(5)
    alive = sum(t.is_alive() for t in threads)
    print("PROBE " + json.dumps({"threads_started": len(threads), "error": error, "alive_after_join": alive}))
"""

# Host --pids-limit alone.  Bounded: at most 120 children, each blocked on a
# pipe.  Progress goes straight to fd 2 so it survives if the sandbox dies.
_HOST_LIMIT_FORKS = """
import json, os, time

def test_forks():
    r, w = os.pipe()
    children, error = [], None
    for _ in range(120):
        try:
            pid = os.fork()
        except OSError as exc:
            error = exc.errno
            break
        if pid == 0:
            os.close(w)
            os.read(r, 1)
            os._exit(0)
        children.append(pid)
        os.write(2, b"FORKED %d\\n" % len(children))
    time.sleep(2)
    os.close(w)
    for pid in children:
        os.waitpid(pid, 0)
    print("PROBE " + json.dumps({"forked": len(children), "errno": error}))
"""

# Shared by the RLIMIT_NPROC workloads.  Counts come from the sandbox's own
# /proc: tasks (threads) per real UID, which is what RLIMIT_NPROC limits.
_NPROC_COMMON = """
import errno, json, os, resource, threading, time

LIMIT = __LIMIT__
CAP = 200  # attempts; far below anything that could strain the runner
HOLD = __HOLD__  # seconds to stay at the limit (lets another sandbox run meanwhile)


def emit(**data):
    print("PROBE " + json.dumps(data, sort_keys=True), flush=True)


LISTING = {"duplicates": [], "max_entries": 0}


def proc_pids():
    # Unique process ids in /proc.  Repeated directory entries are recorded,
    # not counted twice: a repeated entry would inflate the count.
    names = [n for n in os.listdir("/proc") if n.isdigit()]
    seen, repeated = set(), set()
    for name in names:
        (repeated if name in seen else seen).add(name)
    if repeated:
        LISTING["duplicates"].append(sorted(int(n) for n in repeated))
    LISTING["max_entries"] = max(LISTING["max_entries"], len(names))
    return sorted(seen, key=int)


def tasks_by_uid():
    counts = {}
    for name in proc_pids():
        try:
            with open("/proc/%s/status" % name) as fh:
                uid = next(int(line.split()[1]) for line in fh if line.startswith("Uid:"))
            threads = len(os.listdir("/proc/%s/task" % name))
        except (OSError, StopIteration):
            continue
        counts[uid] = counts.get(uid, 0) + threads
    return counts


def tasks(uid):
    return tasks_by_uid().get(uid, 0)


def task_detail(uid):
    # pid, parent, state and thread count of every process owned by uid.
    found = []
    for name in proc_pids():
        try:
            with open("/proc/%s/status" % name) as fh:
                status = dict(line.split(":", 1) for line in fh if ":" in line)
            if int(status["Uid"].split()[0]) != uid:
                continue
            try:
                with open("/proc/%s/cmdline" % name, "rb") as fh:
                    cmdline = fh.read().replace(b"\\0", b" ").decode(errors="replace")[:120]
            except OSError:
                cmdline = None
            found.append({"pid": int(name), "ppid": int(status["PPid"]), "tgid": int(status["Tgid"]),
                          "name": status["Name"].strip(), "state": status["State"].split()[0],
                          "threads": len(os.listdir("/proc/%s/task" % name)), "cmdline": cmdline})
        except (OSError, KeyError, ValueError):
            continue
    return found


def limit_snapshot(uid, known=()):
    # At the limit: live tasks, entries that are not running (zombie/dead),
    # and how the visible count settles over the next second.  Processes that
    # are neither the caller nor in *known* (the children it forked) are listed
    # in full as "unexpected".
    detail = task_detail(uid)
    expected = set(known) | {os.getpid(), os.getppid()}
    unexpected = [d for d in detail if d["pid"] not in expected and d["pid"] not in BASELINE_PIDS]
    series = [sum(d["threads"] for d in detail)]
    for _ in range(10):
        time.sleep(0.1)
        series.append(tasks(uid))
    not_live = [d for d in detail if d["state"] in ("Z", "X")]
    return {
        "visible": series[0],
        "live": sum(d["threads"] for d in detail if d["state"] not in ("Z", "X")),
        "not_live": not_live,
        "series": series,
        "settled": series[-1],
        "processes": len(detail),
        "pids": sorted(d["pid"] for d in detail),
        "known": sorted(set(known)),
        "listing": {"duplicates": LISTING["duplicates"][-3:], "max_entries": LISTING["max_entries"]},
        "multi_threaded": [d for d in detail if d["threads"] > 1],
        "unexpected": unexpected,
    }


BASELINE_PIDS = set()


def remember_baseline(uid):
    BASELINE_PIDS.update(d["pid"] for d in task_detail(uid))
    return [dict(d) for d in task_detail(uid)]


def wait_for_tasks(uid, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate(tasks(uid)) and time.monotonic() < deadline:
        time.sleep(0.02)
    return tasks(uid)


def identity():
    with open("/proc/self/status") as fh:
        status = dict(line.split(":", 1) for line in fh if ":" in line)
    return {
        "uid": os.getuid(), "euid": os.geteuid(), "gid": os.getgid(), "egid": os.getegid(),
        "status_uid": status["Uid"].split(), "cap_eff": status["CapEff"].strip(),
        "nproc": list(resource.getrlimit(resource.RLIMIT_NPROC)),
    }


def failure(exc):
    code = getattr(exc, "errno", None)
    return {"type": type(exc).__name__, "errno": code, "errname": errno.errorcode.get(code or 0),
            "message": str(exc)}
"""

_NPROC_FORKS = _NPROC_COMMON + """

def test_fork_until_the_limit():
    ident = identity()
    uid = os.getuid()
    baseline = tasks(uid)
    baseline_detail = remember_baseline(uid)
    after_again = None
    r, w = os.pipe()
    children = []
    first_failure = refill = again = None
    released = 0

    def fork_one():
        pid = os.fork()
        if pid == 0:
            os.close(w)
            os.read(r, 1)
            os._exit(0)
        children.append(pid)

    try:
        for _ in range(CAP):
            try:
                fork_one()
            except OSError as exc:
                first_failure = failure(exc)
                break
        created = len(children)
        snapshot = limit_snapshot(uid, children)
        at_limit = snapshot["settled"]
        emit(stage="at_limit", created=created, at_limit=at_limit, baseline=baseline, snapshot=snapshot)
        time.sleep(HOLD)
        if first_failure is not None:
            # Releasing exactly one task makes room for exactly one more.
            os.write(w, b"x")
            done, _ = os.wait()
            children.remove(done)
            released = 1
            wait_for_tasks(uid, lambda n: n < at_limit)
            try:
                fork_one()
                refill = "ok"
            except OSError as exc:
                refill = failure(exc)
            try:
                fork_one()
                again = "ok"
            except OSError as exc:
                again = failure(exc)
            after_again = limit_snapshot(uid, children)
    finally:
        os.close(w)
        reaped = 0
        for pid in children:
            os.waitpid(pid, 0)
            reaped += 1
    after = wait_for_tasks(uid, lambda n: n <= baseline)
    emit(stage="done", identity=ident, baseline=baseline, created=created, at_limit=at_limit,
         snapshot=snapshot, after_again=after_again, baseline_detail=baseline_detail,
         children=len(children), failure=first_failure, released=released, refill=refill, again=again,
         outstanding=len(children), reaped=reaped, after=after, owners=tasks_by_uid())
"""

_NPROC_THREADS = _NPROC_COMMON + """

def test_threads_until_the_limit():
    ident = identity()
    uid = os.getuid()
    baseline = tasks(uid)
    baseline_detail = remember_baseline(uid)
    threads, events = [], []
    first_failure = refill = again = fork_at_limit = None
    released = 0

    def start_one():
        event = threading.Event()
        thread = threading.Thread(target=event.wait, daemon=True)
        thread.start()
        threads.append(thread)
        events.append(event)

    try:
        for _ in range(CAP):
            try:
                start_one()
            except RuntimeError as exc:
                first_failure = failure(exc)
                break
        created = len(threads)
        snapshot = limit_snapshot(uid)
        at_limit = snapshot["settled"]
        # The same per-user task limit must now refuse fork() too.
        try:
            pid = os.fork()
        except OSError as exc:
            fork_at_limit = failure(exc)
        else:
            if pid == 0:
                os._exit(0)
            os.waitpid(pid, 0)
            fork_at_limit = "ok"
        emit(stage="at_limit", created=created, at_limit=at_limit, baseline=baseline, snapshot=snapshot)
        time.sleep(HOLD)
        if first_failure is not None:
            events.pop().set()
            threads.pop().join(5)
            released = 1
            wait_for_tasks(uid, lambda n: n < at_limit)
            try:
                start_one()
                refill = "ok"
            except RuntimeError as exc:
                refill = failure(exc)
            try:
                start_one()
                again = "ok"
            except RuntimeError as exc:
                again = failure(exc)
    finally:
        for event in events:
            event.set()
        for thread in threads:
            thread.join(5)
    alive = sum(t.is_alive() for t in threads)
    after = wait_for_tasks(uid, lambda n: n <= baseline)
    emit(stage="done", identity=ident, baseline=baseline, created=created, at_limit=at_limit,
         snapshot=snapshot, baseline_detail=baseline_detail, failure=first_failure, fork_at_limit=fork_at_limit,
         released=released, refill=refill,
         again=again, outstanding=len(threads), alive_after_join=alive, after=after,
         owners=tasks_by_uid())
"""


def _nproc_workload(kind: str, hold: float = 1.5) -> str:
    body = _NPROC_FORKS if kind == "forks" else _NPROC_THREADS
    return body.replace("__LIMIT__", str(NPROC_LIMIT)).replace("__HOLD__", repr(float(hold)))


def _check_nproc_probe(done: dict[str, Any], kind: str) -> None:
    """Everything a completed RLIMIT_NPROC workload must show."""
    ident = done["identity"]
    # Applied, and to the intended non-root identity without a bypass capability.
    assert ident["nproc"] == [NPROC_LIMIT, NPROC_LIMIT], ident
    assert ident["uid"] == ident["euid"] == ident["gid"] == ident["egid"] == SANDBOX_UID, ident
    assert ident["status_uid"] == [str(SANDBOX_UID)] * 4, ident
    assert int(ident["cap_eff"], 16) == 0, ident  # no CAP_SYS_RESOURCE / CAP_SYS_ADMIN
    # Every task in the sandbox belongs to that identity (counted by real UID).
    assert set(done["owners"]) == {str(SANDBOX_UID)}, done["owners"]
    # Creation stopped exactly at the configured limit, through the rlimit path.
    assert 0 < done["baseline"] < NPROC_LIMIT
    assert done["created"] == NPROC_LIMIT - done["baseline"], done
    # At the limit exactly NPROC_LIMIT tasks are live.  An entry briefly
    # visible beyond that must not be running (zombie/dead), and the visible
    # count must settle at the limit.
    snap = done["snapshot"]
    assert snap["live"] == NPROC_LIMIT, snap
    assert snap["visible"] == NPROC_LIMIT + sum(d["threads"] for d in snap["not_live"]), snap
    assert snap["settled"] == done["at_limit"] == NPROC_LIMIT, snap
    assert max(snap["series"]) == snap["visible"], snap  # never grew after the refusal
    # Exactly the processes the workload accounts for: the baseline plus the
    # children it forked (none for threads); nothing unknown.
    assert snap["unexpected"] == [], snap["unexpected"]
    expected_pids = {b["pid"] for b in done["baseline_detail"]} | set(snap["known"])
    assert set(snap["pids"]) == expected_pids, (sorted(set(snap["pids"]) ^ expected_pids), snap["listing"])
    assert len(snap["known"]) == (done["created"] if kind == "forks" else 0), snap["known"]
    if kind == "forks":
        expected = {"type": "BlockingIOError", "errno": EAGAIN, "errname": "EAGAIN"}
    else:
        assert done["failure"]["type"] == "RuntimeError", done["failure"]
        assert "can't start new thread" in done["failure"]["message"], done["failure"]
        assert done["fork_at_limit"] not in (None, "ok"), done
        fork = done["fork_at_limit"]
        assert (fork["errno"], fork["errname"]) == (EAGAIN, "EAGAIN"), fork
        expected = {"type": "RuntimeError"}
    for key, value in expected.items():
        assert done["failure"][key] == value, done["failure"]
    # One released task makes room for exactly one more; then the limit holds again.
    assert done["released"] == 1 and done["refill"] == "ok", done
    assert done["again"] not in (None, "ok"), done
    if kind == "forks":
        assert done["again"]["errname"] == "EAGAIN", done["again"]
    else:
        assert done["again"]["type"] == "RuntimeError", done["again"]
    # Everything created was cleaned up.
    if kind == "forks":
        assert done["reaped"] == done["outstanding"] == done["created"], done
    else:
        assert done["alive_after_join"] == 0 and done["outstanding"] == done["created"], done
    assert done["after"] == done["baseline"], done


async def _nproc_run(tmp_path: Path, kind: str, name: str, hold: float = 1.5) -> LiveRun:
    config = GuardedConfig(image=IMAGE, timeout_seconds=120)
    return await LiveRun.start(config, GVISOR, _probe_ws(tmp_path, _nproc_workload(kind, hold), name), NPROC_ARGS)


@pytest.mark.asyncio
async def test_host_pids_limit_bounds_runsc_host_tasks_but_not_guest_threads(
    tmp_path: Path, no_leftovers: set[str]
) -> None:
    """Finding pinned: --pids-limit counts runsc's host tasks, not the guest's threads.

    The host limit is still applied and never exceeded, but the guest starts
    more threads than it allows, so it cannot be the guest task bound.  If this
    assertion ever flips, the finding (and the need for RLIMIT_NPROC) must be
    re-evaluated, not silently dropped.
    """
    _require_runtime(GVISOR)
    obs = await _observe(_gvisor(pids_limit=64), _probe_ws(tmp_path, _HOST_LIMIT_THREADS))
    probes = _probes(obs.result.stdout)
    record("host_pids_threads", probes=probes, exit_code=obs.result.exit_code, passed=obs.result.passed,
           stderr=obs.result.stderr[-1500:], pids_max=obs.samples[0]["pids.max"],
           peak_pids_current=_peak(obs.samples, "pids.current"), pids_peak=_peak(obs.samples, "pids.peak"),
           peak_host_threads=_peak(obs.samples, "host_threads"),
           host_processes=obs.samples[0]["processes"])  # fmt: skip
    assert obs.result.passed and probes, obs.result.stdout[-500:] + obs.result.stderr[-500:]
    assert obs.samples[0]["pids.max"] == "64"
    assert (_peak(obs.samples, "pids.peak") or 0) <= 64
    assert (_peak(obs.samples, "pids.current") or 0) <= 64
    assert probes[0]["threads_started"] > 64, "host --pids-limit now bounds guest threads: re-evaluate"
    assert probes[0]["alive_after_join"] == 0


@pytest.mark.asyncio
async def test_host_pids_limit_holds_when_guest_processes_exhaust_it(tmp_path: Path, no_leftovers: set[str]) -> None:
    """Without RLIMIT_NPROC, guest processes run into the host task limit.

    Every guest process costs the sandbox host tasks, so the host limit is what
    stops a bounded fork loop here.  It must hold (never exceeded, runner
    unaffected, sandbox fully removed).  Whether the guest sees a clean EAGAIN
    is recorded, not assumed: the earlier run ended abnormally with no output.
    """
    _require_runtime(GVISOR)
    obs = await _observe(_gvisor(pids_limit=64), _probe_ws(tmp_path, _HOST_LIMIT_FORKS))
    leftovers = await _assert_sandbox_gone(obs.container)
    probes = _probes(obs.result.stdout)
    progress = [int(line.split()[1]) for line in obs.result.stderr.splitlines() if line.startswith("FORKED ")]
    reached = max(progress, default=0)
    peak_tasks = _peak(obs.samples, "pids.current") or 0
    record("host_pids_processes", probes=probes, forks_reported=reached, exit_code=obs.result.exit_code,
           passed=obs.result.passed, graceful=bool(probes) and obs.result.passed,
           stdout=obs.result.stdout[-800:], stderr_tail=obs.result.stderr[-800:],
           pids_max=obs.samples[0]["pids.max"], peak_pids_current=peak_tasks,
           pids_peak=_peak(obs.samples, "pids.peak"), peak_host_threads=_peak(obs.samples, "host_threads"),
           first_pids_current=_num(obs.samples[0]["pids.current"]),
           # Approximate: the first sample may already include some forks.
           host_tasks_added_per_guest_process=(
               round((peak_tasks - (_num(obs.samples[0]["pids.current"]) or 0)) / reached, 2) if reached else None
           ),
           leftovers=leftovers, samples=len(obs.samples))  # fmt: skip
    assert obs.samples[0]["pids.max"] == "64"
    assert (_peak(obs.samples, "pids.peak") or 0) <= 64
    assert peak_tasks <= 64
    assert reached < 120, "the bounded fork loop was never stopped"
    assert leftovers == [] and obs.container not in _managed()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["forks", "threads"])
async def test_rlimit_nproc_bounds_tasks_inside_the_sandbox(
    tmp_path: Path, no_leftovers: set[str], kind: str
) -> None:
    """--ulimit nproc=64:64 is applied inside gVisor and stops creation at 64 tasks.

    The host --pids-limit stays at its default so the rlimit, not the host
    limit, is what is exercised; the host peak shows the headroom.
    """
    _require_runtime(GVISOR)
    run = await _nproc_run(tmp_path, kind, kind)
    sampler = asyncio.create_task(_sample(run))
    try:
        result = await run.finish(timeout=180)
        samples = await sampler
    finally:
        leftovers = await run.remove()
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
    done = run.probe("done")
    record("nproc_" + kind, exit_code=result.exit_code, runtime=result.runtime, docker_ulimits=run.ulimits,
           at_limit=run.probe("at_limit"), done=done, stderr=result.stderr[-1500:],
           pids_max=samples[0]["pids.max"] if samples else None,
           peak_pids_current=_peak(samples, "pids.current"), pids_peak=_peak(samples, "pids.peak"),
           host_process_uids=sorted({p["uid"] for p in samples[0]["processes"]}) if samples else None,
           samples=len(samples), leftovers=leftovers)  # fmt: skip
    assert result.runtime == GVISOR
    assert result.exit_code == 0 and done is not None, result.stdout[-1000:] + result.stderr[-1000:]
    _check_nproc_probe(done, kind)
    # The host limit is applied, untouched by this test, and never the binding constraint.
    assert samples and samples[0]["pids.max"] == str(DEFAULT_PIDS_LIMIT)
    assert (_peak(samples, "pids.peak") or 0) < DEFAULT_PIDS_LIMIT
    assert leftovers == []


@pytest.mark.asyncio
async def test_rlimit_nproc_is_per_sandbox_not_shared_between_sandboxes(
    tmp_path: Path, no_leftovers: set[str]
) -> None:
    """Two concurrent gVisor sandboxes, same UID 65534 and nproc=64.

    Docker documents nproc as a per-real-UID limit.  Under runc that UID is
    shared across containers on the host; inside gVisor each sandbox has its
    own kernel and its own count.  Sandbox A holds its limit while B runs: B
    must still reach exactly its own limit, and neither may exceed it.
    """
    _require_runtime(GVISOR)
    runs: list[LiveRun] = []
    samplers: list[asyncio.Task[list[dict[str, Any]]]] = []
    try:
        a = await _nproc_run(tmp_path, "forks", "a", hold=25)
        runs.append(a)
        samplers.append(asyncio.create_task(_sample(a)))
        a_at_limit = await a.wait_for_probe("at_limit", 90)

        b = await _nproc_run(tmp_path, "forks", "b", hold=1.5)
        runs.append(b)
        samplers.append(asyncio.create_task(_sample(b)))
        b_at_limit = await b.wait_for_probe("at_limit", 90)
        a_running_while_b_at_limit = a.running() and a.probe("done") is None
        b_result = await b.finish(timeout=120)
        a_running_after_b = a.running() and a.probe("done") is None
        a_result = await a.finish(timeout=120)
        samples_a, samples_b = await samplers[0], await samplers[1]
    finally:
        leftovers = {run.name: await run.remove() for run in runs}
        for sampler in samplers:
            sampler.cancel()
        await asyncio.gather(*samplers, return_exceptions=True)

    a_done, b_done = a.probe("done"), b.probe("done")

    def host(samples: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "pids_max": samples[0]["pids.max"] if samples else None,
            "peak_pids_current": _peak(samples, "pids.current"),
            "pids_peak": _peak(samples, "pids.peak"),
            "memory_peak": _peak(samples, "memory.peak"),
            "memory_max": samples[0]["memory.max"] if samples else None,
            "host_process_uids": sorted({p["uid"] for p in samples[0]["processes"]}) if samples else None,
        }

    record("nproc_two_sandboxes", a_at_limit=a_at_limit, b_at_limit=b_at_limit,
           a_running_while_b_at_limit=a_running_while_b_at_limit, a_running_after_b=a_running_after_b,
           a_done=a_done, b_done=b_done, a_exit=a_result.exit_code, b_exit=b_result.exit_code,
           host_a=host(samples_a), host_b=host(samples_b), leftovers=leftovers)  # fmt: skip
    assert a_result.exit_code == 0 and b_result.exit_code == 0, a_result.stderr[-800:] + b_result.stderr[-800:]
    assert a_result.runtime == b_result.runtime == GVISOR
    assert a_done is not None and b_done is not None
    # The two sandboxes really overlapped: A was holding its limit throughout B's run.
    assert a_at_limit["at_limit"] == NPROC_LIMIT
    assert a_running_while_b_at_limit and a_running_after_b
    # B was not starved by A's 64 tasks of the same UID, and reached exactly its own limit.
    _check_nproc_probe(a_done, "forks")
    _check_nproc_probe(b_done, "forks")
    assert b_at_limit["created"] == NPROC_LIMIT - b_at_limit["baseline"]
    # Host use stayed bounded for both sandboxes.
    for samples in (samples_a, samples_b):
        assert samples and samples[0]["pids.max"] == str(DEFAULT_PIDS_LIMIT)
        assert (_peak(samples, "pids.peak") or 0) < DEFAULT_PIDS_LIMIT
        assert (_peak(samples, "memory.peak") or 0) <= (_num(samples[0]["memory.max"]) or 0)
    assert all(found == [] for found in leftovers.values()), leftovers


@pytest.mark.asyncio
async def test_memory_limit_is_enforced_on_the_sandbox(tmp_path: Path, no_leftovers: set[str]) -> None:
    _require_runtime(GVISOR)
    body = """
import json, time

def test_allocate():
    blocks = []
    for i in range(12):
        blocks.append(bytearray(64 * 1024 * 1024))
        print("PROBE " + json.dumps({"allocated_mib": 64 * (i + 1)}), flush=True)
        time.sleep(0.3)
"""
    obs = await _observe(_gvisor(memory_mb=320, workspace_tmpfs_mb=64, tmp_tmpfs_mb=32),
                         _probe_ws(tmp_path, body))  # fmt: skip
    probes = _probes(obs.result.stdout)
    limit = 320 * MIB
    record("memory", passed=obs.result.passed, exit_code=obs.result.exit_code,
           last_allocated=probes[-1] if probes else None, memory_max=obs.samples[0]["memory.max"],
           swap_max=obs.samples[0]["memory.swap.max"], peak_current=_peak(obs.samples, "memory.current"),
           memory_peak=_peak(obs.samples, "memory.peak"), events=obs.samples[-1]["memory.events"],
           stderr=obs.result.stderr[-1500:])  # fmt: skip
    assert _num(obs.samples[0]["memory.max"]) == limit
    assert obs.samples[0]["memory.swap.max"] == "0"
    assert (_peak(obs.samples, "memory.current") or 0) <= limit
    assert not obs.result.passed
    assert not probes or probes[-1]["allocated_mib"] < 768


@pytest.mark.asyncio
async def test_cpu_limit_is_enforced_on_the_sandbox(tmp_path: Path, no_leftovers: set[str]) -> None:
    _require_runtime(GVISOR)
    body = """
import os, time

def test_burn():
    children = []
    for _ in range(3):
        pid = os.fork()
        if pid == 0:
            end = time.monotonic() + 8
            while time.monotonic() < end:
                pass
            os._exit(0)
        children.append(pid)
    for pid in children:
        os.waitpid(pid, 0)
"""
    obs = await _observe(_gvisor(cpus=0.5), _probe_ws(tmp_path, body), every=0.5)
    usage = [(s["t"], _stat(s["cpu.stat"], "usage_usec")) for s in obs.samples]
    usage = [(t, u) for t, u in usage if u is not None]
    # The busiest window of at least 3 s, in CPUs.
    rate = max(
        ((u2 - u1) / 1e6 / (t2 - t1) for i, (t1, u1) in enumerate(usage) for t2, u2 in usage[i + 1:] if t2 - t1 >= 3),
        default=None,
    )  # fmt: skip
    record("cpu", cpu_max=obs.samples[0]["cpu.max"], busiest_rate_cpus=rate,
           throttled_usec=_stat(obs.samples[-1]["cpu.stat"], "throttled_usec"), passed=obs.result.passed)  # fmt: skip
    assert obs.result.passed, obs.result.stdout + obs.result.stderr
    assert obs.samples[0]["cpu.max"] == "50000 100000"
    assert rate is not None and 0.2 < rate <= 0.65, rate


# ── 3. Timeout, cancellation, lease loss, orphans ──────────────────────────────


async def _assert_sandbox_gone(container: str, timeout: float = 15) -> list[str]:
    deadline = time.monotonic() + timeout
    leftovers = _host_processes_mentioning(container)
    while leftovers and time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        leftovers = _host_processes_mentioning(container)
    return leftovers


@pytest.mark.asyncio
async def test_timeout_removes_the_container_and_its_sandbox(tmp_path: Path, no_leftovers: set[str]) -> None:
    _require_runtime(GVISOR)
    body = "import time\n\ndef test_hang():\n    time.sleep(300)\n"
    start = time.monotonic()
    obs = await _observe(_gvisor(timeout_seconds=5), _probe_ws(tmp_path, body))
    leftovers = await _assert_sandbox_gone(obs.container)
    record("timeout", elapsed=round(time.monotonic() - start, 1), exit_code=obs.result.exit_code,
           host_processes_during=obs.samples[0]["processes"], leftovers=leftovers)  # fmt: skip
    assert not obs.result.passed and obs.result.exit_code == -1
    assert "timed out after 5s" in obs.result.stderr
    assert obs.container not in _managed()
    assert leftovers == []


@pytest.mark.asyncio
async def test_lease_loss_stops_only_that_jobs_sandbox(tmp_path: Path, no_leftovers: set[str]) -> None:
    _require_runtime(GVISOR)
    hang = "import time\n\ndef test_hang():\n    time.sleep(300)\n"
    slow_pass = "import time\n\ndef test_slow():\n    time.sleep(8)\n"
    lost_a, lost_b = asyncio.Event(), asyncio.Event()
    async with _jobs() as jobs:
        job_a = asyncio.create_task(
            run_until_ownership_lost(run_tests_in_snapshot(_gvisor(), _probe_ws(tmp_path, hang, "a")), lost_a)
        )
        jobs.append(job_a)
        [container_a] = await _wait_for_new(no_leftovers)
        job_b = asyncio.create_task(
            run_until_ownership_lost(run_tests_in_snapshot(_gvisor(), _probe_ws(tmp_path, slow_pass, "b")), lost_b)
        )
        jobs.append(job_b)
        [container_b] = await _wait_for_new(no_leftovers, count=2) - {container_a}

        lost_a.set()
        with pytest.raises(OwnershipLostError):
            await job_a
        leftovers = await _assert_sandbox_gone(container_a)
        remaining = _managed()
        record("lease_loss", a_removed=container_a not in remaining, b_running=container_b in _running(remaining),
               leftovers=leftovers)  # fmt: skip
        assert container_a not in remaining and leftovers == []
        assert container_b in _running(remaining)
        assert (await job_b).passed


@pytest.mark.asyncio
async def test_orphaned_sandbox_stops_at_its_own_deadline(tmp_path: Path, no_leftovers: set[str]) -> None:
    _require_runtime(GVISOR)
    body = (
        "import subprocess, time\n\n"
        "def test_hang():\n"
        "    subprocess.Popen(['sleep', '300'], start_new_session=True)\n"
        "    time.sleep(300)\n"
    )
    config = GuardedConfig(image=IMAGE, timeout_seconds=2, container_grace_seconds=1)
    run = await _raw_run(config, GVISOR, _probe_ws(tmp_path, body))
    record("orphan_deadline", status=run.status, exit_code=run.exit_code, duration=run.duration)
    assert (run.status, run.exit_code) == ("exited", DEADLINE_EXIT)
    assert run.duration < 60
    assert "container deadline exceeded" in run.stderr


# ── 4. Fail closed: refusals before any repository code runs ───────────────────


@pytest.mark.asyncio
async def test_unregistered_runtime_is_refused(tmp_path: Path, no_leftovers: set[str]) -> None:
    executor = SpikeGVisorExecutor(
        GuardedConfig(image=IMAGE, timeout_seconds=60), UNREGISTERED,
        GVisorRuntimeRequirement(name=UNREGISTERED, path=RUNSC),
    )  # fmt: skip
    since = time.time()
    with pytest.raises(ExecutionUnavailableError, match="is not registered") as exc:
        await run_tests_in_snapshot(executor, _sentinel_ws(tmp_path))
    created = _events(since, "create")
    # Docker itself also refuses the name.
    direct = subprocess.run(  # nosec B603 - fixed binary
        [_docker_bin(), "create", "--runtime", UNREGISTERED, IMAGE], capture_output=True, text=True,
        timeout=60, check=False,
    )  # fmt: skip
    if direct.returncode == 0:
        _docker("rm", "--force", direct.stdout.strip())
    record("refused_unregistered", error=str(exc.value), creates=len(created),
           docker_create_rc=direct.returncode, docker_create_stderr=direct.stderr.strip()[:300])  # fmt: skip
    assert created == []
    assert direct.returncode != 0


_MISREGISTERED = [
    (IMPOSTOR, "path is"),
    (NO_SECCOMP, "required runtime argument --oci-seccomp is missing"),
    ("codeforge-gvisor-no-network-none", "required runtime argument --network=none is missing"),
    (NO_MARKER, "required runtime argument --gvisor-marker-file is missing"),
    (DIRECTFS, "required runtime argument --directfs=false is missing"),
    ("codeforge-gvisor-host-network", "'--network=host' is not permitted (network)"),
    ("codeforge-gvisor-debug", "'--debug' is not permitted (debugging)"),
    ("codeforge-gvisor-flag-override", "'--allow-flag-override' is not permitted (flag override)"),
    ("codeforge-gvisor-testonly", "'--TESTONLY-unsafe-nonroot' is not permitted (test-only)"),
    ("codeforge-gvisor-host-uds", "'--host-uds=all' is not permitted (host access)"),
    ("codeforge-gvisor-unknown-flag", "'--codeforge-unknown-flag' is not permitted (unrecognized)"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("runtime", "expected"), _MISREGISTERED, ids=[r for r, _ in _MISREGISTERED])
async def test_misregistered_runtime_is_refused_before_any_container_exists(
    tmp_path: Path, no_leftovers: set[str], runtime: str, expected: str
) -> None:
    _require_runtime(runtime)
    executor = SpikeGVisorExecutor(
        GuardedConfig(image=IMAGE, timeout_seconds=60), runtime,
        GVisorRuntimeRequirement(name=runtime, path=RUNSC),
    )  # fmt: skip
    since = time.time()
    with pytest.raises(ExecutionUnavailableError, match="gVisor runtime verification failed") as exc:
        await run_tests_in_snapshot(executor, _sentinel_ws(tmp_path))
    created = _events(since, "create")
    record("refused_registration", runtime=runtime, error=str(exc.value), creates=len(created))
    assert expected in str(exc.value)
    assert created == []


def test_runc_at_the_configured_path_is_caught_by_its_features_alone() -> None:
    """Operator error: the impostor's runc path was configured as the gVisor path."""
    entry = _require_runtime(IMPOSTOR)
    req = GVisorRuntimeRequirement(name=IMPOSTOR, path=str(entry["path"]))
    problems = daemon_runtime_violations(_info(), req)
    record("refused_impostor_features", runtime=IMPOSTOR, path=entry["path"], problems=problems,
           status_keys=sorted(entry.get("status") or {}))  # fmt: skip
    assert problems == runtime_features_violations(entry) != []


def test_dockers_own_runc_is_identified_as_runc_by_its_features() -> None:
    """Real data for the features check: Docker's runc entry reports runc, not gVisor."""
    _require_runtime(GVISOR)
    runtimes = _info().get("Runtimes") or {}
    runc_problems = runtime_features_violations(runtimes.get("runc") or {})
    gvisor_problems = runtime_features_violations(runtimes[GVISOR])
    record("features_real_data", runc=runc_problems, gvisor=gvisor_problems)
    assert runc_problems == ["the runtime does not report gVisor features", "the runtime reports runc features"]
    assert gvisor_problems == []


@pytest.mark.asyncio
async def test_runtime_selection_mismatch_is_refused_before_start(tmp_path: Path, no_leftovers: set[str]) -> None:
    """Host checks pass for the gVisor runtime, but the container asks for runc."""
    _require_runtime(GVISOR)
    executor = SpikeGVisorExecutor(GuardedConfig(image=IMAGE, timeout_seconds=60), "runc", REQ)
    since = time.time()
    with pytest.raises(ExecutionUnavailableError, match="Runtime is 'runc', expected 'codeforge-gvisor'") as exc:
        await run_tests_in_snapshot(executor, _sentinel_ws(tmp_path))
    created, started = _events(since, "create"), _events(since, "start")
    record("refused_mismatch", error=str(exc.value), creates=len(created), starts=len(started))
    assert len(created) == 1 and started == []


@pytest.mark.asyncio
async def test_guard_refuses_under_runc_and_repository_code_never_runs(
    tmp_path: Path, no_leftovers: set[str]
) -> None:
    """Even with every host-side check skipped, runc never receives the workspace."""
    run = await _raw_run(GuardedConfig(image=IMAGE, timeout_seconds=60), "runc", _sentinel_ws(tmp_path, "raw"))
    record("guard_runc", exit_code=run.exit_code, runtime=run.runtime, stderr=run.stderr[-500:],
           sentinel=SENTINEL in run.stdout + run.stderr)  # fmt: skip
    assert run.runtime == "runc" and run.exit_code == RUNTIME_GUARD_EXIT
    assert GUARD_REFUSAL in run.stderr
    assert SENTINEL not in run.stdout + run.stderr
    with pytest.raises(ExecutionUnavailableError, match="marker was missing"):
        await run_tests_in_snapshot(_unverified("runc"), _sentinel_ws(tmp_path, "executor"))


@pytest.mark.asyncio
async def test_guard_refuses_under_gvisor_without_the_marker_flag(tmp_path: Path, no_leftovers: set[str]) -> None:
    """The marker comes from --gvisor-marker-file: gVisor itself is not enough."""
    _require_runtime(NO_MARKER)
    run = await _raw_run(GuardedConfig(image=IMAGE, timeout_seconds=60), NO_MARKER, _sentinel_ws(tmp_path))
    record("guard_no_marker", exit_code=run.exit_code, runtime=run.runtime, sentinel=SENTINEL in run.stdout)
    assert run.exit_code == RUNTIME_GUARD_EXIT
    assert SENTINEL not in run.stdout + run.stderr


@pytest.mark.asyncio
async def test_impostor_never_runs_repository_code_even_unverified(tmp_path: Path, no_leftovers: set[str]) -> None:
    """runc under the gVisor name, with the host checks skipped."""
    _require_runtime(IMPOSTOR)
    with pytest.raises(ExecutionUnavailableError) as exc:
        await run_tests_in_snapshot(_unverified(IMPOSTOR), _sentinel_ws(tmp_path, "executor"))
    raw_error = None
    try:
        run = await _raw_run(GuardedConfig(image=IMAGE, timeout_seconds=60), IMPOSTOR, _sentinel_ws(tmp_path, "raw"))
        output = run.stdout + run.stderr
        raw = {"status": run.status, "exit_code": run.exit_code, "start_rc": run.start_rc, "stderr": run.stderr[-500:]}
    except subprocess.CalledProcessError as err:  # create itself may be refused
        output, raw, raw_error = "", {}, (err.stderr or "")[:500]
    record("impostor_unverified", executor_error=str(exc.value), raw=raw, raw_error=raw_error,
           sentinel=SENTINEL in output)  # fmt: skip
    assert SENTINEL not in output


@pytest.mark.asyncio
async def test_sentinel_positive_control(tmp_path: Path, no_leftovers: set[str]) -> None:
    """The sentinel is visible when repository code does run (under verified gVisor)."""
    _require_runtime(GVISOR)
    assert daemon_runtime_violations(_info(), REQ) == []
    run = await _raw_run(GuardedConfig(image=IMAGE, timeout_seconds=60), GVISOR, _sentinel_ws(tmp_path, "raw"))
    result = await run_tests_in_snapshot(_gvisor(), _sentinel_ws(tmp_path, "executor"))
    record("sentinel_control", raw_exit=run.exit_code, raw_sentinel=SENTINEL in run.stdout,
           executor_passed=result.passed, executor_sentinel=SENTINEL in result.stdout)  # fmt: skip
    assert run.exit_code == 0 and SENTINEL in run.stdout
    assert result.passed and SENTINEL in result.stdout


# ── 5. Evaluations (recorded; the assertions only check that runs completed) ──


@pytest.mark.asyncio
async def test_evaluate_directfs_false(tmp_path: Path, no_leftovers: set[str]) -> None:
    _require_runtime(GVISOR)
    _require_runtime(DIRECTFS)
    body = """
import os

def test_files():
    for i in range(2000):
        with open("/workspace/f%d" % i, "w") as fh:
            fh.write("x" * 256)
    assert sum(os.path.getsize("/workspace/f%d" % i) for i in range(2000)) == 2000 * 256
    total = 0
    for root, _, files in os.walk("/usr/local/lib"):
        total += len(files)
    assert total > 100
"""
    durations: dict[str, list[float]] = {GVISOR: [], DIRECTFS: []}
    for i in range(3):
        for runtime in (GVISOR, DIRECTFS):
            run = await _raw_run(GuardedConfig(image=IMAGE, timeout_seconds=120), runtime,
                                 _probe_ws(tmp_path, body, f"{runtime}-{i}"))  # fmt: skip
            assert run.exit_code == 0, run.stdout + run.stderr
            durations[runtime].append(run.duration)
    record("evaluate_directfs", durations=durations)


@pytest.mark.asyncio
async def test_evaluate_oci_seccomp(tmp_path: Path, no_leftovers: set[str]) -> None:
    """What --oci-seccomp changes: the same probe under a runtime without it."""
    _require_runtime(NO_SECCOMP)
    body = """
import json, os

def test_probe():
    status = open("/proc/self/status").read()
    mode = status.split("Seccomp:")[1].split()[0] if "Seccomp:" in status else None
    try:
        os.unshare(os.CLONE_NEWUSER)
        unshare = "allowed"
    except OSError as exc:
        unshare = "refused errno=%d" % exc.errno
    print("PROBE " + json.dumps({"seccomp": mode, "unshare_newuser": unshare}))
"""
    run = await _raw_run(GuardedConfig(image=IMAGE, timeout_seconds=60), NO_SECCOMP, _probe_ws(tmp_path, body))
    record("evaluate_oci_seccomp", runtime=NO_SECCOMP, exit_code=run.exit_code, probes=_probes(run.stdout))
    assert run.exit_code == 0, run.stdout + run.stderr
