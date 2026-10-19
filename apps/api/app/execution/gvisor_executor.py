"""gVisor test execution (Phase 10B.5 Stage 1D).

``CODEFORGE_TEST_EXECUTION_MODE=gvisor`` runs the Stage 1C container (same
snapshot input, controls, verification and lifecycle; see
:mod:`app.execution.docker_executor`) under the operator-registered gVisor
runtime.  Design, evidence and residual risks are in
``docs/security/gvisor-test-executor.md``; in short:

* Before anything is created, ``docker info`` must show the configured runtime
  registered by path, at exactly the configured path, with exactly the reviewed
  runsc arguments, and reporting gVisor (not runc) OCI features
  (:func:`~app.execution.gvisor_runtime.daemon_runtime_violations`).
* The container is created with an explicit ``--runtime``, never the daemon
  default, plus ``--ulimit nproc=N:N``.  After ``docker create`` it is only
  started if Docker recorded that runtime, no annotations, the exact NPROC
  ulimit and every Stage 1C control.
* The trusted bootstrap checks the gVisor marker before it reads the snapshot
  from stdin; without it the run fails and no repository file is extracted.
  The marker only corroborates the host-side checks, which are authoritative.
* Task limits have two layers: RLIMIT_NPROC bounds the guest's tasks (gVisor
  enforces it per sandbox), and the host ``--pids-limit`` bounds runsc's host
  tasks with headroom above NPROC (:func:`~app.execution.gvisor_runtime.task_limit_violations`).

Any failure raises :class:`~app.execution.test_executor.ExecutionUnavailableError`;
there is no fallback to runc or the host.  The executor reports
:attr:`IsolationLevel.GVISOR`, which does not permit automatic repairs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.execution.docker_executor import (
    BOOTSTRAP,
    ULIMITS,
    DockerExecutorConfig,
    DockerTestExecutor,
    _DockerCLI,
    _summary,
)
from app.execution.gvisor_runtime import (
    RUNTIME_GUARD_EXIT,
    GVisorRuntimeRequirement,
    container_runtime_violations,
    daemon_runtime_violations,
    guarded_bootstrap,
    task_limit_violations,
)
from app.execution.test_executor import ExecutionUnavailableError, IsolationLevel


@dataclass(frozen=True)
class GVisorExecutorConfig(DockerExecutorConfig):
    """Stage 1C's configuration plus the gVisor runtime and the guest NPROC limit.

    Both are required: there is no way to build this configuration without a
    runtime, so it cannot quietly describe an ordinary runc container.
    """

    runtime: GVisorRuntimeRequirement = field(kw_only=True)
    nproc_limit: int = field(kw_only=True)

    def __post_init__(self) -> None:
        super().__post_init__()
        problems: list[str] = []
        if not isinstance(self.runtime, GVisorRuntimeRequirement):
            problems.append("a gVisor runtime requirement is required")
        problems += task_limit_violations(self.nproc_limit, self.pids_limit)
        if problems:
            raise ExecutionUnavailableError(
                "gVisor test execution is misconfigured: " + "; ".join(problems)
            )

    @classmethod
    def from_settings(cls, settings: Settings) -> GVisorExecutorConfig:
        try:
            runtime = GVisorRuntimeRequirement(
                name=settings.codeforge_gvisor_runtime, path=settings.codeforge_gvisor_runtime_path
            )
        except ValueError as exc:
            raise ExecutionUnavailableError(f"gVisor test execution is misconfigured: {exc}") from None
        return cls(
            image=settings.codeforge_docker_test_image,
            cli=settings.codeforge_docker_cli,
            timeout_seconds=settings.codeforge_docker_timeout_seconds,
            cpus=settings.codeforge_docker_cpus,
            memory_mb=settings.codeforge_docker_memory_mb,
            pids_limit=settings.codeforge_docker_pids_limit,
            runtime=runtime,
            nproc_limit=settings.codeforge_gvisor_nproc_limit,
        )

    def container_cmd(self) -> list[str]:
        """The Stage 1C bootstrap behind the marker guard, and its deadline."""
        deadline = float(self.timeout_seconds) + float(self.container_grace_seconds)
        return ["-I", "-c", guarded_bootstrap(BOOTSTRAP), repr(deadline)]

    def ulimits(self) -> tuple[tuple[str, int, int], ...]:
        # gVisor only: under runc, nproc counts every process of the host UID
        # across containers, so it must never be applied in docker mode.
        return (*ULIMITS, ("nproc", self.nproc_limit, self.nproc_limit))

    def runtime_args(self) -> list[str]:
        return ["--runtime", self.runtime.name]


class GVisorTestExecutor(DockerTestExecutor):
    """Runs pytest on a snapshot in a hardened, throwaway container under gVisor."""

    config: GVisorExecutorConfig

    def __init__(self, config: GVisorExecutorConfig) -> None:
        if not isinstance(config, GVisorExecutorConfig):
            raise ExecutionUnavailableError("gVisor test execution requires a gVisor configuration")
        super().__init__(config)

    @property
    def isolation(self) -> IsolationLevel:
        return IsolationLevel.GVISOR

    async def _preflight(self, cli: _DockerCLI) -> dict[str, Any]:
        info = await super()._preflight(cli)
        problems = daemon_runtime_violations(info, self.config.runtime)
        if problems:
            raise ExecutionUnavailableError(
                "gVisor runtime verification failed: " + "; ".join(problems)
            )
        return info

    def _container_problems(self, inspect: Mapping[str, Any]) -> list[str]:
        return super()._container_problems(inspect) + container_runtime_violations(
            inspect, self.config.runtime
        )

    def _bootstrap_failure(self, exit_code: int, stderr: str) -> str | None:
        if exit_code == RUNTIME_GUARD_EXIT:
            return (
                "The gVisor runtime marker was missing in the test container; "
                f"the workspace was not loaded: {_summary(stderr)}"
            )
        return super()._bootstrap_failure(exit_code, stderr)
