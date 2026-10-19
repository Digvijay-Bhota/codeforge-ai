"""Phase 10B.5 Stage 1D — gVisor test executor (no Docker or gVisor required).

Uses the fake ``docker`` CLI from the Stage 1C tests, which now also reports a
registered gVisor runtime and records ``--runtime``.  These tests pin down
configuration, the factory, command construction, every refusal and the
lifecycle; they are not evidence that gVisor works.  The real-runsc suite is
sandbox/gvisor/spike/test_gvisor_executor_integration.py (gvisor-spike.yml).

Refusal tests use a workspace with a sentinel file: the fake container records
everything it extracts, so ``received == []`` shows repository files never
reached a container.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from app.config import ExecutionMode, Settings, settings
from app.execution import docker_executor
from app.execution.docker_executor import (
    BOOTSTRAP,
    DockerExecutorConfig,
    DockerTestExecutor,
    build_create_args,
)
from app.execution.gvisor_executor import GVisorExecutorConfig, GVisorTestExecutor
from app.execution.gvisor_runtime import (
    MARKER_PATH,
    MIN_HOST_PIDS,
    NPROC_MAX,
    NPROC_MIN,
    GVisorRuntimeRequirement,
    min_host_pids,
    task_limit_violations,
)
from app.execution.host_executor import HostTestExecutor
from app.execution.ownership import OwnershipLostError, run_until_ownership_lost
from app.execution.test_executor import (
    ExecutionUnavailableError,
    IsolationLevel,
    UnavailableTestExecutor,
    get_test_executor,
    run_tests_in_snapshot,
)
from app.orchestration.models import WorkflowStatus
from app.orchestration.orchestrator import Orchestrator
from tests.security.test_docker_executor import FakeDocker, _cancel_after_start, _jobs

IMAGE = "sha256:" + "a" * 64
RUNTIME = "codeforge-gvisor"
RUNSC = "/usr/local/bin/runsc"
REQ = GVisorRuntimeRequirement(name=RUNTIME, path=RUNSC)
REVIEWED_ARGS = ["--network=none", "--oci-seccomp", "--directfs=false", "--gvisor-marker-file"]
FEATURES_KEY = "org.opencontainers.runtime-spec.features"
RUNC_FEATURES = json.dumps({"annotations": {"org.opencontainers.runc.version": "1.3.4"}})
SENTINEL = "repository_sentinel.txt"

# docker mode's ``create`` arguments for a default config, generated from the
# unchanged Stage 1C code (a6ae9ed), minus the trailing bootstrap command.
STAGE_1C_DOCKER_ARGS = [
    "create", "--interactive", "--name", "codeforge-test-x", "--label",
    "codeforge.managed=test-executor", "--pull", "never", "--user", "65534:65534", "--network",
    "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", "--ipc",
    "private", "--cgroupns", "private", "--init", "--log-driver", "none", "--no-healthcheck",
    "--hostname", "codeforge-test", "--memory", "2048m", "--memory-swap", "2048m", "--cpus", "1.0",
    "--pids-limit", "256", "--workdir", "/workspace", "--ulimit", "core=0:0", "--ulimit",
    "nofile=1024:1024", "--tmpfs",
    "/workspace:rw,exec,nosuid,nodev,size=512m,uid=65534,gid=65534,mode=0700", "--tmpfs",
    "/tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777", "--env", "HOME=/tmp", "--env",
    "TMPDIR=/tmp", "--env", "PYTHONPATH=/workspace", "--env", "PYTHONDONTWRITEBYTECODE=1",
    "--entrypoint", "python", IMAGE,
]  # fmt: skip


@pytest.fixture
def fake(tmp_path: Path) -> FakeDocker:
    return FakeDocker(tmp_path)


def _config(**overrides: Any) -> GVisorExecutorConfig:
    values: dict[str, Any] = {"image": IMAGE, "runtime": REQ, "nproc_limit": 64}
    values.update(overrides)
    return GVisorExecutorConfig(**values)


def _executor(fake: FakeDocker, **overrides: Any) -> GVisorTestExecutor:
    return GVisorTestExecutor(_config(cli=str(fake.cli), **overrides))


def _workspace(root: Path, behaviour: str = "pass", name: str = "ws") -> Path:
    ws = root / name
    ws.mkdir()
    (ws / "behaviour").write_text(behaviour)
    (ws / "test_a.py").write_text("def test_ok():\n    assert True\n")
    (ws / SENTINEL).write_text("repository content")
    return ws


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def _gvisor_settings(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "codeforge_test_execution_mode": "gvisor",
        "codeforge_docker_test_image": IMAGE,
        "codeforge_gvisor_runtime": RUNTIME,
        "codeforge_gvisor_runtime_path": RUNSC,
    }
    values.update(overrides)
    return values


def _runtime_entry(**overrides: Any) -> dict[str, Any]:
    return {RUNTIME: overrides}


def _create_argv(fake: FakeDocker) -> list[str]:
    [argv] = [c["argv"] for c in fake.calls() if c["argv"][0] == "create"]
    return list(argv)


# ── Settings ───────────────────────────────────────────────────────────────────


def test_settings_accept_a_complete_gvisor_configuration_in_production() -> None:
    loaded = _settings(
        app_env="production",
        openai_api_key="sk-test",
        database_url="postgresql+asyncpg://u:p@db:5432/prod",
        jwt_secret_key="a-production-grade-secret-key-of-sufficient-length",
        **_gvisor_settings(),
    )
    assert loaded.codeforge_test_execution_mode is ExecutionMode.GVISOR
    assert (loaded.codeforge_gvisor_nproc_limit, loaded.codeforge_docker_pids_limit) == (64, 256)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"codeforge_gvisor_runtime": ""}, "CODEFORGE_GVISOR_RUNTIME"),
        ({"codeforge_gvisor_runtime_path": ""}, "CODEFORGE_GVISOR_RUNTIME_PATH"),
        ({"codeforge_gvisor_runtime": "runc"}, "not a dedicated gVisor runtime name"),
        ({"codeforge_gvisor_runtime": "crun"}, "not a dedicated gVisor runtime name"),
        ({"codeforge_gvisor_runtime": "youki"}, "not a dedicated gVisor runtime name"),
        ({"codeforge_gvisor_runtime": "io.containerd.runc.v2"}, "not a dedicated gVisor runtime name"),
        ({"codeforge_gvisor_runtime": "--runtime=runc"}, "not a dedicated gVisor runtime name"),
        ({"codeforge_gvisor_runtime_path": "runsc"}, "absolute, normalized"),
        ({"codeforge_gvisor_runtime_path": "/usr/local/../bin/runsc"}, "absolute, normalized"),
        ({"codeforge_docker_test_image": ""}, "pinned by digest"),
        ({"codeforge_docker_test_image": "codeforge-test-runner:latest"}, "pinned by digest"),
        ({"codeforge_docker_pids_limit": 255}, "too little headroom"),
        ({"codeforge_docker_pids_limit": 128}, "too little headroom"),
    ],
)
def test_settings_reject_incomplete_or_unsafe_gvisor_configuration(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _settings(**_gvisor_settings(**overrides))


@pytest.mark.parametrize("nproc", [0, NPROC_MIN - 1, NPROC_MAX + 1, 4096])
def test_settings_bound_the_nproc_limit(nproc: int) -> None:
    with pytest.raises(ValueError):
        _settings(**_gvisor_settings(codeforge_gvisor_nproc_limit=nproc))


def test_settings_keep_repairs_disabled_in_gvisor_mode() -> None:
    with pytest.raises(ValueError, match="sandbox"):
        _settings(**_gvisor_settings(codeforge_max_repair_attempts=1))


def test_gvisor_settings_do_not_affect_other_modes() -> None:
    # Not validated (and not used) unless the mode is gvisor.
    loaded = _settings(codeforge_test_execution_mode="docker", codeforge_docker_test_image=IMAGE,
                       codeforge_gvisor_runtime="runc", codeforge_docker_pids_limit=64)  # fmt: skip
    assert loaded.codeforge_test_execution_mode is ExecutionMode.DOCKER


# ── Resource policy ────────────────────────────────────────────────────────────


def test_task_limit_policy_requires_measured_headroom() -> None:
    assert MIN_HOST_PIDS == 256 and NPROC_MAX == 64
    assert task_limit_violations(64, 256) == []
    assert task_limit_violations(16, 256) == []
    assert min_host_pids(64) == 256 and min_host_pids(16) == 256
    assert "headroom" in task_limit_violations(64, 255)[0]
    assert "headroom" in task_limit_violations(16, 128)[0]
    for nproc in (NPROC_MIN - 1, NPROC_MAX + 1, True, 64.0, "64"):
        assert "NPROC limit" in task_limit_violations(nproc, 4096)[0]  # type: ignore[arg-type]


# ── Configuration ──────────────────────────────────────────────────────────────


def test_config_requires_the_runtime_and_nproc() -> None:
    with pytest.raises(TypeError):
        GVisorExecutorConfig(image=IMAGE)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        GVisorExecutorConfig(image=IMAGE, runtime=REQ)  # type: ignore[call-arg]
    with pytest.raises(ExecutionUnavailableError, match="runtime requirement"):
        GVisorExecutorConfig(image=IMAGE, runtime=None, nproc_limit=64)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [{"nproc_limit": 65}, {"nproc_limit": 8}, {"pids_limit": 128}, {"image": "python:latest"}],
)
def test_config_rejects_unsafe_values(overrides: dict[str, Any]) -> None:
    with pytest.raises(ExecutionUnavailableError, match="misconfigured"):
        _config(**overrides)


def test_executor_refuses_a_plain_docker_configuration() -> None:
    with pytest.raises(ExecutionUnavailableError, match="requires a gVisor configuration"):
        GVisorTestExecutor(DockerExecutorConfig(image=IMAGE))  # type: ignore[arg-type]


def test_config_from_settings_copies_runtime_and_limits() -> None:
    loaded = _settings(**_gvisor_settings(
        codeforge_docker_cli="/usr/local/bin/docker", codeforge_docker_timeout_seconds=42,
        codeforge_docker_cpus=1.5, codeforge_docker_memory_mb=3072, codeforge_docker_pids_limit=300,
        codeforge_gvisor_nproc_limit=32,
    ))  # fmt: skip
    config = GVisorExecutorConfig.from_settings(loaded)
    assert config.runtime == REQ
    assert (config.cli, config.timeout_seconds, config.cpus, config.memory_mb) == (
        "/usr/local/bin/docker", 42, 1.5, 3072,
    )
    assert (config.pids_limit, config.nproc_limit) == (300, 32)


# ── Factory and repair gate ────────────────────────────────────────────────────


def _set(monkeypatch: pytest.MonkeyPatch, **values: Any) -> None:
    for key, value in _gvisor_settings(**values).items():
        monkeypatch.setattr(settings, key, ExecutionMode(value) if key.endswith("_mode") else value)


def test_gvisor_mode_returns_the_gvisor_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch)
    executor = get_test_executor()
    assert type(executor) is GVisorTestExecutor
    assert executor.isolation is IsolationLevel.GVISOR
    assert executor.config.runtime == REQ and executor.config.nproc_limit == 64


@pytest.mark.parametrize(
    "values",
    [
        {"codeforge_gvisor_runtime": ""},
        {"codeforge_gvisor_runtime": "runc"},
        {"codeforge_gvisor_runtime_path": "runsc"},
        {"codeforge_docker_test_image": "python:latest"},
        {"codeforge_docker_pids_limit": 64},
        {"codeforge_gvisor_nproc_limit": 1000},
    ],
)
def test_factory_fails_closed_on_mutated_settings(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, Any]
) -> None:
    """A bypassed or mutated Settings object never yields runc or host execution."""
    _set(monkeypatch, **values)
    executor = get_test_executor()
    assert type(executor) is UnavailableTestExecutor
    assert "gVisor test execution is misconfigured" in executor.reason or "pinned" in executor.reason
    assert not isinstance(executor, DockerTestExecutor | HostTestExecutor)


def test_gvisor_isolation_is_not_sandbox_and_does_not_permit_repairs(fake: FakeDocker) -> None:
    assert IsolationLevel.GVISOR is not IsolationLevel.SANDBOX
    assert IsolationLevel.GVISOR.value != IsolationLevel.SANDBOX.value
    with pytest.raises(ValueError, match="SANDBOX"):
        Orchestrator(executor=_executor(fake), max_repair_attempts=1)


@pytest.mark.asyncio
async def test_orchestrator_refuses_repairs_with_a_gvisor_executor(
    fake: FakeDocker, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "codeforge_max_repair_attempts", 2)
    from app.workspace.manager import WorkspaceManager

    ws = tmp_path / "repo"
    ws.mkdir()
    result = await Orchestrator(executor=_executor(fake)).run(WorkspaceManager(ws), "task")
    assert result.workflow_status == WorkflowStatus.FAILED
    assert "SANDBOX" in (result.failure_reason or "")
    assert fake.calls() == []  # refused before anything ran


# ── Command construction ───────────────────────────────────────────────────────


def test_docker_mode_create_arguments_are_unchanged_from_stage_1c() -> None:
    args = build_create_args(DockerExecutorConfig(image=IMAGE), "codeforge-test-x")
    assert args[:-4] == STAGE_1C_DOCKER_ARGS
    assert args[-4:] == ["-I", "-c", BOOTSTRAP, "360.0"]
    assert "--runtime" not in args and not any(a.startswith("nproc=") for a in args)


def test_gvisor_create_arguments_add_only_the_runtime_nproc_and_guard() -> None:
    args = build_create_args(_config(), "codeforge-test-x")
    assert args[args.index("--runtime") + 1] == RUNTIME
    assert args[args.index("nproc=64:64") - 1] == "--ulimit"
    # Removing exactly those leaves Stage 1C's arguments.
    stripped = list(args)
    i = stripped.index("--runtime")
    del stripped[i : i + 2]
    i = stripped.index("nproc=64:64")
    del stripped[i - 1 : i + 1]
    assert stripped[:-4] == STAGE_1C_DOCKER_ARGS
    guarded = stripped[-2]
    assert stripped[-4:-2] == ["-I", "-c"] and guarded.endswith(BOOTSTRAP)
    assert guarded.index(MARKER_PATH) < guarded.index("tarfile.open")


def test_explicit_runtime_is_never_the_daemon_default() -> None:
    args = build_create_args(_config(), "codeforge-test-x")
    assert args.count("--runtime") == 1
    assert args[args.index("--runtime") + 1] not in ("runc", "", "default")


# ── Lifecycle under the fake daemon ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verified_run_creates_with_runtime_and_nproc(fake: FakeDocker, tmp_path: Path) -> None:
    result = await run_tests_in_snapshot(_executor(fake), _workspace(tmp_path))
    assert result.passed and "1 passed" in result.stdout
    assert fake.commands() == ["info", "create", "inspect", "start", "inspect", "rm"]
    argv = _create_argv(fake)
    assert argv[argv.index("--runtime") + 1] == RUNTIME
    assert "nproc=64:64" in argv
    [name] = fake.created()
    assert SENTINEL in fake.received(name)
    assert fake.is_removed(name)


@pytest.mark.asyncio
async def test_explicit_systrap_registration_runs(fake: FakeDocker, tmp_path: Path) -> None:
    fake.scenario(info_override={"Runtimes": _runtime_entry(runtimeArgs=[*REVIEWED_ARGS, "--platform=systrap"])})
    result = await run_tests_in_snapshot(_executor(fake), _workspace(tmp_path))
    assert result.passed and fake.started() == fake.created()


@pytest.mark.asyncio
async def test_failing_suite_is_a_failed_result(fake: FakeDocker, tmp_path: Path) -> None:
    result = await run_tests_in_snapshot(_executor(fake), _workspace(tmp_path, "fail"))
    assert not result.passed and result.exit_code == 1 and "1 failed" in result.stdout
    [name] = fake.created()
    assert fake.is_removed(name)


_DAEMON_REFUSALS = [
    ("path mismatch", _runtime_entry(path="/usr/local/sbin/runsc"), "path is '/usr/local/sbin/runsc'"),
    ("runc impostor", _runtime_entry(path="/usr/bin/runc", status={FEATURES_KEY: RUNC_FEATURES}),
     "the runtime reports runc features"),
    ("impostor at the configured path", _runtime_entry(status={FEATURES_KEY: RUNC_FEATURES}),
     "the runtime does not report gVisor features"),
    ("not registered", {RUNTIME: None}, "is not registered"),
    ("shim type", _runtime_entry(runtimeType="io.containerd.runsc.v1"), "registered by path"),
    ("missing features", _runtime_entry(status=None), "does not report the runtime's OCI features"),
    ("unreadable features", _runtime_entry(status={FEATURES_KEY: "{not json"}), "unreadable"),
    ("no network=none", _runtime_entry(runtimeArgs=REVIEWED_ARGS[1:]), "--network=none is missing"),
    ("no oci-seccomp", _runtime_entry(runtimeArgs=[REVIEWED_ARGS[0], *REVIEWED_ARGS[2:]]),
     "--oci-seccomp is missing"),
    ("no directfs=false", _runtime_entry(runtimeArgs=[*REVIEWED_ARGS[:2], REVIEWED_ARGS[3]]),
     "--directfs=false is missing"),
    ("no marker file", _runtime_entry(runtimeArgs=REVIEWED_ARGS[:3]), "--gvisor-marker-file is missing"),
    ("debug", _runtime_entry(runtimeArgs=[*REVIEWED_ARGS, "--debug"]), "(debugging)"),
    ("strace", _runtime_entry(runtimeArgs=[*REVIEWED_ARGS, "--strace"]), "(debugging)"),
    ("host uds", _runtime_entry(runtimeArgs=[*REVIEWED_ARGS, "--host-uds=all"]), "(host access)"),
    ("host network", _runtime_entry(runtimeArgs=["--network=host", *REVIEWED_ARGS[1:]]), "(network)"),
    ("flag override", _runtime_entry(runtimeArgs=[*REVIEWED_ARGS, "--allow-flag-override"]),
     "(flag override)"),
    ("testonly", _runtime_entry(runtimeArgs=[*REVIEWED_ARGS, "--TESTONLY-unsafe-nonroot"]), "(test-only)"),
    ("unknown", _runtime_entry(runtimeArgs=[*REVIEWED_ARGS, "--some-new-flag"]), "(unrecognized)"),
    ("kvm platform", _runtime_entry(runtimeArgs=[*REVIEWED_ARGS, "--platform=kvm"]), "(unvalidated platform)"),
    ("systrap and kvm", _runtime_entry(runtimeArgs=[*REVIEWED_ARGS, "--platform=systrap", "--platform=kvm"]),
     "more than one --platform"),
    ("duplicate", _runtime_entry(runtimeArgs=[*REVIEWED_ARGS, "--oci-seccomp"]), "is repeated"),
]  # fmt: skip


@pytest.mark.asyncio
@pytest.mark.parametrize(("runtimes", "message"), [(r, m) for _, r, m in _DAEMON_REFUSALS],
                         ids=[i for i, _, _ in _DAEMON_REFUSALS])  # fmt: skip
async def test_unverified_runtime_is_refused_before_anything_is_created(
    fake: FakeDocker, tmp_path: Path, runtimes: dict[str, Any], message: str
) -> None:
    fake.scenario(info_override={"Runtimes": runtimes})
    with pytest.raises(ExecutionUnavailableError, match="gVisor runtime verification failed") as exc:
        await run_tests_in_snapshot(_executor(fake), _workspace(tmp_path))
    assert message in str(exc.value)
    assert fake.commands() == ["info"]
    assert fake.created() == [] and fake.started() == []


@pytest.mark.asyncio
async def test_stage_1c_daemon_checks_still_apply(fake: FakeDocker, tmp_path: Path) -> None:
    fake.scenario(info_override={"SecurityOptions": ["name=seccomp,profile=unconfined"]})
    with pytest.raises(ExecutionUnavailableError, match="seccomp is not enabled"):
        await run_tests_in_snapshot(_executor(fake), _workspace(tmp_path))
    assert fake.created() == []


_CONTAINER_REFUSALS = [
    ("runc selected", {"HostConfig": {"Runtime": "runc"}}, "Runtime is 'runc'"),
    ("other runtime", {"HostConfig": {"Runtime": "codeforge-gvisor-debug"}}, "Runtime is"),
    ("annotations", {"HostConfig": {"Annotations": {"dev.gvisor.flag.debug": "true"}}}, "Annotations"),
    ("nproc missing", {"HostConfig": {"Ulimits": [{"Name": "core", "Soft": 0, "Hard": 0},
                                                  {"Name": "nofile", "Soft": 1024, "Hard": 1024}]}},
     "Ulimits"),
    ("nproc altered", {"HostConfig": {"Ulimits": [{"Name": "core", "Soft": 0, "Hard": 0},
                                                  {"Name": "nofile", "Soft": 1024, "Hard": 1024},
                                                  {"Name": "nproc", "Soft": 64, "Hard": 128}]}},
     "Ulimits"),
    ("privileged", {"HostConfig": {"Privileged": True}}, "Privileged"),
    ("bind mount", {"HostConfig": {"Binds": ["/srv/workspace:/workspace"]}}, "Binds"),
    ("docker socket", {"Mounts": [{"Source": "/var/run/docker.sock"}]}, "container has mounts"),
    ("network", {"HostConfig": {"NetworkMode": "bridge"}}, "NetworkMode"),
    ("unguarded bootstrap", {"Config": {"Cmd": ["-I", "-c", BOOTSTRAP, "360.0"]}}, "Cmd"),
]  # fmt: skip


@pytest.mark.asyncio
@pytest.mark.parametrize(("tamper", "message"), [(t, m) for _, t, m in _CONTAINER_REFUSALS],
                         ids=[i for i, _, _ in _CONTAINER_REFUSALS])  # fmt: skip
async def test_container_that_differs_is_removed_and_never_started(
    fake: FakeDocker, tmp_path: Path, tamper: dict[str, Any], message: str
) -> None:
    fake.scenario(tamper=tamper)
    with pytest.raises(ExecutionUnavailableError, match="did not apply") as exc:
        await run_tests_in_snapshot(_executor(fake), _workspace(tmp_path))
    assert message in str(exc.value)
    [name] = fake.created()
    assert fake.started() == [] and fake.received(name) == []
    assert fake.is_removed(name) and fake.removed_names() == [name]


@pytest.mark.asyncio
async def test_missing_marker_fails_before_the_snapshot_is_read(fake: FakeDocker, tmp_path: Path) -> None:
    fake.scenario(marker="missing")
    with pytest.raises(ExecutionUnavailableError, match="marker was missing") as exc:
        await run_tests_in_snapshot(_executor(fake), _workspace(tmp_path))
    assert "the workspace was not loaded" in str(exc.value)
    [name] = fake.created()
    assert fake.started() == [name]
    assert fake.received(name) == []  # nothing extracted
    assert fake.is_removed(name)


@pytest.mark.asyncio
async def test_completed_run_is_discarded_when_removal_fails(fake: FakeDocker, tmp_path: Path) -> None:
    fake.scenario(rm="fail")
    with pytest.raises(ExecutionUnavailableError, match="could not be removed after the run"):
        await run_tests_in_snapshot(_executor(fake), _workspace(tmp_path, "pass"))


@pytest.mark.asyncio
async def test_refusal_is_preserved_when_removal_also_fails(fake: FakeDocker, tmp_path: Path) -> None:
    fake.scenario(rm="fail", tamper={"HostConfig": {"Runtime": "runc"}})
    with pytest.raises(ExecutionUnavailableError, match="Runtime is 'runc'") as exc:
        await run_tests_in_snapshot(_executor(fake), _workspace(tmp_path))
    assert "could not be removed" not in str(exc.value)
    assert fake.started() == []


@pytest.mark.asyncio
async def test_cancellation_is_preserved_when_removal_also_fails(
    fake: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(docker_executor, "_DRAIN_SECONDS", 0.5)
    fake.scenario(rm="fail")
    with caplog.at_level(logging.ERROR, logger="app.execution.docker_executor"):
        async with _jobs() as jobs:
            task = asyncio.create_task(run_tests_in_snapshot(_executor(fake), _workspace(tmp_path, "hang")))
            jobs.append(task)
            await _cancel_after_start(fake, task, times=1)  # asserts CancelledError
    [name] = fake.created()
    assert set(fake.removed_names()) == {name}
    assert f"could not remove container {name}" in caplog.text


@pytest.mark.asyncio
async def test_cancellation_removes_the_container(fake: FakeDocker, tmp_path: Path) -> None:
    async with _jobs() as jobs:
        task = asyncio.create_task(run_tests_in_snapshot(_executor(fake), _workspace(tmp_path, "hang")))
        jobs.append(task)
        await _cancel_after_start(fake, task, times=3)
    [name] = fake.created()
    assert fake.is_removed(name)


@pytest.mark.asyncio
async def test_lease_loss_stops_only_the_affected_jobs_container(fake: FakeDocker, tmp_path: Path) -> None:
    lost_a, lost_b = asyncio.Event(), asyncio.Event()
    async with _jobs() as jobs:
        job_a = asyncio.create_task(run_until_ownership_lost(
            run_tests_in_snapshot(_executor(fake), _workspace(tmp_path, "hang", "a")), lost_a))  # fmt: skip
        jobs.append(job_a)
        await fake.wait_started(1)
        [name_a] = fake.created()
        job_b = asyncio.create_task(run_until_ownership_lost(
            run_tests_in_snapshot(_executor(fake), _workspace(tmp_path, "until_release", "b")), lost_b))  # fmt: skip
        jobs.append(job_b)
        await fake.wait_started(2)
        [name_b] = [n for n in fake.created() if n != name_a]

        lost_a.set()
        with pytest.raises(OwnershipLostError):
            await job_a
        assert fake.is_removed(name_a) and not fake.is_removed(name_b)
        assert name_b not in fake.removed_names() and not job_b.done()

        (fake.state / "release").touch()
        assert (await job_b).passed
        assert fake.is_removed(name_b)


@pytest.mark.asyncio
async def test_timeout_removes_the_container(fake: FakeDocker, tmp_path: Path) -> None:
    result = await run_tests_in_snapshot(_executor(fake, timeout_seconds=1), _workspace(tmp_path, "hang"))
    assert not result.passed and result.exit_code == -1
    [name] = fake.created()
    assert fake.is_removed(name)


@pytest.mark.asyncio
async def test_output_stays_bounded(fake: FakeDocker, tmp_path: Path) -> None:
    result = await run_tests_in_snapshot(_executor(fake, max_output_bytes=1000), _workspace(tmp_path, "noisy:200000"))
    assert len(result.stdout) < 1100 and len(result.stderr) < 1100


@pytest.mark.asyncio
async def test_refused_gvisor_never_falls_back_to_runc_or_the_host(
    fake: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran_on_host: list[Any] = []

    async def host_run(self: Any, snapshot: Any) -> Any:
        ran_on_host.append(snapshot)
        raise AssertionError("host execution")

    monkeypatch.setattr(HostTestExecutor, "run", host_run)
    fake.scenario(info_override={"Runtimes": {RUNTIME: None}})
    with pytest.raises(ExecutionUnavailableError):
        await run_tests_in_snapshot(_executor(fake), _workspace(tmp_path))
    assert ran_on_host == []
    assert [c for c in fake.calls() if c["argv"][0] in ("create", "start")] == []
