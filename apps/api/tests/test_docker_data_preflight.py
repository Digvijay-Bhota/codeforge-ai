"""infra/scripts/docker_data_preflight.py: refuse to touch Docker when it cannot see its data.

After a reboot on the development host, docker.service and containerd.service
started a few seconds before /mnt/devdata (their data) was mounted, so Docker
reported no containers and no volumes while the data was intact.  A
`docker compose up` in that state would have created a new, empty database
volume.  These tests drive the check with a fake command runner modelled on
that host (the real outputs were: docker 19.5 s, containerd 13.8 s,
mnt-devdata.mount 22.3 s, var-lib-containerd.mount 22.4 s after boot).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("docker_data_preflight", ROOT / "infra/scripts/docker_data_preflight.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module here
    spec.loader.exec_module(module)
    return module


pf = _load()
DAEMON_JSON = json.dumps({"data-root": "/mnt/devdata/docker"})


class Host:
    """The commands the check runs, answered like the development host when healthy."""

    def __init__(self) -> None:
        self.mounted = {"/mnt/devdata", "/var/lib/containerd", "/"}
        self.containing = {"/mnt/devdata/docker": "/mnt/devdata", "/var/lib/containerd": "/var/lib/containerd"}
        self.started = {  # microseconds since boot
            "docker.service": 30_000_000,
            "containerd.service": 29_000_000,
            "mnt-devdata.mount": 22_312_455,
            "var-lib-containerd.mount": 22_386_256,
        }
        self.docker_root: str | None = "/mnt/devdata/docker"
        self.volumes = {"cf-gh-test_postgres_data"}
        self.systemd = True
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> tuple[int, str]:
        cmd = list(command)
        self.calls.append(cmd)
        match cmd:
            case ["findmnt", "-n", "-o", "TARGET", "--target", path]:
                target = self.containing.get(path, "/")
                return (0, target) if target in self.mounted else (0, "/")
            case ["findmnt", "-n", "-o", "TARGET", path]:
                return (0, path) if path in self.mounted else (1, "")
            case ["systemctl", "show", unit, "-p", "ActiveEnterTimestampMonotonic", "--value"]:
                if not self.systemd:
                    return 1, ""
                return 0, str(self.started.get(unit, 0))
            case ["systemd-escape", "--path", "--suffix=mount", target]:
                return 0, target.strip("/").replace("/", "-") + ".mount"
            case ["docker", "info", "--format", _]:
                return (0, self.docker_root) if self.docker_root else (1, "")
            case ["docker", "volume", "inspect", "--format", _, name]:
                return (0, name) if name in self.volumes else (1, "")
        raise AssertionError(f"unexpected command {cmd}")


def check(host: Host, **kwargs: object) -> object:
    args = {"daemon_json": DAEMON_JSON, "containerd_toml": None, "expect_mounts": ["/mnt/devdata"],
            "require_volumes": ["cf-gh-test_postgres_data"], **kwargs}  # fmt: skip
    return pf.Preflight(host).check(**args)


def test_a_healthy_host_passes() -> None:
    report = check(Host())
    assert report.ok and report.problems == []


def test_docker_started_before_its_data_mount_is_caught() -> None:
    host = Host()
    host.started["docker.service"] = 19_510_930  # the real boot
    host.volumes.clear()  # what Docker then shows
    report = check(host)
    assert not report.ok
    assert any("docker.service started before /mnt/devdata was mounted" in p for p in report.problems)
    assert any("does not see the volume cf-gh-test_postgres_data" in p for p in report.problems)


def test_containerd_started_before_its_bind_mount_is_caught() -> None:
    host = Host()
    host.started["containerd.service"] = 13_824_447
    report = check(host)
    assert [p for p in report.problems if p.startswith("containerd.service started before /var/lib/containerd")]


def test_the_root_string_alone_would_not_have_caught_the_real_failure() -> None:
    # On the real host Docker reported the configured root while using the
    # directory under the mount point; only the start order revealed it.
    host = Host()
    host.started["docker.service"] = 19_510_930
    report = check(host, require_volumes=[])
    assert host.docker_root == "/mnt/devdata/docker"
    assert len(report.problems) == 1 and "started before" in report.problems[0]


def test_a_missing_mount_is_caught() -> None:
    host = Host()
    host.mounted.discard("/mnt/devdata")
    report = check(host)
    assert "/mnt/devdata is not mounted." in report.problems


def test_a_different_data_root_is_caught() -> None:
    host = Host()
    host.docker_root = "/var/lib/docker"
    report = check(host)
    assert any("reports data root /var/lib/docker, but its configuration says /mnt/devdata/docker" in p
               for p in report.problems)  # fmt: skip


def test_an_unreachable_daemon_is_caught() -> None:
    host = Host()
    host.docker_root = None
    report = check(host)
    assert report.problems == ["The Docker daemon is not reachable (docker info failed)."]


def test_without_systemd_the_start_order_is_noted_not_failed() -> None:
    host = Host()
    host.systemd = False
    report = check(host)
    assert report.ok and any("not checked" in n for n in report.notes)


def test_data_on_the_root_filesystem_needs_no_start_order_check() -> None:
    host = Host()
    host.containing = {}
    host.docker_root = "/var/lib/docker"  # Docker's default, no daemon.json
    report = check(host, daemon_json=None, expect_mounts=[])
    assert report.ok
    assert not any(c[0] == "systemctl" for c in host.calls)


@pytest.mark.parametrize(
    ("text", "root"),
    [
        (None, "/var/lib/docker"),
        ("not json", "/var/lib/docker"),
        ('{"data-root": "/srv/docker/"}', "/srv/docker"),
        ('{"graph": "/old/docker"}', "/old/docker"),
    ],
)
def test_the_configured_root_is_read_from_daemon_json(text: str | None, root: str) -> None:
    assert pf.configured_docker_root(text) == root


def test_the_containerd_root_is_read_from_its_top_level_config() -> None:
    assert pf.configured_containerd_root(None) == "/var/lib/containerd"
    assert pf.configured_containerd_root('version = 2\nroot = "/data/containerd"\n[plugins]\nroot = "x"') == "/data/containerd"
    assert pf.configured_containerd_root('[plugins]\nroot = "/not/top/level"') == "/var/lib/containerd"


def test_main_exits_non_zero_with_a_safe_recovery_and_no_secrets(tmp_path: Path) -> None:
    daemon = tmp_path / "daemon.json"
    daemon.write_text(json.dumps({"data-root": "/mnt/devdata/docker", "registry-mirrors": ["https://user:pw@mirror"]}))
    host = Host()
    host.started["docker.service"] = 19_510_930
    lines: list[str] = []
    status = pf.main(["--expect-mount", "/mnt/devdata", "--daemon-config", str(daemon),
                      "--containerd-config", str(tmp_path / "absent.toml")], runner=host, out=lines.append)  # fmt: skip
    text = "\n".join(lines)
    assert status == 1
    assert "FAILED" in text and "Nothing was changed" in text
    assert "sudo systemctl restart containerd docker" in text
    assert "pw@mirror" not in text  # configuration contents are never printed
    assert pf.main(["--daemon-config", str(daemon)], runner=Host(), out=lines.append) == 0


FSTAB = """# /etc/fstab of the development host
UUID=8b7076ab-efe5-4d3a-af9f-d9a895530da5 /mnt/devdata ext4 defaults,nofail 0 2
/mnt/devdata/containerd /var/lib/containerd none bind,nofail 0 0
/swapfile none swap sw 0 0
"""


def test_an_unmounted_fstab_filesystem_is_caught_without_expect_mount() -> None:
    # Nothing tells the check which mount to expect, yet /etc/fstab says
    # Docker's data root lives on /mnt/devdata, which is not mounted: the data
    # root then resolves to "/", and `up` would create an empty database there.
    host = Host()
    host.mounted -= {"/mnt/devdata", "/var/lib/containerd"}
    report = check(host, expect_mounts=[], require_volumes=[], fstab=FSTAB)
    assert any("/mnt/devdata/docker belongs on /mnt/devdata (/etc/fstab), which is not mounted" in p
               for p in report.problems)  # fmt: skip
    assert any("/var/lib/containerd belongs on /var/lib/containerd (/etc/fstab)" in p for p in report.problems)


def test_fstab_mounts_that_are_mounted_pass() -> None:
    assert check(Host(), expect_mounts=[], require_volumes=[], fstab=FSTAB).ok


def test_fstab_parsing_ignores_comments_swap_and_unrelated_mounts() -> None:
    assert pf.fstab_mount_points(FSTAB) == ["/mnt/devdata", "/var/lib/containerd"]
    assert pf.fstab_mount_points(None) == []
