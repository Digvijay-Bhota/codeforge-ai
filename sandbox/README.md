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
A stronger runtime (gVisor) is planned for Stage 1D.
