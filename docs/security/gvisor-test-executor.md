# gVisor test executor (Phase 10B.5 Stage 1D)

`CODEFORGE_TEST_EXECUTION_MODE=gvisor` runs a repository's test suite in the
Stage 1C hardened, throwaway container
([`docker-test-executor.md`](docker-test-executor.md)), under gVisor (`runsc`),
an application kernel in user space.  Repository code makes its system calls to
gVisor's Sentry, not to the host kernel.

This mode is **implemented and tested, not enabled**.  No deployment runs it,
the default mode is still `disabled`, and the Docker Compose worker cannot use
it (see [§9](#9-deployment-constraints)).  It reports `IsolationLevel.GVISOR`,
which deliberately does **not** permit automatic repairs: they still require
`SANDBOX`.

## 1. Architecture

* `app/execution/gvisor_runtime.py` holds pure checks and policy: the reviewed
  runsc profile, the `docker info` and `docker inspect` checks, the bootstrap
  guard, and the task-limit policy.
* `app/execution/gvisor_executor.py` holds the production code:
  * `GVisorExecutorConfig` subclasses the Stage 1C `DockerExecutorConfig`.  The
    runtime and the NPROC limit are required fields with no defaults, so the
    configuration cannot describe an ordinary runc container.
  * `GVisorTestExecutor` subclasses the Stage 1C `DockerTestExecutor`.
* Stage 1C gained only behaviour-preserving hooks, whose defaults reproduce
  Stage 1C exactly:
  * `config.ulimits()` and `config.runtime_args()`;
  * `_preflight` now returns the `docker info` description;
  * `_container_problems()` and `_bootstrap_failure()`.

  A test pins docker mode's `create` arguments to the list generated from the
  unchanged Stage 1C code.
* Everything else is inherited unchanged from Stage 1C: snapshot input on
  stdin, every container control and its verification, the lifecycle, bounded
  output, the timeout, cancellation and lease loss, and the cleanup rules.

```
verify archive ─► docker info: Stage 1C checks + runtime registration checks
   ─► docker create --runtime <name> --ulimit nproc=N:N  (+ every Stage 1C flag)
   ─► docker inspect: Stage 1C checks + Runtime, Annotations, exact NPROC ulimit
   ─► docker start: bootstrap guard checks the marker, THEN reads the snapshot
   ─► docker inspect (exit, OOM) ─► docker rm --force (always)
```

## 2. Runtime registration

Register runsc **by path** in `/etc/docker/daemon.json`, under a dedicated
name, with exactly these arguments:

```json
{
  "runtimes": {
    "codeforge-gvisor": {
      "path": "/usr/local/bin/runsc",
      "runtimeArgs": ["--network=none", "--oci-seccomp", "--directfs=false", "--gvisor-marker-file"]
    }
  }
}
```

| Argument | Why |
| --- | --- |
| `--network=none` | No network stack inside the sandbox, whatever Docker attaches. |
| `--oci-seccomp` | Applies Docker's seccomp profile inside the sandbox.  Without it, CI measured `Seccomp: 0` and `unshare(CLONE_NEWUSER)` was allowed. |
| `--directfs=false` | The Sentry reaches files only through the gofer.  CI measured no meaningful cost. |
| `--gvisor-marker-file` | Creates `/proc/gvisor/kernel_is_gvisor` for the bootstrap guard (corroboration only). |

* **Optional:** `--platform=systrap`, at most once.  systrap is runsc's
  default platform, which is what the real-runsc CI registration (no
  `--platform`) runs, so naming it explicitly selects the same configuration.
* **KVM is not supported.**  `--platform=kvm` is refused, as are `ptrace`,
  every other platform and more than one `--platform`.  This stays the case
  until dedicated real-runtime tests demonstrate KVM's behaviour, including the
  host-task budget that the NPROC policy (§5) depends on.
* **Refused:** anything else, including:
  * debugging and tracing flags (`--debug*`, `--strace*`, `--log*`,
    `--profile*`);
  * host access (`--host-uds`, `--host-fifo`, `--rootless`, `--nvproxy`,
    `--directfs=true`);
  * network weakening (`--network=host`/`sandbox`, `--net-raw`);
  * flag overrides and test-only flags (`--allow-flag-override`,
    `--TESTONLY-*`);
  * unvalidated platforms (`--platform=kvm`, `--platform=ptrace`);
  * `--ignore-cgroups`, unknown flags, repeated flags, and two-token forms
    such as `--network none`.
* **Not used:** `runsc install`, because it rewrites `daemon.json`.  Shim-type
  registrations (`runtimeType` / `options`) are refused, because their flags
  live in a file Docker does not report.

**Install and upgrade (pinned):**

1. Choose a release from <https://github.com/google/gvisor/releases>.  CI
   uses `20261005.0`.
2. Download `gvisor.tar.zstd` and `gvisor.tar.zstd.sha512` from
   `https://storage.googleapis.com/gvisor/releases/release/<release>/x86_64/`
   and `SHA512SUMS` from the GitHub release.
3. Require the SHA-512 to match the value pinned in your deployment, the
   bucket's `.sha512` and the `gvisor-x86_64.tar.zstd` line of `SHA512SUMS`.
   Then `sha512sum -c`.
4. Check the tarball members for absolute paths or `..`.  Install `runsc` and
   its `gvisor-bin/` sidecars, root-owned and not writable by others, side by
   side in `/usr/local/bin`.  runsc looks for `gvisor-bin/` next to itself.
5. Merge the runtime entry into `daemon.json` without touching other settings
   (see `sandbox/gvisor/spike/register_runtimes.py`).  Restart dockerd.
6. Run the real-runsc suites (`.github/workflows/gvisor-spike.yml`) against
   the new release.  Re-measure the host-task peak (§5) before rolling out.
   Review at least monthly and on every gVisor security release.

## 3. Settings

| Setting | Rule |
| --- | --- |
| `CODEFORGE_TEST_EXECUTION_MODE=gvisor` | Enables this executor. |
| `CODEFORGE_DOCKER_TEST_IMAGE` | Required.  Pinned by digest, as in docker mode, and never pulled. |
| `CODEFORGE_GVISOR_RUNTIME` | Required, with no default.  `^[a-z0-9][a-z0-9._-]{0,62}$`.  Never an ordinary runtime (`runc`, `crun`, `youki`, `io.containerd.runc.v1/v2`, …). |
| `CODEFORGE_GVISOR_RUNTIME_PATH` | Required, with no default.  Absolute and normalized.  Must equal the registered `path` exactly. |
| `CODEFORGE_GVISOR_NPROC_LIMIT` | Default 64, range 16–64.  Guest task limit. |
| `CODEFORGE_DOCKER_PIDS_LIMIT` | Default 256.  In gvisor mode it must be ≥ 256 (and ≥ 4 × NPROC). |
| CPU, memory, timeout, CLI | The existing `CODEFORGE_DOCKER_*` settings and bounds. |
| `CODEFORGE_MAX_REPAIR_ATTEMPTS` | Must stay 0.  Repairs require `sandbox` mode and `IsolationLevel.SANDBOX`. |

These settings are fail-closed in two places:
* **At settings load:** an invalid or incomplete gvisor configuration is
  rejected when settings load.
* **At the factory:** the factory rebuilds and re-validates the configuration
  at run time.  A mutated or bypassed `Settings` object yields an executor
  that always raises `ExecutionUnavailableError`, never a runc or host
  executor.

## 4. Runtime verification

Verification has three layers.  The host-side checks (1 and 2) carry the
weight; the in-sandbox marker (3) only corroborates them and is not proof on
its own.  `dmesg` and `/proc/version` are not used at all: gVisor documents
that they are easily replicated.

1. **Before anything is created (`docker info`).**
   * All Stage 1C daemon checks: seccomp, and memory, swap, CPU-quota and
     PIDs limit support.
   * The runtime is registered, by `path` and not by a shim type.
   * The `path` equals `CODEFORGE_GVISOR_RUNTIME_PATH`.
   * The arguments are exactly the reviewed profile.
   * The binary's OCI features report `dev.gvisor.*` and not
     `org.opencontainers.runc.*`.
2. **After `docker create`, before `docker start` (`docker inspect`).**
   * Every Stage 1C control is applied exactly (no mounts, no Docker socket,
     no host workspace path, fixed environment, and so on).
   * `HostConfig.Runtime` equals the configured name.  `--runtime` is always
     passed explicitly; the daemon's default runtime is never relied on.
   * `HostConfig.Annotations` is empty, which blocks `dev.gvisor.flag.*`
     overrides.
   * `Ulimits` is exactly `core=0:0`, `nofile=1024:1024` and
     `nproc=N:N`.

   On any difference the container is removed and never started.
3. **Inside the sandbox, before any repository byte exists (corroboration
   only).**
   * The trusted bootstrap first requires `/proc/gvisor/kernel_is_gvisor`.
   * Without it, the bootstrap exits 96 before reading the snapshot from
     stdin, and the run fails with "the workspace was not loaded".
   * Repository code could itself exit 96 later in a run.  That only makes
     its own run fail as unavailable, just as exit 97 does in Stage 1C.

**What these checks rely on.**
* Step 1 verifies the daemon's registration for the configured runtime name:
  its path, its exact arguments and the features its binary reports.
* Step 2 verifies only that the container records that name, with no
  annotations and the exact ulimits.  It does not re-verify what the name
  maps to.
* The marker (step 3) shows the bootstrap ran under a runsc started with
  `--gvisor-marker-file`.  It does not show which other arguments runsc was
  given.
* The registration check, `docker create`, the container check and
  `docker start` are separate calls, not one atomic operation.  Docker can
  reload runtime definitions without restarting, and resolves the runtime's
  binary and arguments when the container starts.
* So these checks assume the trusted Docker daemon configuration does not
  change between the registration check and the start.  If it did:
  * a runsc entry with weakened arguments that kept `--gvisor-marker-file`
    would not be detected;
  * repointing the name to runc would be caught only by the corroborating
    marker check.
* Changing the daemon configuration needs root-equivalent access to the
  daemon host, which is outside the threat model (repository code is the
  adversary).  The window still exists; nothing here closes it.

Every refusal raises `ExecutionUnavailableError`, and the testing stage fails
the task.  There is no fallback to runc, docker mode or the host.

## 5. Resource controls

**Host side:** the Stage 1C limits stay in force under gVisor.  What has been
verified, and how:
* **Measured by CI from runsc's host cgroup**, not taken from
  `docker inspect`: `--memory` = `--memory-swap`, `--cpus` and
  `--pids-limit`.
* **Wall-clock timeout and in-container deadline:** covered by lifecycle
  tests under real runsc (timeout, cancellation, lease loss, orphan
  deadline).  These are not cgroup controls and were not measured as such.
* **`core=0:0` and `nofile=1024:1024`:** Docker's `HostConfig`
  (`docker inspect`) reports them, and the executor refuses a container where
  they differ.  The real-runsc tests do not yet verify these two limits
  independently from inside the sandbox.
* **64 KiB stdout/stderr caps:** enforced by the worker as it reads the
  container's output.  Covered by the fake-Docker tests and the inherited
  Stage 1C tests; not measured as a gVisor cgroup control.

**Guest side:** `--ulimit nproc=N:N` sets `RLIMIT_NPROC` inside the sandbox.

Why both layers are needed (CI evidence, systrap, `release-20261005.0`):

* **Host limit alone:** `--pids-limit` counts runsc's *host* tasks, not the
  guest's.  Under a host limit of 64, the guest started 200 threads while the
  host peaked at 46.  A fork loop that reached the host limit killed the
  sandbox (exit 2, no output) instead of failing cleanly.  gVisor's
  compatibility guide agrees: limits are enforced only through the host cgroup
  containing the sandbox.
* **NPROC as the guest bound:** gVisor enforces `RLIMIT_NPROC` for callers
  without `CAP_SYS_ADMIN`/`CAP_SYS_RESOURCE` (`pkg/sentry/kernel/kernel.go`).
  With `nproc=64`:
  * The effective limit inside the sandbox is `(64, 64)` for UID 65534, with
    no capabilities.
  * Creation stops at exactly 64 live tasks (`64 − baseline`).
  * `fork()` gets `EAGAIN`.  Threads get `RuntimeError: can't start new
    thread`, and `fork()` gets `EAGAIN` at the same point.
  * Releasing one task admits exactly one more, and everything is cleaned up.
* **Per sandbox, not per UID:** two concurrent sandboxes with the same UID
  each reached exactly their own limit.
* **Never in docker mode:** under runc, Docker's `nproc` counts every process
  of the host UID across containers.  Locally, a second runc container could
  not even start (`exec /sbin/docker-init: resource temporarily unavailable`)
  while the first held 64 tasks.  The NPROC ulimit is therefore added only by
  `GVisorExecutorConfig`; docker mode's arguments are unchanged.

**Policy** (`task_limit_violations`, enforced at settings load and when the
configuration is built):

* NPROC is capped at the measured 64.
* The host `--pids-limit` must be at least `max(256, 4 × NPROC)`.  With NPROC
  64, the measured host peak was 169–170 of 256 (about 66 %); each guest
  process cost about 1.9 host tasks on top of roughly 45 for the sandbox.
* The floor is not assumed to hold elsewhere.  The real-runsc CI also
  requires the measured peak at the NPROC limit to stay within 80 % of the
  host limit.  Raising NPROC, supporting another platform (KVM is refused
  today) or upgrading runsc needs new measurements.

**Counting guest tasks:** gVisor's `/proc` directory listing can repeat an
entry.  CI saw PID 55 listed twice at 64 processes.  Count by unique PID.

## 6. Diagnosing failed runtime checks

| Error contains | Cause | Fix |
| --- | --- | --- |
| `runtime '<name>' is not registered` | Name not in `daemon.json`, or dockerd not restarted. | Register by path (§2) and restart dockerd. |
| `must be registered by path` | A shim-type registration (`runtimeType`). | Use `path` + `runtimeArgs`. |
| `path is '…', expected '…'` | The setting and the registration differ. | Make them identical (absolute, normalized). |
| `required runtime argument … is missing` / `is not permitted (…)` / `is repeated` | The registered arguments are not the reviewed profile. | Use exactly the profile; never add debug or host flags in production. |
| `does not report the runtime's OCI features` / `reports runc features` / `does not report gVisor features` | Old Docker, a runtime that cannot run `features` with its args, or not runsc. | Check `docker info --format '{{json .Runtimes}}'` and the binary. |
| `Runtime is '…', expected '…'` | The container was not created under the runtime. | Investigate immediately: this must not happen. |
| `Ulimits is …` | The daemon did not apply the exact NPROC ulimit. | Check daemon or default-ulimit configuration. |
| `marker was missing` | No `--gvisor-marker-file`, or the container is not gVisor. | Fix the registration.  If the host checks passed, treat this as a security incident. |
| `NPROC limit` / `too little headroom` | Settings outside the policy. | Use NPROC ≤ 64 and `CODEFORGE_DOCKER_PIDS_LIMIT` ≥ 256. |

## 7. Leftovers and cleanup

Each run removes its own container.  Cleanup follows Stage 1C:
* Removal is targeted at this run's container.
* If a completed run's container cannot be removed, its result is discarded
  and the run raises.
* A cancellation, lease loss or executor error stays the primary exception.

After a worker crash, the container stops itself at its own deadline but is
not removed.  Operators can sweep:

```bash
docker ps -a --filter label=codeforge.managed=test-executor \
  --format '{{.ID}} {{.Names}} {{.Status}} {{.RunningFor}}'
docker rm --force <id>   # only containers no live worker owns
# Sandbox processes that outlived their container (should be none):
pgrep -a -f 'runsc-(sandbox|gofer)|gvisor-bin' ; pgrep -a -f <container-id>
# Path-registered runtimes keep runsc state under the daemon's runtime root:
sudo runsc --root /var/run/docker/runtime-runc/moby list
sudo runsc --root /var/run/docker/runtime-runc/moby delete --force <container-id>
```

There is no automatic sweep: one worker cannot tell another worker's live
container from an orphan.

## 8. Evidence

The workflow `.github/workflows/gvisor-spike.yml` runs on a disposable
GitHub-hosted `ubuntu-24.04` runner, with a read-only token and no secrets, in
these cases:
* every pull request into `main`, with no path filter.  Its job is a required
  status check on `main`, and a path-filtered workflow that never starts would
  leave that check pending;
* pull requests into a Stage 1D branch (`feature/phase-10b5-stage1d-*`);
* pushes to `main` or a Stage 1D branch;
* manually (`workflow_dispatch`).

Only pull requests into `main` report the check name required on `main`,
`gVisor runtime feasibility (disposable runner)`.  Push runs report
`gVisor runtime feasibility (push validation)`, manual runs
`gVisor runtime feasibility (workflow_dispatch validation)`, and pull requests
into another branch `gVisor runtime feasibility (pull_request validation into
<base>)`.  Those runs test the branch head or a merge with another branch
rather than the merge with `main`, so they do not report the required PR
check.  `ci.yml` names its required check
(`Build, Lint, Type-check, Test, Migrate`) the same way.  This is a workflow
convention that a pull request can change
([`ci-trust-and-branch-governance.md`](ci-trust-and-branch-governance.md)).

Pushes trigger it only when they touch one of these:
* the gVisor executor or runtime checks;
* the Docker controls and lifecycle it inherits (`docker_executor.py`,
  `test_executor.py`, `ownership.py`, `output.py`);
* settings, the workspace snapshot, or the orchestrator's repair gate;
* the related security tests;
* `sandbox/docker/` or `sandbox/gvisor/`;
* dependencies or pytest configuration;
* the workflow itself.

Documentation-only pushes do not trigger it; documentation-only PRs do.
`ci.yml` runs the fake-Docker unit tests on every PR, but not the real-runsc
suites, which need a runner where runsc is installed.

The job installs the pinned, checksum-verified runsc and registers the reviewed
runtime plus deliberately misconfigured ones.  It then runs two suites:
* the 34-test feasibility spike;
* the production-executor suite
  (`sandbox/gvisor/spike/test_gvisor_executor_integration.py`, through the
  factory).

Any skip, failure or short test count fails the job.  So does any leftover
test container, or any gVisor host process still running 30 s after the
suites (`sandbox/gvisor/spike/check_leftovers.py`, matched by `argv[0]` and
executable path).  A positive control then starts one gVisor sandbox and
requires the same check to detect it, and to pass once it is removed.
Evidence is uploaded as the `gvisor-spike-evidence` artifact.

These checks are defined by the pull request under test, so they are the
author's evidence, not an independent verification.  See
[`ci-trust-and-branch-governance.md`](ci-trust-and-branch-governance.md).

Results of run 39218063813 (commit `13f0979`): fake-Docker suites 254 passed,
spike 34/34, production executor 28/28, nothing skipped.  The production
executor, built by the factory with NPROC 64 and host PIDs 256, showed:

| Check | Result |
| --- | --- |
| Container `HostConfig` | `Runtime` was `codeforge-gvisor` and `Ulimits` included `nproc 64/64`. |
| Forks | 61 created on a baseline of 3: 64 live, unique PIDs exactly the expected set.  `/proc` listed PID 55 twice.  The next `fork()` got `EAGAIN`.  Host peak 169/256 (66 %, 87 tasks of headroom). |
| Threads | 61 started.  `RuntimeError: can't start new thread`, and `fork()` got `EAGAIN`.  Host peak 46/256. |
| Two concurrent sandboxes | Each created exactly 61.  B's whole time at its limit fell inside the 26 s that A held its own. |
| Memory | 320 MiB limit: killed (exit 137), peak 292 MB. |
| CPU | 0.5 CPUs: busiest 3 s window measured 0.509. |
| Timeout, cancellation, lease loss | No leftovers. |
| Orphan deadline | Exited 124. |
| Refusals | All 13 registration refusals happened before any container was created.  A container created under runc was refused before it started.  With the marker missing, the run failed with "the workspace was not loaded" and the sentinel never ran. |

The feasibility spike's own results are in run 39212017166.

On the host, runsc's sandbox and stub processes run as UID 65534.  Under
two concurrent sandboxes, Docker's per-UID `nproc` did not count them across
sandboxes: sandbox A held 169 host tasks while B still created all 61 of its
tasks.  Behaviour with many concurrent sandboxes on one host is unmeasured
(§10).

## 9. Deployment constraints

* **Who can reach the daemon:** the worker needs a Docker daemon with runsc
  registered as above.  Whoever can reach that daemon is root-equivalent on
  its host.  That is the worker's privilege, never repository code's.
* **Recommended layout:** a dedicated execution host that runs nothing else,
  with rootful dockerd and runsc, reached by the worker over TLS
  (`DOCKER_HOST=tcp://…:2376`, `DOCKER_TLS_VERIFY`, `DOCKER_CERT_PATH`,
  firewalled to the workers).
* **Not supported:** do not mount the Docker socket into the Compose worker.
  Rootless Docker is not supported, because runsc there typically needs
  `--ignore-cgroups`, which is refused.
* **Compose worker:** the current Compose worker has no Docker CLI or daemon
  access, so `gvisor` mode fails closed there.  Enabling it needs an explicit,
  reviewed execution-daemon architecture.  Documentation alone does not enable
  it.
* **Rollout:** set the runtime, path and image explicitly; the defaults leave
  gvisor mode unusable.  Keep `CODEFORGE_MAX_REPAIR_ATTEMPTS=0`.

## 10. Residual risks and limitations

* **gVisor is not a VM.** A Sentry bug, or a host-kernel bug reachable
  through the Sentry's own seccomp-filtered syscalls, could still lead to
  escape.  The attack surface is much smaller than runc's, not zero.
* **Daemon access remains root-equivalent** for the worker.
* **Operator trust:** the marker and the runtime's self-reported features
  come from software the operator installed.  They catch misconfiguration,
  not a malicious runtime binary.  Binary integrity rests on the pinned
  checksum at install time.
* **Limited measurements:** one runner image, Docker 28, systrap and runsc
  `20261005.0`, at most two concurrent sandboxes.  KVM is therefore refused
  by the runtime checks; other versions and higher concurrency on one daemon
  are unmeasured.  Size the execution host's
  concurrency, and re-run the suites, before raising it.
* **Host-limit failures are not graceful:** if a guest ever hits the host
  limit before NPROC, the sandbox dies instead of returning `EAGAIN`.  The
  headroom policy and the CI check exist to prevent that.
* **OOM detection** relies on Docker's `OOMKilled` flag.  CI did not see the
  cgroup's `oom_kill` counter rise.
* **Stage 1C limitations still apply:** no network, no third-party
  dependencies beyond the image, and results are evidence from untrusted code.
  Some test suites may not run under gVisor; they fail, they never fall back.
