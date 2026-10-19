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

## Run locally (runc-only subset)

```bash
CODEFORGE_IT_DOCKER_IMAGE=$(docker image inspect -f '{{.Id}}' codeforge-test-runner:dev) \
    pytest sandbox/gvisor/spike -rs
```

Without the runtimes registered, only the runc-based negative tests run and the
rest are reported as skipped.  In CI, `CODEFORGE_IT_GVISOR_REQUIRED=1` turns
every such skip into a failure.
