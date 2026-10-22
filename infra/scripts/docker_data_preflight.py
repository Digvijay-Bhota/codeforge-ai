#!/usr/bin/env python3
"""Refuse to touch Docker resources when Docker cannot see its real data.

If the Docker daemon (or containerd) starts before the filesystem holding its
data is mounted, it runs on the empty directory underneath the mount point:
every container, image and volume seems to have disappeared, although the
data is intact on the unmounted-at-start disk.  Running `docker compose up`
then would create new, empty volumes (a fresh database) in the wrong place.

This check changes nothing.  It reads the daemon's configuration, the daemon
itself, the mount table and systemd's start times, and exits non-zero with
the specific mismatch and a safe recovery procedure.  It prints no secrets
(only paths, unit names, timestamps and volume names).

    python3 -I docker_data_preflight.py --expect-mount /mnt/devdata \\
        --require-volume cf-gh-test_postgres_data

Exit status: 0 everything consistent, 1 a problem was found, 2 bad usage.
Standard library only, so it runs with the host's python3.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

DEFAULT_DOCKER_ROOT = "/var/lib/docker"
DEFAULT_CONTAINERD_ROOT = "/var/lib/containerd"

Runner = Callable[[Sequence[str]], tuple[int, str]]


def run(command: Sequence[str]) -> tuple[int, str]:
    """Run *command* without a shell; (exit status, stdout).  A missing program is status 127."""
    try:
        # Fixed argument lists, never a shell.
        result = subprocess.run(  # nosec B603
            list(command), capture_output=True, text=True, timeout=20, check=False
        )
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    return result.returncode, result.stdout.strip()


def configured_docker_root(daemon_json: str | None) -> str:
    """The data root the daemon is configured with (daemon.json "data-root"), or Docker's default."""
    if daemon_json:
        try:
            config = json.loads(daemon_json)
        except ValueError:
            config = {}
        if isinstance(config, dict):
            for key in ("data-root", "graph"):  # "graph" is the pre-17.05 name
                value = config.get(key)
                if isinstance(value, str) and value:
                    return value.rstrip("/") or "/"
    return DEFAULT_DOCKER_ROOT


def configured_containerd_root(config_toml: str | None) -> str:
    """containerd's top-level `root = "..."`, or its default."""
    for line in (config_toml or "").splitlines():
        if line.strip().startswith("["):
            break  # only top-level keys come before the first table
        match = re.match(r'\s*root\s*=\s*"([^"]+)"', line)
        if match:
            return match.group(1).rstrip("/") or "/"
    return DEFAULT_CONTAINERD_ROOT


def read_file(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


@dataclass
class Report:
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


class Preflight:
    def __init__(self, runner: Runner = run) -> None:
        self.run = runner

    def mount_containing(self, path: str) -> str | None:
        status, out = self.run(["findmnt", "-n", "-o", "TARGET", "--target", path])
        return out.splitlines()[0].strip() if status == 0 and out else None

    def is_mount_point(self, path: str) -> bool:
        status, out = self.run(["findmnt", "-n", "-o", "TARGET", path])
        return status == 0 and bool(out)

    def started_at(self, unit: str) -> int | None:
        """systemd's monotonic activation time of *unit* in microseconds, or None if unknown."""
        status, out = self.run(["systemctl", "show", unit, "-p", "ActiveEnterTimestampMonotonic", "--value"])
        if status != 0 or not out.strip().isdigit():
            return None
        value = int(out.strip())
        return value or None

    def mount_unit(self, target: str) -> str | None:
        status, out = self.run(["systemd-escape", "--path", "--suffix=mount", target])
        return out.strip() if status == 0 and out.strip() else None

    def check_start_order(self, service: str, data_path: str, report: Report) -> None:
        """*service* must have started after the mount that holds *data_path*."""
        target = self.mount_containing(data_path)
        if target is None:
            report.notes.append(f"{data_path}: mount not found; start order of {service} not checked")
            return
        if target == "/":
            return  # on the root filesystem: nothing can be mounted late
        unit = self.mount_unit(target)
        service_time = self.started_at(service)
        mount_time = self.started_at(unit) if unit else None
        if service_time is None or mount_time is None:
            report.notes.append(f"start order of {service} and {target} not checked (no systemd timestamps)")
            return
        if service_time < mount_time:
            report.problems.append(
                f"{service} started before {target} was mounted ({unit}; {service} at "
                f"{service_time / 1e6:.1f} s, mount at {mount_time / 1e6:.1f} s after boot). It is using the "
                f"directory underneath the mount point, not the data on {target}."
            )

    def check(
        self,
        *,
        daemon_json: str | None,
        containerd_toml: str | None,
        expect_mounts: Sequence[str] = (),
        require_volumes: Sequence[str] = (),
    ) -> Report:
        report = Report()
        expected_root = configured_docker_root(daemon_json)

        for path in expect_mounts:
            if not self.is_mount_point(path):
                report.problems.append(f"{path} is not mounted.")

        status, reported_root = self.run(["docker", "info", "--format", "{{.DockerRootDir}}"])
        if status != 0 or not reported_root:
            report.problems.append("The Docker daemon is not reachable (docker info failed).")
            return report
        if reported_root.rstrip("/") != expected_root:
            report.problems.append(
                f"Docker reports data root {reported_root}, but its configuration says {expected_root}."
            )

        self.check_start_order("docker.service", expected_root, report)
        # Docker 29's image store lives in containerd, so containerd must see its data too.
        self.check_start_order("containerd.service", configured_containerd_root(containerd_toml), report)

        for volume in require_volumes:
            status, _ = self.run(["docker", "volume", "inspect", "--format", "{{.Name}}", volume])
            if status != 0:
                report.problems.append(
                    f"Docker does not see the volume {volume}. If it existed before, Docker is probably "
                    "not looking at its real data; do not let anything create it again."
                )
        return report


RECOVERY = """\
Nothing was changed.  Do NOT prune, remove or recreate Docker volumes, and do not
run `docker compose up` or `down -v`: the data is most likely intact but invisible.

Read-only diagnosis:
{findmnt}
  systemctl show docker.service containerd.service -p ActiveEnterTimestamp
  docker info --format '{{{{.DockerRootDir}}}} containers={{{{.Containers}}}}'

Recovery (administrator, needs sudo), once the mount is present:
  sudo systemctl restart containerd docker
then run this check again before starting anything.

Lasting fix: docs/operations/docker-data-mount.md (make docker and containerd
wait for their mounts).
"""


def main(argv: Sequence[str] | None = None, runner: Runner = run, out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--expect-mount", action="append", default=[], metavar="PATH",
                        help="a path that must be a mount point (repeatable)")
    parser.add_argument("--require-volume", action="append", default=[], metavar="NAME",
                        help="a Docker volume that must already exist (repeatable)")
    parser.add_argument("--daemon-config", default="/etc/docker/daemon.json")
    parser.add_argument("--containerd-config", default="/etc/containerd/config.toml")
    args = parser.parse_args(argv)

    report = Preflight(runner).check(
        daemon_json=read_file(args.daemon_config),
        containerd_toml=read_file(args.containerd_config),
        expect_mounts=args.expect_mount,
        require_volumes=args.require_volume,
    )
    for note in report.notes:
        out(f"note: {note}")
    if report.ok:
        out("docker data preflight: OK")
        return 0
    out("docker data preflight: FAILED")
    for problem in report.problems:
        out(f"  - {problem}")
    out("")
    mounts = args.expect_mount or ["<mount point>"]
    out(RECOVERY.format(findmnt="\n".join(f"  findmnt {m}" for m in mounts)).rstrip())
    return 1


if __name__ == "__main__":
    sys.exit(main())
