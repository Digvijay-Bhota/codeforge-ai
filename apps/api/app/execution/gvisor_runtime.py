"""gVisor runtime verification and resource policy (Phase 10B.5 Stage 1D).

Pure checks that decide whether the Docker daemon will run a container under
the operator-registered gVisor runtime with the reviewed arguments, and the
guest/host task-limit policy.  :mod:`app.execution.gvisor_executor` applies
them for ``CODEFORGE_TEST_EXECUTION_MODE=gvisor``; settings validation uses the
same policy.  The CI workflow ``gvisor-spike.yml`` exercises them against a
real daemon with a pinned, checksum-verified runsc.

Evidence is layered.  The host-side checks are authoritative; the in-sandbox
marker only corroborates them:

1. ``docker info``: the runtime is registered by ``path`` (not by a shim type
   whose arguments cannot be inspected), the path is the configured one, the
   arguments are exactly the reviewed set, and the binary describes itself as
   gVisor rather than runc in its OCI features.
2. ``docker inspect`` of the created container: Docker selected that runtime
   and no annotations (``dev.gvisor.flag.*`` overrides) are attached.
3. Before the snapshot is read from stdin, the trusted bootstrap requires the
   ``/proc/gvisor/kernel_is_gvisor`` marker (``--gvisor-marker-file``) and
   otherwise exits with :data:`RUNTIME_GUARD_EXIT` without extracting anything.
   ``dmesg`` and ``/proc/version`` are deliberately not used: gVisor documents
   that they are easily replicated.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

RUNTIME_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
# Names that denote an ordinary runtime; never acceptable as the gVisor runtime.
RESERVED_RUNTIME_NAMES = frozenset({
    "runc", "io.containerd.runc.v1", "io.containerd.runc.v2", "io.containerd.runtime.v1.linux",
    "crun", "youki", "sysbox-runc", "nvidia",
})  # fmt: skip

# ── Task-limit policy ──────────────────────────────────────────────────────────
#
# Under gVisor, Docker's --pids-limit bounds runsc's *host* tasks, not the
# guest's: the spike started 200 guest threads under a host limit of 64, and a
# fork loop that reached the host limit killed the sandbox instead of failing
# with EAGAIN.  The guest bound is RLIMIT_NPROC (--ulimit nproc=N:N), which
# gVisor enforces per sandbox for callers without CAP_SYS_ADMIN/CAP_SYS_RESOURCE.
#
# The host limit must therefore leave headroom above what N guest tasks cost on
# the host, so that NPROC (a clean EAGAIN) is always reached first.  Measured on
# the CI runner (systrap, release-20261005.0): NPROC 64 with --pids-limit 256
# peaked at 170 host tasks (~66 %); each guest process cost ~1.9 extra host
# tasks over a ~45-task sandbox.  The policy is an explicit floor, not a
# formula assumed to hold elsewhere: NPROC is capped at the measured 64, and the
# host limit must be at least 256 (and at least 4 x NPROC).  Raising either
# needs new measurements; the real-runtime CI also requires the measured peak
# to stay within 80 % of the host limit.
NPROC_MIN = 16
NPROC_MAX = 64
MIN_HOST_PIDS = 256
HOST_PIDS_PER_NPROC = 4
HOST_PIDS_MAX_PEAK_FRACTION = 0.8


def min_host_pids(nproc: int) -> int:
    """Smallest permitted host --pids-limit for a guest RLIMIT_NPROC of *nproc*."""
    return max(MIN_HOST_PIDS, HOST_PIDS_PER_NPROC * nproc)


def task_limit_violations(nproc: int, pids_limit: int) -> list[str]:
    """Why (*nproc*, host *pids_limit*) is not an accepted gVisor task-limit pair."""
    problems: list[str] = []
    if isinstance(nproc, bool) or not isinstance(nproc, int) or not NPROC_MIN <= nproc <= NPROC_MAX:
        problems.append(f"the NPROC limit must be an integer in [{NPROC_MIN}, {NPROC_MAX}], got {nproc!r}")
    elif pids_limit < min_host_pids(nproc):
        problems.append(
            f"the host PIDs limit {pids_limit} leaves too little headroom above NPROC {nproc}; "
            f"at least {min_host_pids(nproc)} is required"
        )
    return problems

# The reviewed runsc profile.  Every argument must be one of these exact tokens:
# runsc also accepts "--flag value", "-flag" and "--flag=false", so anything
# other than an exact match is refused rather than parsed.
REQUIRED_RUNTIME_ARGS: tuple[str, ...] = (
    "--network=none",  # no network stack in the sandbox, whatever Docker attaches
    "--oci-seccomp",  # apply Docker's seccomp profile inside the sandbox too
    "--directfs=false",  # the Sentry reaches files only through the gofer
    "--gvisor-marker-file",  # corroborating marker for the bootstrap guard
)
OPTIONAL_RUNTIME_ARGS = frozenset({"--platform=systrap", "--platform=kvm"})

# Only used to explain a refusal; the allowlist above is what decides.
_FLAG_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("flag override", ("allow-flag-override",)),
    ("test-only", ("TESTONLY-",)),
    ("debugging", ("debug", "strace", "log", "pcap-log", "profile", "trace", "panic-log",
                   "coverage")),
    ("host access", ("host-uds", "host-fifo", "rootless", "nvproxy", "tpuproxy", "directfs",
                     "gofer-network-namespace", "overlay2", "file-access", "host-settings")),
    ("network", ("network", "net-raw", "allow-packet-socket-write", "num-network-channels")),
    ("resource limits", ("ignore-cgroups", "systemd-cgroup", "cpu-num-from-quota",
                         "total-memory", "total-host-memory")),
    ("seccomp", ("oci-seccomp",)),
)  # fmt: skip

FEATURES_KEY = "org.opencontainers.runtime-spec.features"
GVISOR_ANNOTATION_PREFIX = "dev.gvisor."
RUNC_ANNOTATION_PREFIX = "org.opencontainers.runc."

MARKER_PATH = "/proc/gvisor/kernel_is_gvisor"
# Exit status the bootstrap guard uses when the marker is missing.
RUNTIME_GUARD_EXIT = 96
GUARD_REFUSAL = "codeforge: gVisor runtime marker missing; the workspace was not loaded"

# Prepended to the trusted bootstrap.  Runs before stdin (the snapshot) is read,
# so no repository file exists in the container if the marker is absent.
GUARD = f"""\
import os as _cf_os, sys as _cf_sys
if not _cf_os.path.isfile({MARKER_PATH!r}):
    _cf_sys.stderr.write({GUARD_REFUSAL + chr(10)!r})
    _cf_sys.stderr.flush()
    _cf_os._exit({RUNTIME_GUARD_EXIT})
del _cf_os, _cf_sys
"""


@dataclass(frozen=True)
class GVisorRuntimeRequirement:
    """The operator-configured gVisor runtime: its Docker name and runsc path."""

    name: str
    path: str

    def __post_init__(self) -> None:
        problems: list[str] = []
        if not RUNTIME_NAME_PATTERN.fullmatch(self.name) or self.name in RESERVED_RUNTIME_NAMES:
            problems.append(f"runtime name {self.name!r} is not a dedicated gVisor runtime name")
        if (
            not self.path.startswith("/")
            or os.path.normpath(self.path) != self.path
            or any(c.isspace() for c in self.path)
        ):
            problems.append(f"runtime path {self.path!r} must be an absolute, normalized path")
        if problems:
            raise ValueError("; ".join(problems))


def _category(token: str) -> str:
    name = token.lstrip("-").split("=", 1)[0]
    for label, prefixes in _FLAG_CATEGORIES:
        if any(name == p or name.startswith(p) for p in prefixes):
            return label
    return "unrecognized"


def runtime_arg_violations(args: Iterable[Any]) -> list[str]:
    """Every way *args* differs from the reviewed runsc profile."""
    tokens = list(args)
    problems: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if not isinstance(token, str):
            problems.append(f"runtime argument {token!r} is not a string")
            continue
        if token in seen:
            problems.append(f"runtime argument {token!r} is repeated")
        seen.add(token)
        if token not in REQUIRED_RUNTIME_ARGS and token not in OPTIONAL_RUNTIME_ARGS:
            problems.append(f"runtime argument {token!r} is not permitted ({_category(token)})")
    for required in REQUIRED_RUNTIME_ARGS:
        if required not in seen:
            problems.append(f"required runtime argument {required} is missing")
    if len(seen & OPTIONAL_RUNTIME_ARGS) > 1:
        problems.append("more than one --platform is given")
    return problems


def runtime_features_violations(entry: Mapping[str, Any]) -> list[str]:
    """Whether the registered binary describes itself as gVisor (not runc).

    Docker reports what ``<path> features`` printed.  This is the binary's own
    claim, so it corroborates the path check rather than replacing it.
    """
    status = entry.get("status")
    raw = status.get(FEATURES_KEY) if isinstance(status, Mapping) else None
    if not isinstance(raw, str):
        return ["Docker does not report the runtime's OCI features"]
    try:
        features = json.loads(raw)
    except ValueError:
        return ["the runtime's OCI features are unreadable"]
    annotations = features.get("annotations") if isinstance(features, dict) else None
    if not isinstance(annotations, dict):
        return ["the runtime's OCI features have no annotations"]
    problems: list[str] = []
    if not any(str(k).startswith(GVISOR_ANNOTATION_PREFIX) for k in annotations):
        problems.append("the runtime does not report gVisor features")
    if any(str(k).startswith(RUNC_ANNOTATION_PREFIX) for k in annotations):
        problems.append("the runtime reports runc features")
    return problems


def daemon_runtime_violations(info: Mapping[str, Any], req: GVisorRuntimeRequirement) -> list[str]:
    """Why ``docker info`` does not show *req* registered with the reviewed profile."""
    runtimes = info.get("Runtimes")
    entry = runtimes.get(req.name) if isinstance(runtimes, Mapping) else None
    if not isinstance(entry, Mapping):
        return [f"runtime {req.name!r} is not registered with the Docker daemon"]
    problems: list[str] = []
    if entry.get("runtimeType") or entry.get("options"):
        # Shim-type registrations keep their runsc flags in a config file that
        # Docker does not report, so they cannot be verified.
        problems.append(f"runtime {req.name!r} must be registered by path, not by runtime type")
    if entry.get("path") != req.path:
        problems.append(f"runtime {req.name!r} path is {entry.get('path')!r}, expected {req.path!r}")
    args = entry.get("runtimeArgs") or []
    if not isinstance(args, list):
        problems.append(f"runtime {req.name!r} arguments are unreadable")
    else:
        problems += runtime_arg_violations(args)
    problems += runtime_features_violations(entry)
    return problems


def container_runtime_violations(inspect: Mapping[str, Any], req: GVisorRuntimeRequirement) -> list[str]:
    """Why the created container is not set to run under *req*."""
    host: Mapping[str, Any] = inspect.get("HostConfig") or {}
    problems: list[str] = []
    if host.get("Runtime") != req.name:
        problems.append(f"Runtime is {host.get('Runtime')!r}, expected {req.name!r}")
    if host.get("Annotations"):
        problems.append(f"Annotations must be empty, got {host.get('Annotations')!r}")
    return problems


def guarded_bootstrap(bootstrap: str) -> str:
    """*bootstrap* preceded by the marker guard."""
    return GUARD + bootstrap
