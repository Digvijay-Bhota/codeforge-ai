# gVisor CI feasibility spike (Phase 10B.5 Stage 1D)

A bounded experiment, not the Stage 1D executor.  It answers: can a GitHub
Actions runner run repository tests under gVisor with Stage 1C's controls, can
the runtime be verified from the host, and are resource limits actually
enforced?  Nothing here changes `CODEFORGE_TEST_EXECUTION_MODE`, the executor
factory or the repair policy, and it never runs on a developer machine's daemon.

| File | Purpose |
| --- | --- |
| `../../../.github/workflows/gvisor-spike.yml` | Separate, disposable-runner job (no service containers, no secrets).  `ci.yml` is untouched. |
| `runtimes.json` | The reviewed `codeforge-gvisor` registration plus deliberately misconfigured ones used only as negative tests (never started, except where a test says so). |
| `register_runtimes.py` | Merges those into `/etc/docker/daemon.json` without touching other settings or existing runtimes; refuses conflicts; keeps a backup. |
| `test_gvisor_spike.py` | Real-runtime tests; evidence is appended as JSON lines to `$CODEFORGE_IT_GVISOR_EVIDENCE`. |
| `check_junit.py` | Fails the job if any spike test was skipped, failed or missing. |
| `../../../apps/api/app/execution/gvisor_runtime.py` | The checks under test (pure functions, not wired into any mode). |

## Install

The job installs the pinned release `20261005.0`.  It verifies
`gvisor-x86_64.tar.zstd` against a SHA-512 pinned in the workflow, which must
equal both the GitHub release's `SHA512SUMS` and the bucket's `.sha512`.  It then
installs `runsc` and its `gvisor-bin/` sidecars to `/usr/local/bin`, following
the official tarball procedure.  `runsc install` is not used, because it
rewrites `daemon.json`.

## Reviewed runtime profile

`codeforge-gvisor` → `/usr/local/bin/runsc` with exactly:

* `--network=none`: no network stack, whatever Docker attaches.
* `--oci-seccomp`: Docker's seccomp profile is otherwise *not* applied inside
  the sandbox.
* `--directfs=false`: the Sentry reaches files only through the gofer.
* `--gvisor-marker-file`: creates `/proc/gvisor/kernel_is_gvisor`.

Any other argument (debugging, host access, network, `--allow-flag-override`,
`TESTONLY-*`, unknown flags, `--flag value` forms) is refused.

## Runtime verification layers

1. **Host, `docker info`**: the runtime is registered by path, the path is the
   configured one, the arguments are exactly the profile, and the binary's OCI
   features are gVisor's, not runc's.
2. **Host, `docker inspect`**: the created container selected that runtime and
   carries no annotations.  Spike tests also confirm, from the host cgroup,
   that only `runsc` processes exist for the container (no `pytest` on the host).
3. **Corroboration only**: the trusted bootstrap requires the marker before it
   reads the snapshot from stdin, and exits 96 without extracting otherwise.
   `dmesg` and `/proc/version` are not used; gVisor documents that they are
   easily replicated.

## Task limits: `--pids-limit` is not enough under gVisor

Spike run 39205538725 (systrap, Docker 28.0.4) showed that the host
`--pids-limit` counts runsc's **host** tasks, not the tasks inside the guest
kernel.  Under `--pids-limit 64`, the guest started 500 threads while the host
cgroup peaked at 48.  A fork loop ran the host cgroup to 59 of 64 and the suite
then ended abnormally, with exit code 2 and no output.  gVisor's compatibility
guide says the same: resource limits are enforced only through the host cgroup
that contains the sandbox.

What the spike now asserts:

* `--pids-limit` stays as the bound on runsc's host tasks.  Both host-limit
  tests check that `pids.max` is applied and never exceeded, and that the
  sandbox is fully removed.  The threads test also *pins* the finding: the
  guest exceeds the host limit, so the host limit is not the guest bound.
* `--ulimit nproc=64:64` (`RLIMIT_NPROC`) is the bound on guest tasks.  gVisor
  enforces it for callers without `CAP_SYS_ADMIN`/`CAP_SYS_RESOURCE`.  For
  both `fork()` and threads, the tests check inside the sandbox:
  * the effective soft and hard limits are 64;
  * the identity is UID/GID 65534 with no effective capabilities, and every
    task belongs to it;
  * creation stops at exactly `64 - baseline` tasks, with `EAGAIN` for
    `fork()`, and `RuntimeError: can't start new thread` for threads plus
    `EAGAIN` from `fork()` at the same point;
  * releasing one task admits exactly one more;
  * everything is cleaned up.

  The host `pids.max` (default 256) must leave headroom.
* `nproc` is per sandbox under gVisor: two concurrent sandboxes with the same
  UID each reach their own limit.  Under **runc** the same setting is per host
  UID: locally, a second runc container could not even start
  (`exec /sbin/docker-init: resource temporarily unavailable`) while the first
  held 64 tasks.  So the `nproc` control is only safe with the gVisor runtime,
  never in plain `docker` mode.

## Run locally (runc-only subset)

```bash
CODEFORGE_IT_DOCKER_IMAGE=$(docker image inspect -f '{{.Id}}' codeforge-test-runner:dev) \
    pytest sandbox/gvisor/spike -rs
```

Without the runtimes registered, only the runc-based negative tests run and the
rest are reported as skipped.  In CI, `CODEFORGE_IT_GVISOR_REQUIRED=1` turns
every such skip into a failure.
