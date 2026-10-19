"""Unit tests of check_leftovers.py against a synthetic /proc (no gVisor needed).

Process shapes come from the spike's host-cgroup evidence (run 39243259613):
runsc's children appear as ``runsc-sandbox``/``runsc-gofer`` and
``/usr/local/bin/gvisor-bin/runsc-fd-parking``.  That the check also detects a
real sandbox is shown in CI, where it must find a live one (--expect-present).
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

import pytest
from check_leftovers import find_gvisor_processes, gvisor_reason, main

RUNSC = "/usr/local/bin/runsc"


def _proc(root: Path, pid: int, argv: list[str] | None, exe: str | None = None) -> Path:
    entry = root / str(pid)
    entry.mkdir()
    if argv is not None:
        (entry / "cmdline").write_bytes(
            b"\0".join(a.encode() for a in argv) + (b"\0" if argv else b"")
        )
    if exe is not None:
        os.symlink(exe, entry / "exe")
    return entry


@pytest.mark.parametrize(
    ("argv", "exe"),
    [
        (["runsc-sandbox", "--systemd-cgroup=true", "--directfs=false"], None),
        (["runsc-gofer", "--root=/var/run/docker/runtime-runc/moby"], None),
        (["/usr/local/bin/gvisor-bin/runsc-fd-parking"], None),
        (["runsc-fd-parking"], None),
        ([RUNSC, "--root=/var/run/docker/runtime-runc/moby", "state", "x"], None),
        (["exe", "boot"], RUNSC),  # re-executed through /proc/self/exe
        ([], "/usr/local/bin/gvisor-bin/gvisor_sentry"),
        (["gvisor_sentry"], None),
        (["anything"], RUNSC + " (deleted)"),  # binary replaced while running
    ],
)
def test_gvisor_processes_are_detected(tmp_path: Path, argv: list[str], exe: str | None) -> None:
    _proc(tmp_path, 4242, argv, exe)
    [found] = find_gvisor_processes(tmp_path, RUNSC)
    assert found.startswith("pid 4242: ")


@pytest.mark.parametrize(
    ("argv", "exe"),
    [
        ([], None),  # kernel thread: empty cmdline, unreadable exe
        (None, None),  # exited between listing and reading
        (
            ["python3", "-I", "sandbox/gvisor/spike/check_leftovers.py", "--runsc", RUNSC],
            "/usr/bin/python3.12",
        ),
        (["/usr/bin/bash", "-c", "pgrep -a runsc; pgrep -f gvisor-bin"], "/usr/bin/bash"),
        (["sudo", "python3", "-I", "check_leftovers.py", "--runsc", RUNSC], "/usr/bin/sudo"),
        (["/usr/bin/containerd-shim-runc-v2", "-namespace", "moby", "-id", "abc"], None),
        (["/usr/bin/dockerd", "-H", "fd://"], "/usr/bin/dockerd"),
        (["/usr/local/bin/python", "-m", "pytest"], "/usr/local/bin/python3.12"),
        (["/opt/runsc-notes/tool"], "/opt/runsc-notes/tool"),  # "runsc" only in a directory name
    ],
)
def test_unrelated_processes_are_ignored(
    tmp_path: Path, argv: list[str] | None, exe: str | None
) -> None:
    _proc(tmp_path, 7, argv, exe)
    (tmp_path / "self").mkdir()
    (tmp_path / "sys").mkdir()
    assert find_gvisor_processes(tmp_path, RUNSC) == []


def test_sidecar_directory_follows_the_configured_runsc() -> None:
    assert (
        gvisor_reason(None, "/opt/gvisor/gvisor-bin/gvisor_sentry", "/opt/gvisor/runsc") is not None
    )
    assert (
        gvisor_reason(None, "/usr/local/bin/gvisor-bin/gvisor_sentry", "/opt/gvisor/runsc") is None
    )
    assert gvisor_reason(None, "/usr/local/bin/gvisor-binary", RUNSC) is None


def test_clean_runner_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _proc(tmp_path, 1, ["/sbin/init"], "/usr/lib/systemd/systemd")
    assert main(["--runsc", RUNSC, "--timeout", "0", "--proc", str(tmp_path)]) == 0
    assert "no gVisor processes left" in capsys.readouterr().out


def test_leftover_fails_and_is_listed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _proc(tmp_path, 31, ["runsc-sandbox", "--root=/var/run/docker/runtime-runc/moby"])
    assert main(["--runsc", RUNSC, "--timeout", "0", "--proc", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "pid 31: argv[0] runsc-sandbox" in out and "FAILED" in out


def test_waits_for_a_sandbox_that_is_still_exiting(tmp_path: Path) -> None:
    entry = _proc(tmp_path, 31, ["runsc-gofer"])
    timer = threading.Timer(0.3, shutil.rmtree, [entry])
    timer.start()
    try:
        assert main(["--runsc", RUNSC, "--timeout", "10", "--proc", str(tmp_path)]) == 0
    finally:
        timer.join()


def test_expect_present_proves_detection(tmp_path: Path) -> None:
    assert (
        main(["--runsc", RUNSC, "--timeout", "0", "--expect-present", "--proc", str(tmp_path)]) == 1
    )
    _proc(tmp_path, 31, ["runsc-sandbox"])
    assert (
        main(["--runsc", RUNSC, "--timeout", "0", "--expect-present", "--proc", str(tmp_path)]) == 0
    )


@pytest.mark.parametrize("timeout", ["0", "2.5"])
def test_finite_non_negative_timeouts_are_accepted(tmp_path: Path, timeout: str) -> None:
    assert main(["--runsc", RUNSC, f"--timeout={timeout}", "--proc", str(tmp_path)]) == 0


@pytest.mark.parametrize("timeout", ["-1", "-0.5", "nan", "NaN", "inf", "+inf", "-inf", "1e999"])
def test_invalid_timeouts_are_rejected_before_scanning(
    tmp_path: Path,
    timeout: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def scan(*_: object) -> list[str]:
        raise AssertionError("scanned /proc despite an invalid --timeout")

    monkeypatch.setattr("check_leftovers.find_gvisor_processes", scan)
    with pytest.raises(SystemExit) as exc:
        main(["--runsc", RUNSC, f"--timeout={timeout}", "--proc", str(tmp_path)])
    assert exc.value.code == 2
    assert "argument --timeout: must be a finite, non-negative" in capsys.readouterr().err


@pytest.mark.parametrize("runsc", ["runsc", "usr/local/bin/runsc", "/usr/local/../bin/runsc"])
def test_runsc_path_must_be_absolute_and_normalized(runsc: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--runsc", runsc, "--timeout", "0"])
    assert exc.value.code == 2
