"""Phase 10B.5 Stage 1C — hardened Docker test executor (no Docker required).

A fake ``docker`` CLI (fake_docker.py) records every invocation and simulates
the container lifecycle, so these tests pin down what the executor asks Docker
for, what it refuses to accept, and that it always cleans up its own container
and only its own container.  Real-Docker coverage lives in
test_docker_executor_integration.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import subprocess  # nosec B404 - runs the bootstrap locally with garbage input
import sys
import tarfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from app.config import ExecutionMode, Settings, settings
from app.execution import docker_executor
from app.execution.docker_executor import (
    BOOTSTRAP,
    CONTAINER_ENV,
    CONTAINER_USER,
    EXTRACTION_FAILED_EXIT,
    NAME_PREFIX,
    DockerExecutorConfig,
    DockerTestExecutor,
    build_create_args,
    container_violations,
    daemon_violations,
)
from app.execution.host_executor import HostTestExecutor
from app.execution.ownership import OwnershipLostError, run_until_ownership_lost
from app.execution.test_executor import (
    ExecutionUnavailableError,
    IsolationLevel,
    TestExecutor,
    UnavailableTestExecutor,
    get_test_executor,
    run_tests_in_snapshot,
)
from app.orchestration.orchestrator import Orchestrator
from app.workspace.snapshot import SnapshotError, workspace_snapshot

IMAGE = "sha256:" + "a" * 64
FAKE_DOCKER = Path(__file__).with_name("fake_docker.py")


class FakeDocker:
    """Handle on one fake docker CLI and its state directory."""

    def __init__(self, root: Path) -> None:
        self.state = root / "docker-state"
        (self.state / "containers").mkdir(parents=True)
        self.cli = root / "docker"
        self.cli.write_text(
            f'#!/bin/sh\nexec {sys.executable} -I {FAKE_DOCKER} {self.state} "$@"\n'
        )
        self.cli.chmod(0o755)

    def scenario(self, **values: Any) -> None:
        (self.state / "scenario.json").write_text(json.dumps(values))

    def calls(self) -> list[dict[str, Any]]:
        path = self.state / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def commands(self) -> list[str]:
        return [c["argv"][0] for c in self.calls()]

    def created(self) -> list[str]:
        names = []
        for call in self.calls():
            argv = call["argv"]
            if argv[0] == "create":
                names.append(argv[argv.index("--name") + 1])
        return names

    def removed_names(self) -> list[str]:
        return [c["argv"][-1] for c in self.calls() if c["argv"][0] == "rm"]

    def is_removed(self, name: str) -> bool:
        return (self.state / "removed" / name).exists()

    def started(self) -> list[str]:
        folder = self.state / "started"
        return sorted(p.name for p in folder.iterdir()) if folder.exists() else []

    def received(self, name: str) -> list[str]:
        root = self.state / "received" / name
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())

    async def wait_started(self, count: int = 1, timeout: float = 15.0) -> list[str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.started()) >= count:
                return self.started()
            await asyncio.sleep(0.02)
        raise AssertionError("container never started")


@pytest.fixture
def fake(tmp_path: Path) -> FakeDocker:
    return FakeDocker(tmp_path)


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


def _executor(fake: FakeDocker, **overrides: Any) -> DockerTestExecutor:
    values: dict[str, Any] = {"image": IMAGE, "cli": str(fake.cli)}
    values.update(overrides)
    return DockerTestExecutor(DockerExecutorConfig(**values))


def _workspace(root: Path, behaviour: str = "pass", name: str = "ws") -> Path:
    ws = root / name
    ws.mkdir()
    (ws / "behaviour").write_text(behaviour)
    (ws / "test_a.py").write_text("def test_ok():\n    assert True\n")
    return ws


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


# ── Configuration ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "image",
    [
        "",
        "python",
        "python:3.12-alpine",
        "codeforge-test-runner:latest",
        "sha256:abc",
        "sha256:" + "A" * 64,
        "python@sha256:" + "a" * 63,
        "-python@sha256:" + "a" * 64,
        "--privileged@sha256:" + "a" * 64,
        "py thon@sha256:" + "a" * 64,
        "python@sha256:" + "a" * 64 + " ",
        "Python@sha256:" + "a" * 64,
    ],
)
def test_config_requires_a_digest_pinned_image(image: str) -> None:
    with pytest.raises(ExecutionUnavailableError, match="pinned by digest"):
        DockerExecutorConfig(image=image)


@pytest.mark.parametrize(
    "image",
    [
        "sha256:" + "0" * 64,
        "python@sha256:" + "b" * 64,
        "python:3.12-alpine@sha256:" + "c" * 64,
        "registry.example.com:5000/team/runner@sha256:" + "d" * 64,
    ],
)
def test_config_accepts_pinned_references(image: str) -> None:
    assert DockerExecutorConfig(image=image).image == image


@pytest.mark.parametrize(
    "overrides",
    [
        {"memory_mb": 512},  # smaller than the tmpfs mounts it must hold
        {"cpus": 0},
        {"cpus": -1},
        {"pids_limit": 1},
        {"timeout_seconds": 0},
        {"cli": ""},
        {"cli": "--host=tcp://evil"},
        {"cli": "docker --debug"},
        {"max_output_bytes": 0},
        {"container_grace_seconds": 0},
    ],
)
def test_config_rejects_unsafe_values(overrides: dict[str, Any]) -> None:
    with pytest.raises(ExecutionUnavailableError, match="misconfigured"):
        DockerExecutorConfig(image=IMAGE, **overrides)


def test_settings_reject_docker_mode_without_a_pinned_image() -> None:
    with pytest.raises(ValueError, match="CODEFORGE_DOCKER_TEST_IMAGE"):
        _settings(codeforge_test_execution_mode="docker")
    with pytest.raises(ValueError, match="pinned by digest"):
        _settings(
            codeforge_test_execution_mode="docker",
            codeforge_docker_test_image="codeforge-test-runner:latest",
        )


def test_settings_accept_docker_mode_with_a_pinned_image_in_production() -> None:
    loaded = _settings(
        app_env="production",
        openai_api_key="sk-test",
        database_url="postgresql+asyncpg://u:p@db:5432/prod",
        jwt_secret_key="a-production-grade-secret-key-of-sufficient-length",
        codeforge_test_execution_mode="docker",
        codeforge_docker_test_image=IMAGE,
    )
    assert loaded.codeforge_test_execution_mode is ExecutionMode.DOCKER


def test_settings_keep_repairs_disabled_in_docker_mode() -> None:
    with pytest.raises(ValueError, match="sandbox"):
        _settings(
            codeforge_test_execution_mode="docker",
            codeforge_docker_test_image=IMAGE,
            codeforge_max_repair_attempts=1,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"codeforge_docker_memory_mb": 512},
        {"codeforge_docker_cpus": 0},
        {"codeforge_docker_pids_limit": 0},
        {"codeforge_docker_pids_limit": 100_000},
        {"codeforge_docker_timeout_seconds": 0},
    ],
)
def test_settings_bound_docker_limits(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _settings(**overrides)


def test_config_from_settings_copies_limits() -> None:
    loaded = _settings(
        codeforge_test_execution_mode="docker",
        codeforge_docker_test_image=IMAGE,
        codeforge_docker_cli="/usr/local/bin/docker",
        codeforge_docker_timeout_seconds=42,
        codeforge_docker_cpus=1.5,
        codeforge_docker_memory_mb=3072,
        codeforge_docker_pids_limit=128,
    )
    config = DockerExecutorConfig.from_settings(loaded)
    assert (config.image, config.cli, config.timeout_seconds) == (IMAGE, "/usr/local/bin/docker", 42)
    assert (config.cpus, config.memory_mb, config.pids_limit) == (1.5, 3072, 128)


# ── Factory and repair gate ────────────────────────────────────────────────────


def test_docker_mode_returns_the_docker_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "codeforge_test_execution_mode", ExecutionMode.DOCKER)
    monkeypatch.setattr(settings, "codeforge_docker_test_image", IMAGE)
    executor = get_test_executor()
    assert isinstance(executor, DockerTestExecutor)
    assert isinstance(executor, TestExecutor)
    assert executor.isolation is IsolationLevel.CONTAINER
    assert executor.config.image == IMAGE


def test_factory_fails_closed_on_a_mutated_unpinned_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "codeforge_test_execution_mode", ExecutionMode.DOCKER)
    monkeypatch.setattr(settings, "codeforge_docker_test_image", "python:latest")
    executor = get_test_executor()
    assert isinstance(executor, UnavailableTestExecutor)
    assert "pinned by digest" in executor.reason


def test_container_isolation_does_not_permit_repairs(fake: FakeDocker) -> None:
    assert IsolationLevel.CONTAINER is not IsolationLevel.SANDBOX
    with pytest.raises(ValueError, match="SANDBOX"):
        Orchestrator(executor=_executor(fake), max_repair_attempts=1)


# ── Command construction ───────────────────────────────────────────────────────


def _options(args: list[str]) -> list[tuple[str, str]]:
    image_at = args.index(IMAGE)
    pairs = []
    i = 1
    while i < image_at:
        if i + 1 < image_at and not args[i + 1].startswith("--"):
            pairs.append((args[i], args[i + 1]))
            i += 2
        else:
            pairs.append((args[i], ""))
            i += 1
    return pairs


def test_create_args_request_every_required_control() -> None:
    config = DockerExecutorConfig(
        image=IMAGE, cpus=1.5, memory_mb=3072, pids_limit=128, workspace_tmpfs_mb=600
    )
    args = build_create_args(config, "codeforge-test-x")
    assert args[0] == "create"
    pairs = set(_options(args))
    for required in [
        ("--interactive", ""),
        ("--name", "codeforge-test-x"),
        ("--label", "codeforge.managed=test-executor"),
        ("--pull", "never"),
        ("--user", "65534:65534"),
        ("--network", "none"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--read-only", ""),
        ("--ipc", "private"),
        ("--cgroupns", "private"),
        ("--init", ""),
        ("--log-driver", "none"),
        ("--memory", "3072m"),
        ("--memory-swap", "3072m"),
        ("--cpus", "1.5"),
        ("--pids-limit", "128"),
        ("--ulimit", "core=0:0"),
        ("--ulimit", "nofile=1024:1024"),
        ("--tmpfs", "/workspace:rw,exec,nosuid,nodev,size=600m,uid=65534,gid=65534,mode=0700"),
        ("--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777"),
        ("--workdir", "/workspace"),
        ("--entrypoint", "python"),
    ]:
        assert required in pairs, required
    env = sorted(v for k, v in pairs if k == "--env")
    assert env == sorted(f"{k}={v}" for k, v in CONTAINER_ENV.items())


def test_create_args_never_mount_share_or_escalate() -> None:
    args = build_create_args(DockerExecutorConfig(image=IMAGE), "codeforge-test-x")
    forbidden_flags = {
        "-v", "--volume", "--mount", "--volumes-from", "--privileged", "--cap-add",
        "--device", "--pid", "--userns", "--uts", "--group-add",
        "--sysctl", "--env-file", "-e", "--add-host", "-p", "--publish", "--publish-all",
        "--runtime", "--oom-kill-disable", "--security-opt=seccomp=unconfined",
    }  # fmt: skip
    options = args[: args.index(IMAGE)]
    assert not forbidden_flags & set(options)
    joined = " ".join(options)
    for needle in ("docker.sock", "unconfined", "systempaths", "/var/run", "/home"):
        assert needle not in joined, needle
    # No namespace is shared with the host or another container.
    for _flag, value in _options(args):
        assert value != "host" and not value.startswith("container:"), value
    # Never inherit a worker variable by name (``--env NAME`` without a value).
    assert all("=" in value for flag, value in _options(args) if flag == "--env")


def test_image_and_fixed_command_end_the_arguments() -> None:
    config = DockerExecutorConfig(image=IMAGE, timeout_seconds=90, container_grace_seconds=15)
    args = build_create_args(config, "codeforge-test-x")
    at = args.index(IMAGE)
    assert args[at - 2 : at] == ["--entrypoint", "python"]
    # The bootstrap, then the container's own deadline: the timeout plus grace.
    assert args[at + 1 :] == ["-I", "-c", BOOTSTRAP, "105.0"]


def test_bootstrap_is_valid_python_and_runs_only_pytest() -> None:
    compile(BOOTSTRAP, "<bootstrap>", "exec")
    assert 'filter="data"' in BOOTSTRAP
    assert "'-m', 'pytest'" in BOOTSTRAP


def test_bootstrap_reports_unreadable_snapshots_with_its_own_exit_status() -> None:
    proc = subprocess.run(  # nosec B603 - fixed arguments, local interpreter
        [sys.executable, "-I", "-c", BOOTSTRAP, "30.0"],
        input=b"this is not a tar archive" * 100,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == EXTRACTION_FAILED_EXIT
    assert b"snapshot extraction failed" in proc.stderr


# ── Post-create verification ───────────────────────────────────────────────────


def _good_inspect(config: DockerExecutorConfig) -> dict[str, Any]:
    """Shaped like real ``docker inspect`` output for a correctly created container."""
    return {
        "AppArmorProfile": "docker-default",
        "Mounts": [],
        "HostConfig": {
            "Binds": None,
            "Mounts": None,
            "Tmpfs": config.tmpfs(),
            "Privileged": False,
            "ReadonlyRootfs": True,
            "NetworkMode": "none",
            "IpcMode": "private",
            "PidMode": "",
            "UTSMode": "",
            "UsernsMode": "",
            "CgroupnsMode": "private",
            "CapAdd": None,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Memory": config.memory_bytes,
            "MemorySwap": config.memory_bytes,
            "NanoCpus": config.nano_cpus,
            "PidsLimit": config.pids_limit,
            "Ulimits": [
                {"Name": "core", "Hard": 0, "Soft": 0},
                {"Name": "nofile", "Hard": 1024, "Soft": 1024},
            ],
            "Devices": [],
            "DeviceRequests": None,
            "VolumesFrom": None,
            "Init": True,
            "LogConfig": {"Type": "none", "Config": {}},
            "OomKillDisable": False,
            "Sysctls": None,
            "Links": None,
            "MaskedPaths": ["/proc/kcore", "/sys/firmware"],
            "ReadonlyPaths": ["/proc/sys"],
        },
        "Config": {
            "User": CONTAINER_USER,
            "Image": config.image,
            "Entrypoint": ["python"],
            "Cmd": config.container_cmd(),
            "Tty": False,
            "Volumes": None,
            "Env": [f"{k}={v}" for k, v in CONTAINER_ENV.items()] + ["PATH=/usr/bin"],
        },
    }


def test_correct_container_passes_verification() -> None:
    config = DockerExecutorConfig(image=IMAGE, cpus=1.5)
    assert container_violations(_good_inspect(config), config) == []


@pytest.mark.parametrize(
    "path,value",
    [
        (("HostConfig", "Privileged"), True),
        (("HostConfig", "ReadonlyRootfs"), False),
        (("HostConfig", "NetworkMode"), "bridge"),
        (("HostConfig", "NetworkMode"), "host"),
        (("HostConfig", "IpcMode"), "host"),
        (("HostConfig", "CgroupnsMode"), "host"),
        (("HostConfig", "PidMode"), "host"),
        (("HostConfig", "UTSMode"), "host"),
        (("HostConfig", "UsernsMode"), "host"),
        (("HostConfig", "CapDrop"), None),
        (("HostConfig", "CapAdd"), ["SYS_ADMIN"]),
        (("HostConfig", "SecurityOpt"), None),
        (("HostConfig", "SecurityOpt"), ["no-new-privileges", "seccomp=unconfined"]),
        (("HostConfig", "Memory"), 0),
        (("HostConfig", "MemorySwap"), -1),
        (("HostConfig", "NanoCpus"), 0),
        (("HostConfig", "PidsLimit"), None),
        (("HostConfig", "Tmpfs"), {"/tmp": "rw"}),
        (("HostConfig", "Binds"), ["/var/run/docker.sock:/var/run/docker.sock"]),
        (("HostConfig", "Mounts"), [{"Type": "bind", "Source": "/", "Target": "/host"}]),
        (("HostConfig", "VolumesFrom"), ["other"]),
        (("HostConfig", "Devices"), [{"PathOnHost": "/dev/kvm"}]),
        (("HostConfig", "Sysctls"), {"net.ipv4.ip_forward": "1"}),
        (("HostConfig", "Init"), False),
        (("HostConfig", "LogConfig"), {"Type": "json-file"}),
        (("HostConfig", "OomKillDisable"), True),
        (("HostConfig", "Ulimits"), []),
        (("HostConfig", "MaskedPaths"), []),
        (("HostConfig", "ReadonlyPaths"), None),
        (("Mounts",), [{"Type": "volume", "Destination": "/data"}]),
        (("AppArmorProfile",), "unconfined"),
        (("Config", "User"), "0:0"),
        (("Config", "User"), ""),
        (("Config", "Image"), "sha256:" + "e" * 64),
        (("Config", "Entrypoint"), ["sh"]),
        (("Config", "Cmd"), ["-c", "import os"]),
        (("Config", "Cmd"), ["-I", "-c", BOOTSTRAP, "999999.0"]),
        (("Config", "Tty"), True),
        (("Config", "Volumes"), {"/data": {}}),
        (("Config", "Env"), ["PATH=/usr/bin"]),
    ],
)
def test_any_downgraded_or_added_setting_is_rejected(path: tuple[str, ...], value: Any) -> None:
    config = DockerExecutorConfig(image=IMAGE)
    data = _good_inspect(config)
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert container_violations(data, config) != []


def test_daemon_must_support_seccomp_and_limits() -> None:
    good = {
        "SecurityOptions": ["name=apparmor", "name=seccomp,profile=builtin"],
        "MemoryLimit": True,
        "SwapLimit": True,
        "CpuCfsQuota": True,
        "PidsLimit": True,
    }
    assert daemon_violations(good) == []
    assert daemon_violations({**good, "SecurityOptions": ["name=apparmor"]})
    assert daemon_violations({**good, "SecurityOptions": ["name=seccomp,profile=unconfined"]})
    for key in ("MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit"):
        assert daemon_violations({**good, key: False}), key
        assert daemon_violations({k: v for k, v in good.items() if k != key}), key
    assert daemon_violations({**good, "ServerErrors": ["down"]})


# ── Lifecycle (fake docker CLI) ────────────────────────────────────────────────


async def _cancel_after_start(fake: FakeDocker, task: asyncio.Task[Any], *, times: int) -> None:
    await fake.wait_started()
    for _ in range(times):
        task.cancel()
        await asyncio.sleep(0.01)
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_passing_run_consumes_only_the_snapshot_and_removes_the_container(
    fake: FakeDocker, tmp_path: Path
) -> None:
    ws = _workspace(tmp_path)
    (ws / ".env").write_text("OPENAI_API_KEY=sk-live\n")
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("[core]\n")

    result = await run_tests_in_snapshot(_executor(fake), ws)

    assert result.passed and result.exit_code == 0
    assert "1 passed" in result.stdout
    assert fake.commands() == ["info", "create", "inspect", "start", "inspect", "rm"]
    [name] = fake.created()
    assert name.startswith(NAME_PREFIX) and len(name) == len(NAME_PREFIX) + 32
    assert fake.removed_names() == [name] and fake.is_removed(name)
    # The container received the policy-filtered snapshot, not the workspace.
    assert fake.received(name) == ["behaviour", "test_a.py"]


@pytest.mark.asyncio
async def test_docker_cli_gets_a_minimal_environment_and_private_config(
    fake: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-worker-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_worker_secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:secret@db/x")
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "operator-docker-config"))
    monkeypatch.setenv("DOCKER_HOST", "unix:///run/user/1000/docker.sock")

    await run_tests_in_snapshot(_executor(fake), _workspace(tmp_path))

    configs = set()
    for call in fake.calls():
        env = call["env"]
        assert "sk-worker-secret" not in json.dumps(env)
        assert "ghs_worker_secret" not in json.dumps(env)
        assert not {"OPENAI_API_KEY", "GITHUB_TOKEN", "DATABASE_URL"} & set(env)
        assert env["DOCKER_HOST"] == "unix:///run/user/1000/docker.sock"
        configs.add(env["DOCKER_CONFIG"])
    [config_dir] = configs
    assert config_dir != str(tmp_path / "operator-docker-config")
    assert not Path(config_dir).exists()  # private and deleted after the run


@pytest.mark.asyncio
async def test_failing_tests_are_reported_and_cleaned_up(fake: FakeDocker, tmp_path: Path) -> None:
    result = await run_tests_in_snapshot(_executor(fake), _workspace(tmp_path, "fail"))
    assert not result.passed and result.exit_code == 1
    assert "boom" in result.stderr
    [name] = fake.created()
    assert fake.is_removed(name)


@pytest.mark.asyncio
async def test_timeout_removes_the_container(fake: FakeDocker, tmp_path: Path) -> None:
    start = time.monotonic()
    result = await run_tests_in_snapshot(
        _executor(fake, timeout_seconds=1), _workspace(tmp_path, "hang")
    )
    assert time.monotonic() - start < 10
    assert not result.passed and result.exit_code == -1
    assert "timed out after 1s" in result.stderr
    [name] = fake.created()
    assert fake.is_removed(name)


@pytest.mark.asyncio
async def test_timeout_without_successful_removal_is_an_error(
    fake: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(docker_executor, "_DRAIN_SECONDS", 0.5)
    fake.scenario(rm="fail")
    with pytest.raises(ExecutionUnavailableError, match="could not be removed"):
        await run_tests_in_snapshot(
            _executor(fake, timeout_seconds=1), _workspace(tmp_path, "hang")
        )


@pytest.mark.asyncio
async def test_cancellation_removes_the_container(fake: FakeDocker, tmp_path: Path) -> None:
    async with _jobs() as jobs:
        task = asyncio.create_task(
            run_tests_in_snapshot(_executor(fake), _workspace(tmp_path, "hang"))
        )
        jobs.append(task)
        await _cancel_after_start(fake, task, times=1)
    [name] = fake.created()
    assert fake.is_removed(name)
    assert fake.removed_names()[0] == name


@pytest.mark.asyncio
async def test_repeated_cancellation_still_removes_the_container(
    fake: FakeDocker, tmp_path: Path
) -> None:
    async with _jobs() as jobs:
        task = asyncio.create_task(
            run_tests_in_snapshot(_executor(fake), _workspace(tmp_path, "hang"))
        )
        jobs.append(task)
        await _cancel_after_start(fake, task, times=5)
    [name] = fake.created()
    assert fake.is_removed(name)


@pytest.mark.asyncio
async def test_cancellation_during_create_removes_the_container_afterwards(
    fake: FakeDocker, tmp_path: Path
) -> None:
    fake.scenario(create_delay=0.5)
    async with _jobs() as jobs:
        task = asyncio.create_task(
            run_tests_in_snapshot(_executor(fake), _workspace(tmp_path, "pass"))
        )
        jobs.append(task)
        deadline = time.monotonic() + 15
        while "create" not in fake.commands():
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    [name] = fake.created()
    assert fake.started() == []
    assert fake.is_removed(name)
    # Removal ran after the in-flight create finished, not before it.
    assert fake.commands()[-1] == "rm"
    assert not (fake.state / "containers" / f"{name}.json").exists()


@pytest.mark.asyncio
async def test_lease_loss_stops_only_the_affected_jobs_container(
    fake: FakeDocker, tmp_path: Path
) -> None:
    lost_a, lost_b = asyncio.Event(), asyncio.Event()
    async with _jobs() as jobs:
        job_a = asyncio.create_task(
            run_until_ownership_lost(
                run_tests_in_snapshot(_executor(fake), _workspace(tmp_path, "hang", "a")), lost_a
            )
        )
        jobs.append(job_a)
        await fake.wait_started(1)
        [name_a] = fake.created()
        job_b = asyncio.create_task(
            run_until_ownership_lost(
                run_tests_in_snapshot(_executor(fake), _workspace(tmp_path, "until_release", "b")),
                lost_b,
            )
        )
        jobs.append(job_b)
        await fake.wait_started(2)
        [name_b] = [n for n in fake.created() if n != name_a]

        lost_a.set()  # job A's heartbeat lost the lease
        with pytest.raises(OwnershipLostError):
            await job_a
        assert fake.is_removed(name_a)
        assert not fake.is_removed(name_b)
        assert name_b not in fake.removed_names()
        assert not job_b.done()

        (fake.state / "release").touch()
        result_b = await job_b
        assert result_b.passed
        assert fake.is_removed(name_b)
        assert fake.removed_names().count(name_a) >= 1


@pytest.mark.asyncio
async def test_output_is_bounded(fake: FakeDocker, tmp_path: Path) -> None:
    result = await run_tests_in_snapshot(
        _executor(fake, max_output_bytes=1000), _workspace(tmp_path, "noisy:500000")
    )
    assert result.passed
    assert result.stdout.startswith("x" * 1000)
    assert len(result.stdout) < 1100 and "truncated at 1000 bytes" in result.stdout
    assert len(result.stderr) < 1100 and "truncated at 1000 bytes" in result.stderr


@pytest.mark.asyncio
async def test_out_of_memory_is_a_failure(fake: FakeDocker, tmp_path: Path) -> None:
    result = await run_tests_in_snapshot(_executor(fake), _workspace(tmp_path, "oom"))
    assert not result.passed
    assert "memory limit of 2048 MiB exceeded" in result.stderr


# ── Fail closed ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario,match",
    [
        ({"info": "unreachable"}, "daemon is unavailable"),
        ({"info_override": {"SecurityOptions": ["name=apparmor"]}}, "seccomp"),
        ({"info_override": {"PidsLimit": False}}, "PidsLimit"),
    ],
)
async def test_unusable_daemon_fails_before_creating_anything(
    fake: FakeDocker, tmp_path: Path, scenario: dict[str, Any], match: str
) -> None:
    fake.scenario(**scenario)
    with pytest.raises(ExecutionUnavailableError, match=match):
        await run_tests_in_snapshot(_executor(fake), _workspace(tmp_path))
    assert fake.commands() == ["info"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario,match",
    [
        ({"create": "fail"}, "refused to create"),
        ({"create": "warn"}, "reported warnings"),
        ({"tamper": {"HostConfig": {"PidsLimit": None}}}, "PidsLimit"),
        ({"tamper": {"HostConfig": {"Privileged": True}}}, "Privileged"),
        ({"tamper": {"HostConfig": {"Binds": ["/:/host"]}}}, "Binds"),
        ({"tamper": {"HostConfig": {"NetworkMode": "bridge"}}}, "NetworkMode"),
        ({"tamper": {"Config": {"User": "root"}}}, "User"),
    ],
)
async def test_refused_or_downgraded_security_never_starts_the_container(
    fake: FakeDocker, tmp_path: Path, scenario: dict[str, Any], match: str
) -> None:
    fake.scenario(**scenario)
    with pytest.raises(ExecutionUnavailableError, match=match):
        await run_tests_in_snapshot(_executor(fake), _workspace(tmp_path))
    assert "start" not in fake.commands()
    assert fake.started() == []
    [name] = fake.created()
    assert fake.removed_names() == [name]


@pytest.mark.asyncio
async def test_container_that_never_ran_is_an_error(fake: FakeDocker, tmp_path: Path) -> None:
    fake.scenario(start="never_started")
    with pytest.raises(ExecutionUnavailableError, match="did not run"):
        await run_tests_in_snapshot(_executor(fake), _workspace(tmp_path))
    [name] = fake.created()
    assert fake.is_removed(name)


@pytest.mark.asyncio
async def test_unloadable_snapshot_is_an_error_not_a_test_failure(
    fake: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A container that cannot extract its input exits with the bootstrap's status.
    garbage = tmp_path / "garbage.tar"
    garbage.write_bytes(b"not a tar archive" * 100)
    original = docker_executor._feed

    async def feed_garbage(stdin: asyncio.StreamWriter, archive_path: Path) -> None:
        await original(stdin, garbage)

    monkeypatch.setattr(docker_executor, "_feed", feed_garbage)
    with pytest.raises(ExecutionUnavailableError, match="could not be loaded"):
        await run_tests_in_snapshot(_executor(fake), _workspace(tmp_path))
    [name] = fake.created()
    assert fake.is_removed(name)


@pytest.mark.asyncio
async def test_tampered_archive_is_rejected_before_docker_is_called(
    fake: FakeDocker, tmp_path: Path
) -> None:
    ws = _workspace(tmp_path)
    async with workspace_snapshot(ws) as snapshot:
        data = snapshot.archive_path.read_bytes()
        snapshot.archive_path.write_bytes(data.replace(b"assert True", b"assert 1==1"))
        with pytest.raises(SnapshotError, match="changed"):
            await _executor(fake).run(snapshot)
    assert fake.calls() == []


@pytest.mark.asyncio
async def test_unsafe_archive_member_is_rejected_before_docker_is_called(
    fake: FakeDocker, tmp_path: Path
) -> None:
    import hashlib

    from app.workspace.snapshot import SnapshotManifest, WorkspaceSnapshot

    archive = tmp_path / "evil.tar"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("../escape.py")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    archive.write_bytes(buf.getvalue())
    snapshot = WorkspaceSnapshot(
        archive, SnapshotManifest((), (), 1), hashlib.sha256(buf.getvalue()).hexdigest()
    )
    with pytest.raises(SnapshotError, match="Unsafe"):
        await _executor(fake).run(snapshot)
    assert fake.calls() == []


@pytest.mark.asyncio
async def test_missing_docker_cli_never_runs_tests_on_the_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def host_must_not_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("host execution attempted")

    monkeypatch.setattr(HostTestExecutor, "run", host_must_not_run)
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "codeforge_test_execution_mode", ExecutionMode.DOCKER)
    monkeypatch.setattr(settings, "codeforge_docker_test_image", IMAGE)
    monkeypatch.setattr(settings, "codeforge_docker_cli", str(tmp_path / "no-such-docker"))
    marker = tmp_path / "ran-on-host"
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "conftest.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    (ws / "test_a.py").write_text("def test_ok(): assert True\n")

    executor = get_test_executor()
    assert isinstance(executor, DockerTestExecutor)
    with pytest.raises(ExecutionUnavailableError, match="not found"):
        await run_tests_in_snapshot(executor, ws)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_unreachable_daemon_never_runs_tests_on_the_host(
    fake: FakeDocker, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def host_must_not_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("host execution attempted")

    monkeypatch.setattr(HostTestExecutor, "run", host_must_not_run)
    monkeypatch.setattr(settings, "codeforge_test_execution_mode", ExecutionMode.DOCKER)
    monkeypatch.setattr(settings, "codeforge_docker_test_image", IMAGE)
    monkeypatch.setattr(settings, "codeforge_docker_cli", str(fake.cli))
    fake.scenario(info="unreachable")
    marker = tmp_path / "ran-on-host"
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "conftest.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")

    with pytest.raises(ExecutionUnavailableError, match="unavailable"):
        await run_tests_in_snapshot(get_test_executor(), ws)
    assert not marker.exists()
    assert fake.commands() == ["info"]


def test_docker_executor_has_no_path_to_host_execution() -> None:
    source = Path(docker_executor.__file__).read_text(encoding="utf-8")
    assert "host_executor" not in source
    assert "HostTestExecutor" not in source
