"""Phase 10B.5 Stage 1D spike — gVisor runtime verification (no Docker required).

These pin down what app.execution.gvisor_runtime accepts and refuses, using
synthetic ``docker info`` / ``docker inspect`` data.  They are unit tests of the
checks only, not evidence that gVisor works: that comes from the real-runtime
spike in sandbox/gvisor/spike/, which runs in its own CI job.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 - runs the guard locally with a fixed interpreter
import sys
from typing import Any

import pytest

from app.execution.docker_executor import BOOTSTRAP
from app.execution.gvisor_runtime import (
    GUARD_REFUSAL,
    MARKER_PATH,
    REQUIRED_RUNTIME_ARGS,
    RUNTIME_GUARD_EXIT,
    GVisorRuntimeRequirement,
    container_runtime_violations,
    daemon_runtime_violations,
    guarded_bootstrap,
    runtime_arg_violations,
    runtime_features_violations,
)

REQ = GVisorRuntimeRequirement(name="codeforge-gvisor", path="/usr/local/bin/runsc")
GVISOR_FEATURES = json.dumps(
    {"annotations": {"dev.gvisor.flag.debug": "false", "dev.gvisor.internal.seccomp.": ""}}
)
RUNC_FEATURES = json.dumps({"annotations": {"org.opencontainers.runc.version": "1.3.4"}})


def _entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": REQ.path,
        "runtimeArgs": list(REQUIRED_RUNTIME_ARGS),
        "status": {"org.opencontainers.runtime-spec.features": GVISOR_FEATURES},
    }
    entry.update(overrides)
    return entry


def _info(entry: dict[str, Any] | None = None) -> dict[str, Any]:
    runtimes: dict[str, Any] = {"runc": {"path": "runc"}}
    if entry is not None:
        runtimes[REQ.name] = entry
    return {"Runtimes": runtimes, "DefaultRuntime": "runc"}


# ── Requirement ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["runc", "io.containerd.runc.v2", "", "-runsc", "Runsc", "a b"])
def test_requirement_rejects_ordinary_or_malformed_names(name: str) -> None:
    with pytest.raises(ValueError, match="not a dedicated gVisor runtime name"):
        GVisorRuntimeRequirement(name=name, path="/usr/local/bin/runsc")


@pytest.mark.parametrize("path", ["runsc", "usr/local/bin/runsc", "/usr/local/../bin/runsc", "/a b"])
def test_requirement_rejects_relative_or_unnormalized_paths(path: str) -> None:
    with pytest.raises(ValueError, match="absolute, normalized"):
        GVisorRuntimeRequirement(name="codeforge-gvisor", path=path)


# ── Runtime arguments ──────────────────────────────────────────────────────────


def test_reviewed_profile_is_accepted() -> None:
    assert runtime_arg_violations(REQUIRED_RUNTIME_ARGS) == []
    assert runtime_arg_violations([*REQUIRED_RUNTIME_ARGS, "--platform=systrap"]) == []


@pytest.mark.parametrize("missing", REQUIRED_RUNTIME_ARGS)
def test_each_required_argument_is_required(missing: str) -> None:
    args = [a for a in REQUIRED_RUNTIME_ARGS if a != missing]
    assert runtime_arg_violations(args) == [f"required runtime argument {missing} is missing"]


@pytest.mark.parametrize(
    ("extra", "category"),
    [
        ("--allow-flag-override", "flag override"),
        ("--TESTONLY-unsafe-nonroot", "test-only"),
        ("--debug", "debugging"),
        ("--debug-log=/tmp/runsc/", "debugging"),
        ("--strace", "debugging"),
        ("--log-packets", "debugging"),
        ("--host-uds=all", "host access"),
        ("--host-fifo=open", "host access"),
        ("--rootless", "host access"),
        ("--nvproxy", "host access"),
        ("--directfs=true", "host access"),
        ("--network=host", "network"),
        ("--network=sandbox", "network"),
        ("--net-raw", "network"),
        ("--allow-packet-socket-write", "network"),
        ("--ignore-cgroups", "resource limits"),
        ("--oci-seccomp=false", "seccomp"),
        ("--platform=ptrace", "unrecognized"),
        ("--some-future-flag", "unrecognized"),
        ("-debug", "debugging"),
        ("none", "unrecognized"),
    ],
)
def test_weakening_or_unknown_arguments_are_refused(extra: str, category: str) -> None:
    problems = runtime_arg_violations([*REQUIRED_RUNTIME_ARGS, extra])
    assert problems == [f"runtime argument {extra!r} is not permitted ({category})"]


def test_two_token_and_repeated_forms_are_refused() -> None:
    # "--network none" would be parsed by runsc, but only exact tokens are allowed.
    args = [a for a in REQUIRED_RUNTIME_ARGS if a != "--network=none"] + ["--network", "none"]
    problems = runtime_arg_violations(args)
    assert "required runtime argument --network=none is missing" in problems
    assert len(problems) == 3
    repeated = runtime_arg_violations([*REQUIRED_RUNTIME_ARGS, "--oci-seccomp"])
    assert repeated == ["runtime argument '--oci-seccomp' is repeated"]
    platforms = runtime_arg_violations([*REQUIRED_RUNTIME_ARGS, "--platform=kvm", "--platform=systrap"])
    assert platforms == ["more than one --platform is given"]


def test_non_string_arguments_are_refused() -> None:
    assert "is not a string" in runtime_arg_violations([*REQUIRED_RUNTIME_ARGS, 1])[0]


# ── docker info ────────────────────────────────────────────────────────────────


def test_registered_reviewed_runtime_is_accepted() -> None:
    assert daemon_runtime_violations(_info(_entry()), REQ) == []


def test_missing_runtime_is_refused() -> None:
    assert daemon_runtime_violations(_info(), REQ) == [
        "runtime 'codeforge-gvisor' is not registered with the Docker daemon"
    ]
    assert daemon_runtime_violations({}, REQ) != []
    assert daemon_runtime_violations({"Runtimes": None}, REQ) != []


def test_runc_registered_under_the_gvisor_name_is_refused() -> None:
    impostor = _entry(path="/usr/bin/runc", status={"org.opencontainers.runtime-spec.features": RUNC_FEATURES})
    problems = daemon_runtime_violations(_info(impostor), REQ)
    assert any("path is '/usr/bin/runc'" in p for p in problems)
    assert "the runtime does not report gVisor features" in problems
    assert "the runtime reports runc features" in problems


def test_runc_at_the_configured_path_is_refused_by_its_features() -> None:
    """Even if the operator configured runc's path, the features give it away."""
    req = GVisorRuntimeRequirement(name="codeforge-gvisor", path="/usr/bin/runc")
    impostor = _entry(path="/usr/bin/runc", status={"org.opencontainers.runtime-spec.features": RUNC_FEATURES})
    assert daemon_runtime_violations(_info(impostor), req) == [
        "the runtime does not report gVisor features",
        "the runtime reports runc features",
    ]


def test_shim_type_registration_is_refused() -> None:
    entry = _entry(runtimeType="io.containerd.runsc.v1", options={"TypeUrl": "x"})
    assert any("registered by path" in p for p in daemon_runtime_violations(_info(entry), REQ))


@pytest.mark.parametrize(
    "status",
    [None, {}, {"org.opencontainers.runtime-spec.features": "{not json"},
     {"org.opencontainers.runtime-spec.features": "[]"},
     {"org.opencontainers.runtime-spec.features": json.dumps({"annotations": {}})}],
)  # fmt: skip
def test_missing_or_unreadable_features_are_refused(status: Any) -> None:
    assert runtime_features_violations({"status": status}) != []


def test_unreadable_arguments_are_refused() -> None:
    problems = daemon_runtime_violations(_info(_entry(runtimeArgs="--network=none")), REQ)
    assert "arguments are unreadable" in problems[0]


# ── docker inspect ─────────────────────────────────────────────────────────────


def test_container_must_select_the_runtime_without_annotations() -> None:
    assert container_runtime_violations({"HostConfig": {"Runtime": REQ.name}}, REQ) == []
    assert container_runtime_violations({"HostConfig": {"Runtime": "runc"}}, REQ) == [
        "Runtime is 'runc', expected 'codeforge-gvisor'"
    ]
    assert container_runtime_violations({}, REQ) == ["Runtime is None, expected 'codeforge-gvisor'"]
    annotated = {"HostConfig": {"Runtime": REQ.name, "Annotations": {"dev.gvisor.flag.debug": "true"}}}
    assert container_runtime_violations(annotated, REQ) == [
        "Annotations must be empty, got {'dev.gvisor.flag.debug': 'true'}"
    ]


# ── Bootstrap guard ────────────────────────────────────────────────────────────


def test_guard_refuses_before_reading_stdin_without_the_marker(tmp_path: Any) -> None:
    """Outside gVisor the marker is absent: the guard exits before stdin is read."""
    sentinel = tmp_path / "sentinel"
    script = guarded_bootstrap(f"open({str(sentinel)!r}, 'w').write('ran')\n")
    proc = subprocess.run(  # nosec B603 - fixed interpreter and script
        [sys.executable, "-I", "-c", script], input=b"never read", capture_output=True, timeout=30,
        check=False,
    )
    assert proc.returncode == RUNTIME_GUARD_EXIT
    assert GUARD_REFUSAL in proc.stderr.decode()
    assert not sentinel.exists()


def test_guard_precedes_the_stage_1c_bootstrap() -> None:
    script = guarded_bootstrap(BOOTSTRAP)
    assert script.index(MARKER_PATH) < script.index("tarfile.open")
    assert script.endswith(BOOTSTRAP)
    compile(script, "<bootstrap>", "exec")
