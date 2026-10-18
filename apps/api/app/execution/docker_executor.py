"""Hardened Docker test execution (Phase 10B.5 Stage 1C).

Runs a workspace snapshot's test suite in a throwaway container.  The design,
threat model and residual risks are in ``docs/security/docker-test-executor.md``;
in short:

* No host path is mounted.  The snapshot archive is re-verified on the host,
  streamed to the container on stdin, and extracted inside it onto a tmpfs that
  disappears with the container, so nothing the tests write is copied back.
* The container runs as 65534:65534 with every capability dropped,
  no-new-privileges, a read-only root filesystem, no network, private
  namespaces, an init process, no log driver, and memory/swap/CPU/PIDs limits.
  The image must be digest-pinned and is never pulled.
* After ``docker create`` the container is inspected, and it is only started if
  Docker applied every one of those settings exactly; ``docker info`` must
  report seccomp and limit support.  Anything else raises
  :class:`~app.execution.test_executor.ExecutionUnavailableError`.  There is
  no fallback to the host and no relaxed retry.
* Every container gets a fresh random name, and is force-removed when the run
  ends for any reason: success, test failure, timeout, error or cancellation
  (which is how a lost lease stops it).  Cleanup only ever names the run's own
  container.

Docker shares the host kernel.  This is a container boundary, not complete
isolation, and it reports :attr:`IsolationLevel.CONTAINER`, which does not
permit automatic repairs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import time
import uuid
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from app.config import PINNED_IMAGE_PATTERN, Settings
from app.execution.output import MAX_OUTPUT_BYTES, BoundedBuffer
from app.execution.test_executor import ExecutionUnavailableError, IsolationLevel
from app.schemas.task import TestResult
from app.workspace.snapshot import (
    WorkspaceSnapshot,
    private_tempdir,
    run_blocking,
    verify_snapshot_archive,
)

logger = logging.getLogger(__name__)

# "nobody": not root in the container, and not a real user on the host.
CONTAINER_USER = "65534:65534"
WORKSPACE_DIR = "/workspace"
NAME_PREFIX = "codeforge-test-"
MANAGED_LABEL_KEY = "codeforge.managed"
MANAGED_LABEL_VALUE = "test-executor"
# Exit status the in-container bootstrap uses when the snapshot cannot be extracted.
EXTRACTION_FAILED_EXIT = 97
# Exit status when the container's own deadline (timeout + grace) expires.
DEADLINE_EXIT = 124
ENTRYPOINT = "python"
PYTEST_ARGS: tuple[str, ...] = ("-m", "pytest", "--tb=short", "-q", "-p", "no:cacheprovider")
# The only environment the container gets (plus the image's own ENV).  The
# /tmp paths here and below are inside the container (a private tmpfs), not
# temporary files on the host.
CONTAINER_ENV: dict[str, str] = {
    "HOME": "/tmp",  # nosec B108
    "TMPDIR": "/tmp",  # nosec B108
    "PYTHONPATH": WORKSPACE_DIR,
    "PYTHONDONTWRITEBYTECODE": "1",
}
ULIMITS: tuple[tuple[str, int, int], ...] = (("core", 0, 0), ("nofile", 1024, 1024))
# Variables the docker CLI may see.  DOCKER_CONFIG is always replaced by an
# empty private directory: no client-side proxy injection or credential helpers.
_CLI_ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")
_MANAGEMENT_OUTPUT_BYTES = 1024 * 1024
_STREAM_CHUNK = 256 * 1024
# How long to wait for the CLI and its pipes once the container is gone.
_DRAIN_SECONDS = 5.0

# Runs with ``python -I`` and one argument, the container's own deadline in
# seconds.  Extracts the snapshot from stdin onto the workspace tmpfs (the
# ``data`` filter rejects absolute paths, links leaving the target and special
# files), runs pytest as a child, and passes on its exit status.  If the worker
# never removes the container (for example because it crashed), the deadline
# kills every process in the container so it stops instead of running forever.
# Repository code runs as the same user and could defeat this; it is a
# backstop for orphaned containers, not a security boundary.
BOOTSTRAP = f"""\
import os, signal, sys, tarfile, time
try:
    deadline = time.monotonic() + float(sys.argv[1])
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
        archive.extractall({WORKSPACE_DIR!r}, filter="data")
    while sys.stdin.buffer.read(65536):
        pass
except Exception as exc:
    sys.stderr.write("codeforge: snapshot extraction failed: %s\\n" % type(exc).__name__)
    sys.exit({EXTRACTION_FAILED_EXIT})
null = os.open(os.devnull, os.O_RDONLY)
os.dup2(null, 0)
os.close(null)
os.chdir({WORKSPACE_DIR!r})
child = os.fork()
if child == 0:
    try:
        os.execv(sys.executable, [sys.executable, *{list(PYTEST_ARGS)!r}])
    finally:
        os._exit(127)
while True:
    pid, status = os.waitpid(child, os.WNOHANG)
    if pid:
        code = os.waitstatus_to_exitcode(status)
        os._exit(128 - code if code < 0 else code)
    if time.monotonic() >= deadline:
        sys.stderr.write("codeforge: container deadline exceeded\\n")
        sys.stderr.flush()
        os.kill(-1, signal.SIGKILL)
        os._exit({DEADLINE_EXIT})
    time.sleep(0.25)
"""


@dataclass(frozen=True)
class DockerExecutorConfig:
    """Resource limits and image for :class:`DockerTestExecutor`.  Validated on creation."""

    image: str
    cli: str = "docker"
    timeout_seconds: float = 300
    cpus: float = 1.0
    memory_mb: int = 2048
    pids_limit: int = 256
    workspace_tmpfs_mb: int = 512
    tmp_tmpfs_mb: int = 256
    max_output_bytes: int = MAX_OUTPUT_BYTES
    management_timeout_seconds: float = 30.0
    # Added to timeout_seconds for the container's own deadline (see BOOTSTRAP).
    container_grace_seconds: float = 60.0

    def __post_init__(self) -> None:
        problems: list[str] = []
        if not PINNED_IMAGE_PATTERN.fullmatch(self.image):
            problems.append(
                "the test image must be pinned by digest (name@sha256:<digest> or sha256:<id>)"
            )
        if not self.cli or self.cli.startswith("-") or any(c.isspace() for c in self.cli):
            problems.append("the Docker CLI path is invalid")
        if not 0 < self.timeout_seconds <= 3600:
            problems.append("timeout_seconds must be in (0, 3600]")
        if not 0 < self.cpus <= 64:
            problems.append("cpus must be in (0, 64]")
        if not 16 <= self.pids_limit <= 65536:
            problems.append("pids_limit must be in [16, 65536]")
        if self.workspace_tmpfs_mb < 1 or self.tmp_tmpfs_mb < 1:
            problems.append("tmpfs sizes must be positive")
        # tmpfs pages are charged to the container's memory limit.
        if self.memory_mb < self.workspace_tmpfs_mb + self.tmp_tmpfs_mb + 128:
            problems.append("memory_mb must exceed the tmpfs sizes by at least 128 MiB")
        if (
            self.max_output_bytes < 1
            or self.management_timeout_seconds <= 0
            or self.container_grace_seconds <= 0
        ):
            problems.append("output, management and grace limits must be positive")
        if problems:
            raise ExecutionUnavailableError(
                "Docker test execution is misconfigured: " + "; ".join(problems)
            )

    @classmethod
    def from_settings(cls, settings: Settings) -> DockerExecutorConfig:
        return cls(
            image=settings.codeforge_docker_test_image,
            cli=settings.codeforge_docker_cli,
            timeout_seconds=settings.codeforge_docker_timeout_seconds,
            cpus=settings.codeforge_docker_cpus,
            memory_mb=settings.codeforge_docker_memory_mb,
            pids_limit=settings.codeforge_docker_pids_limit,
        )

    @property
    def memory_bytes(self) -> int:
        return self.memory_mb * 1024 * 1024

    @property
    def nano_cpus(self) -> int:
        return round(self.cpus * 1_000_000_000)

    def container_cmd(self) -> list[str]:
        """The container's command: the bootstrap and its deadline."""
        deadline = float(self.timeout_seconds) + float(self.container_grace_seconds)
        return ["-I", "-c", BOOTSTRAP, repr(deadline)]

    def tmpfs(self) -> dict[str, str]:
        uid, gid = CONTAINER_USER.split(":")
        return {
            WORKSPACE_DIR: (
                f"rw,exec,nosuid,nodev,size={self.workspace_tmpfs_mb}m,uid={uid},gid={gid},mode=0700"
            ),
            "/tmp": f"rw,noexec,nosuid,nodev,size={self.tmp_tmpfs_mb}m,mode=1777",  # nosec B108
        }


# ── Command construction and verification (pure) ───────────────────────────────


def build_create_args(config: DockerExecutorConfig, name: str) -> list[str]:
    """Arguments for ``docker create`` (after the binary).  No host path is mounted."""
    args = [
        "create",
        "--interactive",
        "--name", name,
        "--label", f"{MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}",
        "--pull", "never",
        "--user", CONTAINER_USER,
        "--network", "none",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--ipc", "private",
        "--cgroupns", "private",
        "--init",
        "--log-driver", "none",
        "--no-healthcheck",
        "--hostname", "codeforge-test",
        "--memory", f"{config.memory_mb}m",
        "--memory-swap", f"{config.memory_mb}m",
        "--cpus", repr(float(config.cpus)),
        "--pids-limit", str(config.pids_limit),
        "--workdir", WORKSPACE_DIR,
    ]  # fmt: skip
    for ulimit, soft, hard in ULIMITS:
        args += ["--ulimit", f"{ulimit}={soft}:{hard}"]
    for path, options in config.tmpfs().items():
        args += ["--tmpfs", f"{path}:{options}"]
    for key, value in CONTAINER_ENV.items():
        args += ["--env", f"{key}={value}"]
    args += ["--entrypoint", ENTRYPOINT, config.image, *config.container_cmd()]
    return args


def daemon_violations(info: Mapping[str, Any]) -> list[str]:
    """Why the daemon described by ``docker info`` cannot run the executor."""
    problems: list[str] = []
    if info.get("ServerErrors"):
        problems.append("the Docker daemon is unreachable")
        return problems
    seccomp = [o for o in info.get("SecurityOptions") or [] if str(o).startswith("name=seccomp")]
    if not seccomp or any("profile=unconfined" in str(o) for o in seccomp):
        problems.append("seccomp is not enabled")
    for key in ("MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit"):
        if info.get(key) is not True:
            problems.append(f"{key} is not supported")
    return problems


def container_violations(inspect: Mapping[str, Any], config: DockerExecutorConfig) -> list[str]:
    """Every way the created container differs from the required configuration."""
    host: Mapping[str, Any] = inspect.get("HostConfig") or {}
    cfg: Mapping[str, Any] = inspect.get("Config") or {}
    problems: list[str] = []

    def expect(label: str, actual: Any, wanted: Any) -> None:
        if actual != wanted:
            problems.append(f"{label} is {actual!r}, expected {wanted!r}")

    expect("Privileged", host.get("Privileged"), False)
    expect("ReadonlyRootfs", host.get("ReadonlyRootfs"), True)
    expect("NetworkMode", host.get("NetworkMode"), "none")
    expect("CapDrop", host.get("CapDrop"), ["ALL"])
    expect("SecurityOpt", host.get("SecurityOpt"), ["no-new-privileges"])
    expect("IpcMode", host.get("IpcMode"), "private")
    expect("CgroupnsMode", host.get("CgroupnsMode"), "private")
    for key in ("PidMode", "UTSMode", "UsernsMode"):
        # "" is Docker's private default; anything else ("host", "container:…") shares one.
        expect(key, host.get(key) or "", "")
    expect("Memory", host.get("Memory"), config.memory_bytes)
    expect("MemorySwap", host.get("MemorySwap"), config.memory_bytes)
    expect("NanoCpus", host.get("NanoCpus"), config.nano_cpus)
    expect("PidsLimit", host.get("PidsLimit"), config.pids_limit)
    expect("Tmpfs", host.get("Tmpfs"), config.tmpfs())
    expect("Init", host.get("Init"), True)
    expect("LogConfig.Type", (host.get("LogConfig") or {}).get("Type"), "none")
    expect("OomKillDisable", bool(host.get("OomKillDisable")), False)
    ulimits = {(u.get("Name"), u.get("Soft"), u.get("Hard")) for u in host.get("Ulimits") or []}
    expect("Ulimits", ulimits, set(ULIMITS))
    for key in ("CapAdd", "Binds", "Mounts", "VolumesFrom", "Devices", "DeviceRequests",
                "Links", "Sysctls"):  # fmt: skip
        if host.get(key):
            problems.append(f"{key} must be empty, got {host.get(key)!r}")
    # Empty masked/read-only path lists mean /proc and /sys are exposed unmasked.
    for key in ("MaskedPaths", "ReadonlyPaths"):
        if not host.get(key):
            problems.append(f"{key} must not be empty")
    if inspect.get("Mounts"):
        problems.append(f"container has mounts: {inspect.get('Mounts')!r}")
    if inspect.get("AppArmorProfile") == "unconfined":
        problems.append("AppArmor profile is unconfined")
    expect("User", cfg.get("User"), CONTAINER_USER)
    expect("Image", cfg.get("Image"), config.image)
    expect("Entrypoint", cfg.get("Entrypoint"), [ENTRYPOINT])
    expect("Cmd", cfg.get("Cmd"), config.container_cmd())
    expect("Tty", cfg.get("Tty"), False)
    if cfg.get("Volumes"):
        problems.append(f"image declares volumes: {cfg.get('Volumes')!r}")
    env = set(cfg.get("Env") or [])
    for key, value in CONTAINER_ENV.items():
        if f"{key}={value}" not in env:
            problems.append(f"environment is missing {key}={value}")
    return problems


# ── Process helpers ────────────────────────────────────────────────────────────

_T = TypeVar("_T")


async def _finish(aw: Awaitable[_T]) -> _T:
    """Await *aw* to completion even if the caller is cancelled (repeatedly).

    Used for short, bounded steps that must not be abandoned halfway: Docker
    management commands and container removal.  A cancellation is re-raised
    once the step is done.
    """
    fut = asyncio.ensure_future(aw)
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        while not fut.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait([fut])
        if not fut.cancelled():
            fut.exception()  # mark retrieved; the cancellation wins
        raise


def _kill(proc: asyncio.subprocess.Process) -> None:
    # The CLI runs in its own session, so its process group id is its pid.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


def _summary(text: str, limit: int = 500) -> str:
    text = " ".join(text.split())
    return text[:limit] or "no details"


@dataclass(frozen=True)
class _Completed:
    returncode: int
    stdout: str
    stderr: str


class _DockerCLI:
    """Runs the docker CLI with a minimal environment and an empty config directory."""

    def __init__(self, binary: str, config_dir: Path, timeout: float) -> None:
        self.binary = binary
        self.timeout = timeout
        env = {k: os.environ[k] for k in _CLI_ENV_ALLOWLIST if k in os.environ}
        env.setdefault("PATH", os.defpath)
        env["DOCKER_CONFIG"] = str(config_dir)
        self.env = env

    async def spawn(self, *args: str, stdin: int = asyncio.subprocess.DEVNULL) -> asyncio.subprocess.Process:
        try:
            return await asyncio.create_subprocess_exec(
                self.binary,
                *args,
                env=self.env,
                stdin=stdin,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise ExecutionUnavailableError(
                f"Cannot run the Docker CLI: {exc.strerror or exc}"
            ) from None

    async def run(self, *args: str) -> _Completed:
        """Run a management command to completion (bounded by the timeout, not cancellation)."""
        return await _finish(self._run(*args))

    async def _run(self, *args: str) -> _Completed:
        proc = await self.spawn(*args)
        stdout, stderr = proc.stdout, proc.stderr
        if stdout is None or stderr is None:  # pragma: no cover - PIPE requested
            _kill(proc)
            await proc.wait()
            raise ExecutionUnavailableError("Docker CLI output pipes are unavailable.")
        out = BoundedBuffer(_MANAGEMENT_OUTPUT_BYTES)
        err = BoundedBuffer(_MANAGEMENT_OUTPUT_BYTES)

        async def communicate() -> int:
            await asyncio.gather(out.drain(stdout), err.drain(stderr))
            return await proc.wait()

        try:
            returncode = await asyncio.wait_for(communicate(), timeout=self.timeout)
        except TimeoutError:
            _kill(proc)
            await proc.wait()
            raise ExecutionUnavailableError(
                f"docker {args[0]} did not finish within {self.timeout:g}s"
            ) from None
        return _Completed(returncode, out.text(), err.text())


async def _feed(stdin: asyncio.StreamWriter, archive_path: Path) -> None:
    try:
        with open(archive_path, "rb") as fh:
            while chunk := fh.read(_STREAM_CHUNK):
                stdin.write(chunk)
                await stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # the container stopped reading; its exit status says why
    finally:
        with contextlib.suppress(Exception):
            stdin.close()


async def _reap(proc: asyncio.subprocess.Process) -> None:
    try:
        await asyncio.wait_for(proc.wait(), timeout=_DRAIN_SECONDS)
    except TimeoutError:
        _kill(proc)
        await proc.wait()


async def _settle(feeder: asyncio.Task[None], readers: list[asyncio.Task[None]]) -> None:
    feeder.cancel()
    _, pending = await asyncio.wait(readers, timeout=_DRAIN_SECONDS)
    for reader in pending:
        reader.cancel()
    await asyncio.gather(feeder, *readers, return_exceptions=True)


# ── Executor ───────────────────────────────────────────────────────────────────


class DockerTestExecutor:
    """Runs pytest on a snapshot inside a hardened, throwaway Docker container."""

    def __init__(self, config: DockerExecutorConfig) -> None:
        self.config = config

    @property
    def isolation(self) -> IsolationLevel:
        return IsolationLevel.CONTAINER

    async def run(self, snapshot: WorkspaceSnapshot) -> TestResult:
        binary = shutil.which(self.config.cli)
        if binary is None:
            raise ExecutionUnavailableError(
                f"Docker CLI {self.config.cli!r} was not found; test execution is unavailable."
            )
        # The archive is handed to another extractor, so check it is still
        # the one that was built and that every member is safe.
        await run_blocking(verify_snapshot_archive, snapshot)

        with private_tempdir("codeforge-docker-") as config_dir:
            cli = _DockerCLI(binary, Path(config_dir), self.config.management_timeout_seconds)
            await self._preflight(cli)
            name = NAME_PREFIX + uuid.uuid4().hex
            try:
                return await self._run_container(cli, name, snapshot)
            finally:
                await _finish(self._remove(cli, name))

    async def _preflight(self, cli: _DockerCLI) -> None:
        res = await cli.run("info", "--format", "{{json .}}")
        try:
            info = json.loads(res.stdout) if res.stdout.strip() else {}
        except ValueError:
            info = {}
        if res.returncode != 0 or not isinstance(info, dict) or not info:
            raise ExecutionUnavailableError(
                f"Docker daemon is unavailable: {_summary(res.stderr)}"
            )
        problems = daemon_violations(info)
        if problems:
            raise ExecutionUnavailableError(
                "Docker cannot enforce the required test isolation: " + "; ".join(problems)
            )

    async def _inspect(self, cli: _DockerCLI, name: str) -> dict[str, Any]:
        res = await cli.run("inspect", "--type", "container", "--format", "{{json .}}", name)
        if res.returncode != 0:
            raise ExecutionUnavailableError(
                f"Cannot inspect the test container: {_summary(res.stderr)}"
            )
        try:
            data = json.loads(res.stdout)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ExecutionUnavailableError("Docker returned an unreadable container description")
        return data

    async def _remove(self, cli: _DockerCLI, name: str) -> bool:
        """Force-remove (and so stop) *name*.  True if it is gone."""
        try:
            res = await cli.run("rm", "--force", name)
        except ExecutionUnavailableError as exc:
            logger.error("DockerTestExecutor: could not remove container %s: %s", name, exc)
            return False
        if res.returncode == 0 or "no such container" in res.stderr.lower():
            return True
        logger.error(
            "DockerTestExecutor: could not remove container %s: %s", name, _summary(res.stderr)
        )
        return False

    async def _run_container(
        self, cli: _DockerCLI, name: str, snapshot: WorkspaceSnapshot
    ) -> TestResult:
        create = await cli.run(*build_create_args(self.config, name))
        if create.returncode != 0:
            raise ExecutionUnavailableError(
                f"Docker refused to create the test container: {_summary(create.stderr)}"
            )
        if create.stderr.strip():
            # Docker reports discarded limits as warnings; never run on a downgrade.
            raise ExecutionUnavailableError(
                f"Docker reported warnings creating the test container: {_summary(create.stderr)}"
            )
        problems = container_violations(await self._inspect(cli, name), self.config)
        if problems:
            raise ExecutionUnavailableError(
                "Docker did not apply the required test isolation: " + "; ".join(problems)
            )
        logger.info("DockerTestExecutor: starting %s (image %s)", name, self.config.image)
        return await self._start(cli, name, snapshot)

    async def _start(self, cli: _DockerCLI, name: str, snapshot: WorkspaceSnapshot) -> TestResult:
        config = self.config
        start = time.monotonic()
        proc = await cli.spawn(
            "start", "--attach", "--interactive", name, stdin=asyncio.subprocess.PIPE
        )
        if proc.stdin is None or proc.stdout is None or proc.stderr is None:  # pragma: no cover
            await _finish(self._remove(cli, name))
            await _finish(_reap(proc))
            raise ExecutionUnavailableError("Docker CLI pipes are unavailable.")
        out = BoundedBuffer(config.max_output_bytes)
        err = BoundedBuffer(config.max_output_bytes)
        readers = [
            asyncio.create_task(out.drain(proc.stdout)),
            asyncio.create_task(err.drain(proc.stderr)),
        ]
        feeder = asyncio.create_task(_feed(proc.stdin, snapshot.archive_path))

        timed_out = False
        stopped = True
        try:
            await asyncio.wait_for(proc.wait(), timeout=config.timeout_seconds)
        except TimeoutError:
            timed_out = True
        finally:
            if proc.returncode is None:
                # Timeout or cancellation (lease loss).  Removing the container
                # stops it; killing the CLI alone would leave it running.
                stopped = await _finish(self._remove(cli, name))
                await _finish(_reap(proc))
                if not timed_out:
                    logger.warning("DockerTestExecutor: cancelled; container %s removed", name)
            await _finish(_settle(feeder, readers))
        duration = round(time.monotonic() - start, 3)

        if timed_out:
            if not stopped:
                raise ExecutionUnavailableError(
                    f"Test container timed out and could not be removed ({name})"
                )
            logger.warning("DockerTestExecutor: %s timed out after %ss", name, config.timeout_seconds)
            message = f"Test execution timed out after {config.timeout_seconds:g}s"
            stderr = err.text()
            return TestResult(
                passed=False,
                exit_code=-1,
                stdout=out.text(),
                stderr=f"{stderr}\n{message}" if stderr else message,
                duration_seconds=duration,
            )

        state = (await self._inspect(cli, name)).get("State") or {}
        started = str(state.get("StartedAt") or "")
        if state.get("Status") != "exited" or not started or started.startswith("0001-"):
            raise ExecutionUnavailableError(
                f"The test container did not run: {_summary(err.text())}"
            )
        exit_code = int(state.get("ExitCode", -1))
        if exit_code == EXTRACTION_FAILED_EXIT:
            raise ExecutionUnavailableError(
                "The workspace snapshot could not be loaded in the test container"
            )
        stderr = err.text()
        if state.get("OOMKilled"):
            note = f"Test container was killed: memory limit of {config.memory_mb} MiB exceeded"
            stderr = f"{stderr}\n{note}" if stderr else note
        logger.info(
            "DockerTestExecutor: %s finished in %.2fs exit_code=%d", name, duration, exit_code
        )
        return TestResult(
            passed=exit_code == 0 and not state.get("OOMKilled"),
            exit_code=exit_code,
            stdout=out.text(),
            stderr=stderr,
            duration_seconds=duration,
        )
