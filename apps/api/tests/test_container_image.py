"""The API image (also the worker's and dispatcher's) provides what the code runs.

The worker clones GitHub repositories by running ``git`` (app/github/git.py).
The image once installed only curl, so every GitHub task failed at repository
acquisition with "No such file or directory: 'git'".  These tests read the
Dockerfile and Compose file; CI additionally runs ``git --version`` inside the
built worker image (.github/workflows/ci.yml).
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from tests.test_compose_config import APP_SERVICES, COMPOSE, load

ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = ROOT / "apps" / "api" / "Dockerfile"


def _instructions() -> list[tuple[str, str]]:
    """(INSTRUCTION, arguments) pairs, with line continuations joined and comments dropped."""
    text = re.sub(r"\\\n", " ", DOCKERFILE.read_text())
    result = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            keyword, _, rest = line.partition(" ")
            result.append((keyword.upper(), rest.strip()))
    return result


def _apt_packages() -> set[str]:
    packages: set[str] = set()
    for keyword, args in _instructions():
        if keyword != "RUN":
            continue
        for command in re.split(r"&&|;", args):
            words = shlex.split(command)
            if words[:2] == ["apt-get", "install"]:
                packages.update(w for w in words[2:] if not w.startswith("-"))
    return packages


def test_the_image_installs_git_for_repository_cloning() -> None:
    assert "git" in _apt_packages()


def test_the_image_keeps_its_healthcheck_dependency() -> None:
    assert "curl" in _apt_packages()


def test_the_image_runs_as_the_non_root_user() -> None:
    users = [args for keyword, args in _instructions() if keyword == "USER"]
    assert users and users[-1] == "codeforge"


def test_every_app_service_builds_from_this_image() -> None:
    services = load(COMPOSE)["services"]
    for name in APP_SERVICES:
        build = services[name]["build"]
        assert (ROOT / build["context"] / build.get("dockerfile", "Dockerfile")).resolve() == DOCKERFILE
