# Execution image for CODEFORGE_TEST_EXECUTION_MODE=docker.
#
# Contains Python and pytest only: no repository dependencies, no build tools,
# no credentials.  The base image is pinned by digest and Python packages by
# hash, so a rebuild is reproducible until the pins are deliberately updated.
# Configure workers with the resulting image ID (or a pushed name@sha256
# digest), never a tag.  Update procedure: docs/security/docker-test-executor.md.
#
#   docker build -t codeforge-test-runner:1 sandbox/docker
#   docker image inspect --format '{{.Id}}' codeforge-test-runner:1

# python:3.12-alpine (Python 3.12.15), resolved 2026-10-18.
FROM python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-compile --disable-pip-version-check \
        --require-hashes --only-binary=:all: -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

LABEL org.opencontainers.image.title="codeforge-test-runner" \
      org.opencontainers.image.description="CodeForge AI Docker test executor runtime"

# The executor also passes --user, --read-only and its own entrypoint and
# command; these are defaults for anyone running the image by hand.
USER 65534:65534
WORKDIR /workspace
