# Docker test executor (Phase 10B.5 Stage 1C)

`CODEFORGE_TEST_EXECUTION_MODE=docker` runs a repository's test suite in a
hardened, throwaway Docker container built from the immutable workspace
snapshot.  This document records the design, the threat model it answers, the
controls it applies and what it does **not** protect against.

Docker is a container boundary, not a virtual machine.  Containers share the
host kernel; a kernel or container-runtime vulnerability can break out of any
configuration described here.  This executor is a substantial improvement over
`host_unsafe`, not complete isolation.  Stage 1D runs the same container under
gVisor: see [`gvisor-test-executor.md`](gvisor-test-executor.md).

## 1. Execution architecture before Stage 1C

* Every test run (testing stage, the coding agent's `run_tests` tool, repair
  attempts) goes through one `TestExecutor`, chosen by
  `CODEFORGE_TEST_EXECUTION_MODE` (`app/execution/test_executor.py`).
* `run_tests_in_snapshot` builds a `WorkspaceSnapshot`: a deterministic tar
  archive of the workspace, policy-filtered (`.git`, secret-looking names,
  special files and unsafe symlinks excluded), in a 0700 temporary directory
  that is deleted afterwards.  Executors receive the snapshot, never the
  trusted workspace, and nothing is ever copied back.
* `disabled` (default) fails closed.  `host_unsafe` extracts the snapshot to a
  temporary directory and runs pytest **on the worker host under the worker's
  UID** — hygiene, not isolation; development/test only.  `sandbox` is reserved
  and fails closed.
* The worker cancels the active execution when it loses a job's lease
  (`run_until_ownership_lost`), awaits the executor's cleanup, and discards any
  result produced afterwards; it never finalizes a job it no longer owns.
* Automatic repair attempts require `IsolationLevel.SANDBOX` and
  `CODEFORGE_TEST_EXECUTION_MODE=sandbox`; the default budget is 0.

## 2. Threat model

**Adversary:** the repository under test.  Its tests, `conftest.py`, pytest
plugins and anything they import are arbitrary code, possibly written by an
attacker or by a prompt-injected agent.

**Assets to protect:**

1. Worker credentials: GitHub installation tokens, the OpenAI key, database
   and Redis URLs, JWT secret (worker environment and `/proc/<pid>/environ`).
2. The trusted workspace the worker later commits and pushes from, and its
   `.git` directory (hooks, config).
3. The Docker daemon (root-equivalent on its host) and the host itself.
4. Other jobs running concurrently on the same worker or daemon.
5. Availability of the worker host (CPU, memory, PIDs, disk).
6. Integrity of job results: a stale worker must not publish them.

**Assumptions:**

* The worker process, the CodeForge code and the executor image are trusted.
* The operator controls the Docker daemon and the image store; the configured
  image is the one built from `sandbox/docker/`.
* The host kernel, runc/containerd and Docker are patched.
* The test result itself is untrusted output (repository code chooses its exit
  status); it is bounded and never executed or interpreted.

**Out of scope for Stage 1C:** kernel or runtime escapes (Stage 1D:
[`gvisor-test-executor.md`](gvisor-test-executor.md)),
installing repository dependencies, network access for tests, multi-host
scheduling.

## 3. Container security controls

Each run creates one container named `codeforge-test-<random 128-bit hex>`,
labelled `codeforge.managed=test-executor`.  The executor creates it, verifies
the daemon applied the requested configuration, starts it, and removes it.

| Control | Setting | Why |
| --- | --- | --- |
| Workspace input | snapshot tar streamed on **stdin**, extracted inside the container to a tmpfs at `/workspace` | No host path is mounted at all — not the trusted workspace, not a temporary copy.  The host-side archive is re-verified (SHA-256 and member validation) before it is streamed. |
| No write-back | `/workspace` is a tmpfs destroyed with the container | Nothing the tests write ever reaches the host. |
| Mounts | no `-v`/`--mount`/`--volumes-from`; verified empty after create | No Docker socket, no host files, no credentials. |
| User | `--user 65534:65534` | Non-root; maps to `nobody` on the host if the container is escaped without user-namespace remapping. |
| Capabilities | `--cap-drop ALL`, no `--cap-add` | Removes the default capability set. |
| Privilege escalation | `--security-opt no-new-privileges` | setuid binaries cannot raise privileges. |
| Seccomp / AppArmor | daemon defaults; the executor refuses daemons without seccomp and containers whose AppArmor profile is `unconfined` | Default syscall filter stays on. |
| Root filesystem | `--read-only` | Image cannot be modified; only the tmpfs mounts are writable. |
| Writable paths | `/workspace` (rw, exec, nosuid, nodev, 512 MiB, owned by 65534, 0700); `/tmp` (rw, noexec, nosuid, nodev, 256 MiB, 1777) | Narrowly scoped.  `/workspace` keeps exec so repositories' own scripts can run; the Python interpreter can execute arbitrary code regardless. |
| Network | `--network none` | No exception: dependencies must be baked into the image. |
| Namespaces | no `--privileged`, no host PID/IPC/UTS/user/network namespace; `--ipc private`, `--cgroupns private` | Verified after create.  An explicit private cgroup namespace also on cgroup v1 hosts, where Docker defaults to the host's. |
| Memory | `--memory` = `--memory-swap` = `CODEFORGE_DOCKER_MEMORY_MB` (default 2048) | No swap beyond the limit.  tmpfs pages count against it. |
| CPU | `--cpus CODEFORGE_DOCKER_CPUS` (default 1.0) | |
| PIDs | `--pids-limit CODEFORGE_DOCKER_PIDS_LIMIT` (default 256) | Fork bombs are bounded. |
| Other limits | `--ulimit core=0`, `--ulimit nofile=1024:1024` | |
| Time | `CODEFORGE_DOCKER_TIMEOUT_SECONDS` (default 300) wall clock | On expiry the worker force-removes the container.  The container also carries its own deadline (timeout + 60 s): an in-container supervisor kills every process and exits 124, so a container orphaned by a crashed worker stops instead of running forever. |
| Output | stdout/stderr read concurrently, each capped at 64 KiB in memory; `--log-driver none` | A noisy suite can neither exhaust worker memory nor fill the daemon's disk with logs. |
| PID 1 | `--init` | Reaps zombies; signals are delivered. |
| Environment | fixed: `HOME=/tmp`, `TMPDIR=/tmp`, `PYTHONPATH=/workspace`, `PYTHONDONTWRITEBYTECODE=1` | Nothing is inherited from the worker. |
| Image | `CODEFORGE_DOCKER_TEST_IMAGE` must be digest-pinned (`name@sha256:<64 hex>` or an image ID `sha256:<64 hex>`); `--pull never` | The image cannot change under a tag, and no registry is contacted during a job. |
| Docker CLI | runs with a minimal environment (`PATH`, `DOCKER_HOST`, `DOCKER_TLS_VERIFY`, `DOCKER_CERT_PATH`) and an empty private `DOCKER_CONFIG` | No worker secrets reach the CLI; client-side `config.json` proxy injection and credential helpers are not used. |

### Verification, not trust

Docker can silently discard some settings (for example a PIDs limit on a kernel
without the pids controller).  After `docker create` the executor inspects the
container and refuses to start it unless every control above is present
exactly as requested: privileged off, read-only rootfs, network `none`,
`CapDrop == ["ALL"]`, no `CapAdd`, `SecurityOpt == ["no-new-privileges"]`, no
host namespaces (private IPC and cgroup namespaces), exact memory/swap/CPU/PIDs values, the exact tmpfs set, no
binds/mounts/devices/volumes, `--init`, log driver `none`, the configured user,
image, entrypoint and command.  `docker info` must report seccomp support and
memory, swap, CPU-quota and PIDs limit support.  Any deviation, and any
warning printed by `docker create`, fails the run with
`ExecutionUnavailableError`, and the container is removed.

## 4. Lifecycle and ownership

```
verify archive ─► docker info ─► docker create ─► docker inspect (verify)
     ─► docker start --attach --interactive  (stream tar on stdin)
     ─► docker inspect (exit code, OOM) ─► docker rm --force      (always)
```

* **Removal always runs** — on success, test failure, timeout, executor error
  and cancellation — and is shielded from repeated cancellation.  The
  container name is generated per run, so cleanup only ever targets that run's
  container; cancelling one job cannot stop another job's container.
* **Failed removal:** if a run completed (tests passed or failed) but its
  container cannot be removed, the result is discarded and the run raises
  `ExecutionUnavailableError` naming the container: a container that may
  still exist is never reported as a completed test run.  If an exception was
  already propagating (cancellation, lost ownership, an executor error), that
  exception is preserved and the removal failure is logged with the container
  name.
* **Cancellation / lease loss:** the worker's `run_until_ownership_lost`
  cancels the orchestrator; the executor force-removes its container (which
  stops it), waits for the CLI processes, and re-raises.  The worker then
  raises `OwnershipLostError` and writes nothing.
* **Management commands** (`info`, `create`, `inspect`, `rm`) have a 30 s
  timeout.  If cancellation arrives while `docker create` is in flight, the
  executor lets that command finish (bounded) before removing the container,
  so a container is never created after its cleanup ran.
* **Timeout:** the container is force-removed and a failed `TestResult` with
  exit code -1 is returned.  If removal fails, the run raises instead of
  reporting a result, because the container may still be running.
* **Worker crash:** if the worker process dies (SIGKILL, OOM, host restart)
  no cleanup can run.  The container then stops itself at its own deadline
  (timeout + 60 s) but is not removed.
* **Leftovers:** if removal fails (the error is logged), or after a worker
  crash, containers can remain.  Operators can sweep with
  `docker ps -a --filter label=codeforge.managed=test-executor` and
  `docker rm --force`.  There is no automatic sweep, because one worker cannot
  tell another worker's live container from an orphan.

## 5. Fail-closed behaviour

* Docker CLI missing, daemon unreachable, image missing, a security option
  refused or downgraded, `docker create` warnings, a container that never
  started, a snapshot that cannot be extracted in the container (exit code 97)
  → `ExecutionUnavailableError`.  The testing stage fails the task; the
  coding agent's `run_tests` tool reports execution as unavailable.
* There is **no fallback** to `host_unsafe` or any other executor, and no
  setting that relaxes a control to make tests run.
* A missing or unpinned image is rejected when settings load; the factory also
  re-checks at run time.
* The Docker executor reports `IsolationLevel.CONTAINER`.  Automatic repairs
  still require `IsolationLevel.SANDBOX` and `sandbox` mode, so they stay
  disabled.

## 6. The executor image

`sandbox/docker/Dockerfile` builds the image from
`python:3.12-alpine` pinned by digest, installs pytest and its dependencies
from `sandbox/docker/requirements.txt` with `--require-hashes`
(binary wheels only), and sets `USER 65534:65534`.  It contains no repository
dependencies; suites that need third-party packages will fail with import
errors until a project-specific image is provided.

Build and configure:

```bash
docker build -t codeforge-test-runner:1 sandbox/docker
docker image inspect --format '{{.Id}}' codeforge-test-runner:1
# → sha256:…  set CODEFORGE_DOCKER_TEST_IMAGE to this value
```

Updating (security patches, Python or pytest upgrades):

1. Resolve the new base digest:
   `docker buildx imagetools inspect python:3.12-alpine` (or pull and read
   `RepoDigests`), and replace the digest in the `FROM` line.
2. Update the pinned versions in `requirements.txt` and regenerate the hashes
   (`pip download --only-binary=:all: --no-deps …` then `sha256sum`, or
   `pip-compile --generate-hashes`).
3. Rebuild, record the new image ID, run the Docker integration tests
   (`CODEFORGE_IT_DOCKER_IMAGE=<id> pytest apps/api/tests/security/test_docker_executor_integration.py`),
   then roll the new `CODEFORGE_DOCKER_TEST_IMAGE` out to workers.
4. Review at least monthly and whenever the base image publishes a security fix.

## 7. Deployment constraints

* The worker needs the `docker` CLI and access to a Docker daemon.  The
  current `docker-compose.yml` worker image has neither, so `docker` mode
  fails closed there until a deployment provides them.  This change does not
  mount the Docker socket into the Compose worker.
* Whoever can talk to the daemon is root-equivalent on the daemon's host.
  Giving the **worker** that access is a real privilege increase for the worker
  process (repository code never receives it).  Prefer a dedicated execution
  host, a remote daemon over TLS (`DOCKER_HOST`, `DOCKER_TLS_VERIFY`,
  `DOCKER_CERT_PATH`), rootless Docker, or `userns-remap`.  Docker contexts
  (`DOCKER_CONTEXT`) are not supported because the CLI runs with an empty
  `DOCKER_CONFIG`.
* The daemon must support cgroup memory, swap, CPU-quota and PIDs limits and
  seccomp; otherwise the executor refuses to run.

## 8. Acceptance criteria and tests

Unit tests (`tests/security/test_docker_executor.py`, no Docker required; a
fake `docker` CLI records every invocation):

* configuration: image pinning, limits, settings validation, factory mapping,
  `CONTAINER` isolation, repairs still refused;
* command construction: every required flag present, nothing forbidden
  (binds, socket, privileged, host namespaces, cap-add, network);
* post-create verification rejects each downgraded or added setting;
* lifecycle: removal on success, failure, timeout, cancellation and errors;
  bounded output; snapshot-only input; minimal CLI environment;
* lease loss of one job removes only that job's container while a concurrent
  job's container keeps running and its result is still delivered;
* unavailable Docker (missing CLI, unreachable daemon, refused options) raises
  `ExecutionUnavailableError` and never runs anything on the host.

Integration tests (`tests/security/test_docker_executor_integration.py`) run
only when `CODEFORGE_IT_DOCKER_IMAGE` names a built executor image and a daemon
is reachable; otherwise they are skipped and reported as skipped.  They check,
inside a real container: non-root, no capabilities, no-new-privileges,
seccomp, no network, read-only root, no Docker socket, no worker secrets, no
trusted-workspace path, no write-back, PIDs and memory limits, timeout,
cancellation and lease-loss cleanup.

## 9. Remaining limitations

* The in-container deadline is a backstop for orphaned containers, not a
  security control: repository code runs as the same user as the supervisor
  and can kill it.  The authoritative timeout is the worker's.
* Shared kernel: container escapes via kernel/runtime bugs are not mitigated
  beyond seccomp/AppArmor defaults (Stage 1D: `gvisor` mode).
* Do not add `--ulimit nproc` to docker mode: under runc it counts every
  process of the host UID across containers (see the gVisor document, §5).
* The worker (not repository code) needs daemon access, which is
  root-equivalent on the daemon host unless rootless/remote Docker is used.
* No user-namespace remapping is enforced by the executor; it depends on
  daemon configuration.
* Disk usage of the container's writable layer is not limited by Docker on
  overlayfs; the root filesystem is read-only and all writable paths are
  size-limited tmpfs, so this is bounded in practice.
* CPU time inside the wall-clock timeout, kernel memory and I/O bandwidth are
  bounded only by the CPU, memory and PIDs limits above.
* Tests that need network access or third-party dependencies do not work
  without a purpose-built image; there is deliberately no network exception.
* Repository code still controls its own exit status and output; results are
  evidence from untrusted code, not proof of correctness.
