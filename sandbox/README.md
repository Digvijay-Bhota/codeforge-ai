# sandbox/

Execution environments for repository-controlled code (the test suite).

## `docker/` — Docker test executor image (Phase 10B.5 Stage 1C)

The image used by `CODEFORGE_TEST_EXECUTION_MODE=docker`
(`apps/api/app/execution/docker_executor.py`): Python 3.12 and a hash-pinned
pytest on a digest-pinned base image, running as 65534:65534.

```bash
docker build -t codeforge-test-runner:1 sandbox/docker
docker image inspect --format '{{.Id}}' codeforge-test-runner:1
# set CODEFORGE_DOCKER_TEST_IMAGE=sha256:<that id>
```

Design, threat model, controls, update procedure and remaining limitations:
[`docs/security/docker-test-executor.md`](../docs/security/docker-test-executor.md).

Docker is a container boundary on a shared kernel, not complete isolation.

## `gvisor/` — gVisor runtime evidence and tests (Phase 10B.5 Stage 1D)

`CODEFORGE_TEST_EXECUTION_MODE=gvisor`
(`apps/api/app/execution/gvisor_executor.py`) runs the same image and container
under a verified gVisor runtime.  `gvisor/spike/` holds the CI feasibility
spike, the real-runsc tests of the production executor and the runtime
registrations used by `.github/workflows/gvisor-spike.yml`.  Design, runtime
registration, settings, resource policy and operations:
[`docs/security/gvisor-test-executor.md`](../docs/security/gvisor-test-executor.md).
