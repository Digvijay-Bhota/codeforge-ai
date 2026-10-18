"""A stand-in for the ``docker`` CLI, for DockerTestExecutor unit tests.

Invoked as ``python -I fake_docker.py <state-dir> <docker args...>``.  Every
invocation is appended to ``<state-dir>/calls.jsonl`` with the environment it
received.  Containers are JSON files; ``inspect`` describes them from the flags
``create`` was given, so the executor's verification sees exactly what it
asked for unless ``scenario.json`` says otherwise.  Nothing is executed: what a
container "does" is chosen by a ``behaviour`` file in the snapshot it receives:

    pass | fail | hang | until_release | noisy:<bytes> | oom
"""

from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

STATE = Path(sys.argv[1])
ARGS = sys.argv[2:]

_VALUE_FLAGS = {
    "--name", "--label", "--pull", "--user", "--network", "--cap-drop", "--security-opt",
    "--ipc", "--cgroupns", "--log-driver", "--hostname", "--memory", "--memory-swap", "--cpus",
    "--pids-limit", "--workdir", "--ulimit", "--tmpfs", "--env", "--entrypoint",
}  # fmt: skip
_BOOL_FLAGS = {"--interactive", "--read-only", "--init", "--no-healthcheck"}
_IMAGE_ENV = ["PATH=/usr/local/bin:/usr/bin:/bin", "LANG=C.UTF-8"]


def scenario() -> dict[str, Any]:
    path = STATE / "scenario.json"
    return json.loads(path.read_text()) if path.exists() else {}


def record() -> None:
    with open(STATE / "calls.jsonl", "a") as fh:
        fh.write(json.dumps({"argv": ARGS, "env": dict(os.environ), "t": time.time()}) + "\n")


def container_path(name: str) -> Path:
    return STATE / "containers" / f"{name}.json"


def load(name: str) -> dict[str, Any] | None:
    path = container_path(name)
    return json.loads(path.read_text()) if path.exists() else None


def save(name: str, data: dict[str, Any]) -> None:
    path = container_path(name)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = value
    return base


def removed(name: str) -> bool:
    return (STATE / "removed" / name).exists()


def fail(message: str, code: int = 1) -> None:
    sys.stderr.write(message + "\n")
    sys.exit(code)


def cmd_info() -> None:
    sc = scenario()
    if sc.get("info") in ("unreachable", "unreachable_rc0"):
        print(json.dumps({"ServerErrors": ["Cannot connect to the Docker daemon"]}))
        if sc["info"] == "unreachable":
            fail("Cannot connect to the Docker daemon at unix:///var/run/docker.sock")
        return  # some CLI versions exit 0 here
    info = {
        "SecurityOptions": ["name=apparmor", "name=seccomp,profile=builtin", "name=cgroupns"],
        "MemoryLimit": True,
        "SwapLimit": True,
        "CpuCfsQuota": True,
        "PidsLimit": True,
    }
    print(json.dumps(merge(info, sc.get("info_override", {}))))


def cmd_create() -> None:
    sc = scenario()
    if delay := sc.get("create_delay"):
        time.sleep(delay)
    if sc.get("create") == "fail":
        fail("Error response from daemon: invalid option", 125)
    opts: dict[str, list[str]] = {}
    flags: set[str] = set()
    rest = ARGS[1:]
    i = 0
    while i < len(rest) and rest[i].startswith("-"):
        flag = rest[i]
        if flag in _BOOL_FLAGS:
            flags.add(flag)
            i += 1
        elif flag in _VALUE_FLAGS:
            opts.setdefault(flag, []).append(rest[i + 1])
            i += 2
        else:
            fail(f"unknown flag: {flag}", 125)
    image, cmd = rest[i], rest[i + 1 :]
    name = opts["--name"][0]

    def one(flag: str) -> str:
        return opts[flag][-1]

    def mib(value: str) -> int:
        assert value.endswith("m"), value
        return int(value[:-1]) * 1024 * 1024

    ulimits = []
    for spec in opts.get("--ulimit", []):
        key, limits = spec.split("=")
        soft, hard = limits.split(":")
        ulimits.append({"Name": key, "Soft": int(soft), "Hard": int(hard)})
    data = {
        "Name": "/" + name,
        "AppArmorProfile": "docker-default",
        "Mounts": [],
        "HostConfig": {
            "Binds": None,
            "Mounts": None,
            "Tmpfs": dict(spec.split(":", 1) for spec in opts.get("--tmpfs", [])),
            "Privileged": False,
            "ReadonlyRootfs": "--read-only" in flags,
            "NetworkMode": one("--network"),
            "IpcMode": one("--ipc"),
            "CgroupnsMode": one("--cgroupns"),
            "PidMode": "",
            "UTSMode": "",
            "UsernsMode": "",
            "CapAdd": None,
            "CapDrop": opts.get("--cap-drop"),
            "SecurityOpt": opts.get("--security-opt"),
            "Memory": mib(one("--memory")),
            "MemorySwap": mib(one("--memory-swap")),
            "NanoCpus": round(float(one("--cpus")) * 1_000_000_000),
            "PidsLimit": int(one("--pids-limit")),
            "Ulimits": ulimits,
            "Devices": [],
            "DeviceRequests": None,
            "VolumesFrom": None,
            "Links": None,
            "Sysctls": None,
            "Init": "--init" in flags,
            "LogConfig": {"Type": one("--log-driver"), "Config": {}},
            "OomKillDisable": False,
            "MaskedPaths": ["/proc/kcore", "/sys/firmware"],
            "ReadonlyPaths": ["/proc/sys", "/proc/sysrq-trigger"],
        },
        "Config": {
            "User": one("--user"),
            "Image": image,
            "Entrypoint": [one("--entrypoint")],
            "Cmd": cmd,
            "Tty": False,
            "Env": opts.get("--env", []) + _IMAGE_ENV,
            "Volumes": None,
            "Labels": dict(spec.split("=", 1) for spec in opts.get("--label", [])),
        },
        "State": {"Status": "created", "ExitCode": 0, "OOMKilled": False,
                  "StartedAt": "0001-01-01T00:00:00Z"},  # fmt: skip
    }
    save(name, merge(data, sc.get("tamper", {})))
    print("f" * 64)
    if sc.get("create") == "warn":
        sys.stderr.write("WARNING: Your kernel does not support pids limit capabilities\n")


def cmd_inspect() -> None:
    name = ARGS[-1]
    data = load(name)
    if data is None or removed(name):
        fail(f"Error: No such object: {name}")
    print(json.dumps(data))


def cmd_rm() -> None:
    name = ARGS[-1]
    if scenario().get("rm") == "fail":
        fail("Error response from daemon: removal of container failed")
    (STATE / "removed").mkdir(exist_ok=True)
    (STATE / "removed" / name).touch()
    container_path(name).unlink(missing_ok=True)


def finish(name: str, code: int, *, oom: bool = False) -> None:
    data = load(name)
    if data is not None and not removed(name):
        data["State"] = {"Status": "exited", "ExitCode": code, "OOMKilled": oom,
                         "StartedAt": "2026-10-18T00:00:00Z"}  # fmt: skip
        save(name, data)
    sys.stdout.flush()
    sys.exit(code)


def wait_until(predicate: Any) -> None:
    while not predicate():
        time.sleep(0.02)


def cmd_start() -> None:
    name = ARGS[-1]
    if load(name) is None:
        fail(f"Error response from daemon: No such container: {name}")
    archive = sys.stdin.buffer.read()
    (STATE / "started").mkdir(exist_ok=True)
    (STATE / "started" / name).touch()
    if scenario().get("start") == "never_started":
        fail("Error response from daemon: OCI runtime create failed")
    target = STATE / "received" / name
    target.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            tar.extractall(target, filter="data")
    except tarfile.TarError:
        finish(name, 97)
    behaviour_file = target / "behaviour"
    behaviour = behaviour_file.read_text().strip() if behaviour_file.exists() else "pass"

    if behaviour == "pass":
        print("1 passed")
        finish(name, 0)
    elif behaviour == "fail":
        print("1 failed")
        sys.stderr.write("boom\n")
        finish(name, 1)
    elif behaviour == "hang":
        wait_until(lambda: removed(name))
        sys.exit(137)
    elif behaviour == "until_release":
        wait_until(lambda: removed(name) or (STATE / "release").exists())
        if removed(name):
            sys.exit(137)
        print("1 passed")
        finish(name, 0)
    elif behaviour.startswith("noisy:"):
        size = int(behaviour.split(":")[1])
        sys.stdout.write("x" * size)
        sys.stderr.write("y" * size)
        finish(name, 0)
    elif behaviour == "oom":
        finish(name, 137, oom=True)
    else:
        fail(f"unknown behaviour {behaviour!r}")


def main() -> None:
    record()
    command = ARGS[0]
    handlers = {
        "info": cmd_info,
        "create": cmd_create,
        "inspect": cmd_inspect,
        "rm": cmd_rm,
        "start": cmd_start,
    }
    if command not in handlers:
        fail(f"fake docker: unsupported command {command!r}", 125)
    handlers[command]()


if __name__ == "__main__":
    main()
