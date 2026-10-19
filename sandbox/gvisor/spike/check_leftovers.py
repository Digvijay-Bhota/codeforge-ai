"""Fail if any gVisor process is still running on the CI runner.

``sudo python3 -I check_leftovers.py --runsc /usr/local/bin/runsc``: waits up to
``--timeout`` seconds for every gVisor host process to exit and fails if any is
left.  ``--expect-present`` inverts the check; CI uses it on a live sandbox to
prove the detection is not vacuous.

A process counts as gVisor's if either of these holds:

* its ``argv[0]`` names runsc or one of its sidecars.  runsc renames its
  re-executed children (``runsc-sandbox``, ``runsc-gofer``), so ``pgrep runsc``,
  which matches the kernel's process name, can miss them;
* its executable (``/proc/PID/exe``) is the runsc binary or lies in the
  ``gvisor-bin/`` directory next to it.  Reading another user's ``exe`` needs
  root; without it only ``argv[0]`` is used.

Nothing else on the command line is matched, so this script, the shell that
runs it and tools whose arguments mention runsc are never reported.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

SIDECAR_NAMES = frozenset({"gvisor_sentry", "gvisor-sentry-prewarmer", "checkpointgofer"})


def _read_argv0(proc: Path) -> str | None:
    try:
        raw = (proc / "cmdline").read_bytes()
    except OSError:
        return None
    return raw.split(b"\0", 1)[0].decode(errors="replace") or None


def _read_exe(proc: Path) -> str | None:
    try:
        return os.readlink(proc / "exe")
    except OSError:
        return None


def gvisor_reason(argv0: str | None, exe: str | None, runsc: str) -> str | None:
    """Why a process with this ``argv[0]`` and executable is gVisor's (None: it is not)."""
    sidecars = os.path.join(os.path.dirname(runsc), "gvisor-bin") + "/"
    if exe is not None:
        exe = exe.removesuffix(" (deleted)")
        if exe == runsc or exe.startswith(sidecars):
            return f"executable {exe}"
    if argv0 is not None:
        name = os.path.basename(argv0)
        if (
            argv0 == runsc
            or argv0.startswith(sidecars)
            or name.startswith("runsc")
            or name in SIDECAR_NAMES
        ):
            return f"argv[0] {argv0}"
    return None


def find_gvisor_processes(proc_root: Path, runsc: str) -> list[str]:
    found = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        reason = gvisor_reason(_read_argv0(entry), _read_exe(entry), runsc)
        if reason is not None:
            found.append(f"pid {entry.name}: {reason}")
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--runsc", required=True, help="absolute path of the installed runsc binary"
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--expect-present", action="store_true")
    parser.add_argument("--proc", default="/proc", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not os.path.isabs(args.runsc) or os.path.normpath(args.runsc) != args.runsc:
        parser.error("--runsc must be an absolute, normalized path")

    deadline = time.monotonic() + args.timeout
    while True:
        found = find_gvisor_processes(Path(args.proc), args.runsc)
        if bool(found) == args.expect_present or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    for line in found:
        print(f"  {line}")
    if args.expect_present:
        if found:
            print(
                f"gVisor leftover check: detected {len(found)} live gVisor process(es), as expected"
            )
            return 0
        print(f"gVisor leftover check FAILED: no gVisor process detected within {args.timeout:g}s")
        return 1
    if found:
        print(
            f"gVisor leftover check FAILED: {len(found)} gVisor process(es) still running after {args.timeout:g}s"
        )
        return 1
    print("gVisor leftover check: no gVisor processes left")
    return 0


if __name__ == "__main__":
    sys.exit(main())
