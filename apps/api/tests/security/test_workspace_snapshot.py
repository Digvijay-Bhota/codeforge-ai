"""Phase 10B.5 Stage 1B — safe workspace snapshots.

Repository tests run against a snapshot of the workspace, never the
workspace itself.  These tests pin down the snapshot policy (what is archived,
excluded or rejected), determinism, safe extraction, the executor boundary
(no copy-back into the trusted workspace) and cleanup on every exit path.

A snapshot is a content boundary, not OS-level isolation: HostTestExecutor
still runs tests as the worker's UID.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import io
import json
import os
import socket
import stat
import tarfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import app.workspace.snapshot as snapshot_mod
from app.config import settings
from app.execution.host_executor import HostTestExecutor
from app.execution.test_executor import (
    ExecutionUnavailableError,
    IsolationLevel,
    UnavailableTestExecutor,
    run_tests_in_snapshot,
)
from app.orchestration.stages import TestingStageError, run_testing_stage
from app.schemas.task import TestResult
from app.services.task_service import _copy_isolated_workspace
from app.workspace.manager import WorkspaceManager
from app.workspace.snapshot import (
    SNAPSHOT_MTIME,
    ExclusionReason,
    SnapshotError,
    SnapshotLimitError,
    SnapshotLimits,
    SnapshotManifest,
    WorkspaceSnapshot,
    extract_snapshot,
    run_blocking,
    workspace_snapshot,
    write_snapshot,
)

CANARY = "codeforge-canary-7f3a91"


def _build(root: Path, limits: SnapshotLimits | None = None) -> tuple[bytes, SnapshotManifest]:
    out = io.BytesIO()
    manifest = write_snapshot(root, out, limits or SnapshotLimits())
    return out.getvalue(), manifest


def _members(data: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        return {m.name: m for m in tar.getmembers()}


def _reasons(manifest: SnapshotManifest) -> dict[str, ExclusionReason]:
    return {e.path: e.reason for e in manifest.excluded}


def _tree_digest(root: Path) -> str:
    """Digest of every path, type, mode and content under *root* (no symlink following)."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            p = Path(dirpath, name)
            st = p.lstat()
            h.update(f"{p.relative_to(root)}\0{st.st_mode}\0".encode())
            if stat.S_ISLNK(st.st_mode):
                h.update(os.readlink(p).encode())
            elif stat.S_ISREG(st.st_mode):
                h.update(p.read_bytes())
    return h.hexdigest()


@pytest.fixture(autouse=True)
def _development_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_env", "development")


# ── Content policy ─────────────────────────────────────────────────────────────


def test_regular_files_are_archived_with_normalised_metadata(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
    (tmp_path / "run.sh").write_text("#!/bin/sh\n")
    (tmp_path / "private.txt").write_text("p\n")
    (tmp_path / "suid.sh").write_text("#!/bin/sh\n")
    os.chmod(tmp_path / "run.sh", 0o700)
    os.chmod(tmp_path / "private.txt", 0o600)
    os.chmod(tmp_path / "suid.sh", 0o6755)

    data, manifest = _build(tmp_path)
    members = _members(data)

    assert sorted(members) == ["pkg/mod.py", "private.txt", "run.sh", "suid.sh"]
    assert members["run.sh"].mode == 0o755
    assert members["suid.sh"].mode == 0o755  # setuid/setgid stripped
    assert members["private.txt"].mode == 0o644
    for m in members.values():
        assert m.isreg()
        assert (m.uid, m.gid, m.uname, m.gname) == (0, 0, "", "")
        assert m.mtime == SNAPSHOT_MTIME
    entry = {e.path: e for e in manifest.entries}["pkg/mod.py"]
    assert entry.sha256 == hashlib.sha256(b"x = 1\n").hexdigest()
    assert manifest.total_bytes == sum(e.size for e in manifest.entries)


def test_git_metadata_is_excluded_at_any_depth_and_case(tmp_path: Path) -> None:
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    (tmp_path / ".git" / "config").write_text(f"[core]\n\tfsmonitor = {CANARY}\n")
    (tmp_path / ".git" / "hooks" / "pre-commit").write_text(CANARY)
    (tmp_path / "vendor" / "lib" / ".git").mkdir(parents=True)
    (tmp_path / "vendor" / "lib" / ".git" / "HEAD").write_text(CANARY)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".git").write_text(f"gitdir: /{CANARY}\n")  # worktree pointer file
    (tmp_path / "Upper" / ".GIT").mkdir(parents=True)
    (tmp_path / "Upper" / ".GIT" / "config").write_text(CANARY)
    (tmp_path / "keep.py").write_text("ok\n")

    data, manifest = _build(tmp_path)

    assert list(_members(data)) == ["keep.py"]
    assert CANARY.encode() not in data
    assert _reasons(manifest) == {
        ".git": ExclusionReason.GIT_METADATA,
        "Upper/.GIT": ExclusionReason.GIT_METADATA,
        "sub/.git": ExclusionReason.GIT_METADATA,
        "vendor/lib/.git": ExclusionReason.GIT_METADATA,
    }


def test_secrets_caches_and_bytecode_are_excluded(tmp_path: Path) -> None:
    for name in (".env", ".env.local", "id_rsa", "id_ed25519.pub", "server.pem", "tls.key", "api.token"):
        (tmp_path / name).write_text(CANARY)
    (tmp_path / "credentials").mkdir()
    (tmp_path / "credentials" / "aws").write_text(CANARY)
    (tmp_path / "secrets").write_text(CANARY)
    for cache in ("__pycache__", "node_modules", ".venv", ".pytest_cache"):
        (tmp_path / cache).mkdir()
        (tmp_path / cache / "blob").write_text(CANARY)
    (tmp_path / "mod.pyc").write_text(CANARY)
    (tmp_path / "app.py").write_text("ok\n")

    data, manifest = _build(tmp_path)

    assert list(_members(data)) == ["app.py"]
    assert CANARY.encode() not in data
    reasons = _reasons(manifest)
    assert reasons[".env"] is ExclusionReason.SECRET
    assert reasons["credentials"] is ExclusionReason.SECRET
    assert reasons["server.pem"] is ExclusionReason.SECRET
    assert reasons["node_modules"] is ExclusionReason.IGNORED_DIRECTORY
    assert reasons["mod.pyc"] is ExclusionReason.BYTECODE
    assert len(reasons) == 14


def test_symlink_policy(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(CANARY)
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "a.py").write_text("a\n")
    (ws / ".env").write_text(CANARY)
    (ws / "sub" / "ok").symlink_to("../a.py")
    (ws / "same_dir").symlink_to("a.py")
    (ws / "abs_passwd").symlink_to("/etc/passwd")
    (ws / "abs_inside").symlink_to(ws / "a.py")
    (ws / "up").symlink_to("..")
    (ws / "escape").symlink_to("../outside/secret.txt")
    (ws / "dotdot_mid").symlink_to("sub/../a.py")
    (ws / "dir_link").symlink_to("sub")
    (ws / "dangling").symlink_to("missing.py")
    (ws / "chain").symlink_to("same_dir")
    (ws / "to_secret").symlink_to(".env")
    (ws / "id_rsa").symlink_to("a.py")

    data, manifest = _build(ws)
    members = _members(data)

    assert sorted(members) == ["a.py", "same_dir", "sub/ok"]
    assert members["sub/ok"].issym() and members["sub/ok"].linkname == "../a.py"
    assert members["same_dir"].issym() and members["same_dir"].linkname == "a.py"
    assert CANARY.encode() not in data
    assert _reasons(manifest) == {
        ".env": ExclusionReason.SECRET,
        "abs_inside": ExclusionReason.SYMLINK_ABSOLUTE,
        "abs_passwd": ExclusionReason.SYMLINK_ABSOLUTE,
        "chain": ExclusionReason.SYMLINK_TARGET,
        "dangling": ExclusionReason.SYMLINK_TARGET,
        "dir_link": ExclusionReason.SYMLINK_TARGET,
        "dotdot_mid": ExclusionReason.SYMLINK_ESCAPE,
        "escape": ExclusionReason.SYMLINK_ESCAPE,
        "id_rsa": ExclusionReason.SECRET,
        "to_secret": ExclusionReason.SYMLINK_TARGET,
        "up": ExclusionReason.SYMLINK_ESCAPE,
    }


def test_symlinked_directory_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(CANARY)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "data").symlink_to(outside, target_is_directory=True)

    data, manifest = _build(ws)

    assert _members(data) == {}
    assert CANARY.encode() not in data
    assert _reasons(manifest) == {"data": ExclusionReason.SYMLINK_ABSOLUTE}


def test_special_files_are_excluded(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "fifo")
    sock_path = tmp_path / "sock"
    if len(str(sock_path)) >= 100:
        pytest.skip("socket path too long for AF_UNIX")
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(sock_path))
    try:
        (tmp_path / "a.py").write_text("a\n")
        # Devices cannot be created unprivileged; S_ISREG is the gate for them too.
        data, manifest = _build(tmp_path)
    finally:
        sock.close()

    assert list(_members(data)) == ["a.py"]
    assert _reasons(manifest) == {
        "fifo": ExclusionReason.SPECIAL_FILE,
        "sock": ExclusionReason.SPECIAL_FILE,
    }


def test_hardlinks_are_excluded(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text(CANARY)
    ws = tmp_path / "ws"
    ws.mkdir()
    os.link(outside, ws / "innocent.txt")  # same inode as a file outside the workspace
    (ws / "a.py").write_text("a\n")
    os.link(ws / "a.py", ws / "b.py")

    data, manifest = _build(ws)

    assert _members(data) == {}
    assert CANARY.encode() not in data
    assert _reasons(manifest) == {
        "a.py": ExclusionReason.HARDLINK,
        "b.py": ExclusionReason.HARDLINK,
        "innocent.txt": ExclusionReason.HARDLINK,
    }


def test_unsafe_names_are_excluded_and_rendered_printably(tmp_path: Path) -> None:
    (tmp_path / "bad\nname.py").write_text("x\n")
    (tmp_path / "esc\x1b[31m.py").write_text("x\n")
    with open(os.fsencode(tmp_path) + b"/latin\xff.py", "wb") as fh:
        fh.write(b"x\n")
    (tmp_path / "ok_ünïcode.py").write_text("x\n")

    data, manifest = _build(tmp_path)

    assert list(_members(data)) == ["ok_ünïcode.py"]
    reasons = _reasons(manifest)
    assert set(reasons.values()) == {ExclusionReason.UNSAFE_NAME}
    assert set(reasons) == {"bad\\nname.py", "esc\\x1b[31m.py", "latin\\xff.py"}
    assert all(name.isprintable() for name in reasons)


# ── Limits ─────────────────────────────────────────────────────────────────────


def test_too_many_files_fails_closed(tmp_path: Path) -> None:
    for i in range(4):
        (tmp_path / f"f{i}.py").write_text("x\n")
    with pytest.raises(SnapshotLimitError, match="more than 3 files"):
        _build(tmp_path, SnapshotLimits(max_files=3))
    _build(tmp_path, SnapshotLimits(max_files=4))


def test_total_size_limit_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "a.bin").write_bytes(b"a" * 600)
    (tmp_path / "b.bin").write_bytes(b"b" * 600)
    with pytest.raises(SnapshotLimitError, match="exceeds 1000 bytes"):
        _build(tmp_path, SnapshotLimits(max_total_bytes=1000))


def test_scanned_entry_limit_fails_closed_even_for_excluded_trees(tmp_path: Path) -> None:
    (tmp_path / "node_modules").mkdir()
    for i in range(5):
        (tmp_path / f".env{i}").write_text("x")
    with pytest.raises(SnapshotLimitError, match="more than 4 entries"):
        _build(tmp_path, SnapshotLimits(max_scanned_entries=4))


def test_oversized_file_is_excluded(tmp_path: Path) -> None:
    (tmp_path / "big.bin").write_bytes(b"x" * 101)
    (tmp_path / "small.bin").write_bytes(b"x" * 100)
    data, manifest = _build(tmp_path, SnapshotLimits(max_file_bytes=100))
    assert list(_members(data)) == ["small.bin"]
    assert _reasons(manifest) == {"big.bin": ExclusionReason.TOO_LARGE}


def test_overly_deep_directories_are_excluded(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "x.py").write_text("x\n")
    (tmp_path / "a" / "y.py").write_text("y\n")
    data, manifest = _build(tmp_path, SnapshotLimits(max_depth=2))
    assert list(_members(data)) == ["a/y.py"]
    assert _reasons(manifest) == {"a/b/c": ExclusionReason.TOO_DEEP}


# ── Determinism ────────────────────────────────────────────────────────────────


def _populate(root: Path, order: list[str], mode: int) -> None:
    contents = {"z.py": "z\n", "a/b.py": "b\n", "a/c/d.txt": "d\n", "m.sh": "#!/bin/sh\n"}
    for rel in order:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents[rel])
        os.chmod(path, 0o755 if rel.endswith(".sh") else mode)
    (root / "link").symlink_to("z.py")


def test_snapshots_are_byte_identical_across_builds_and_trees(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    _populate(one, ["z.py", "a/b.py", "a/c/d.txt", "m.sh"], 0o644)
    _populate(two, ["a/c/d.txt", "m.sh", "a/b.py", "z.py"], 0o664)
    os.utime(two / "z.py", (1, 1))
    os.utime(two / "a" / "b.py", (2_000_000_000, 2_000_000_000))

    first, manifest = _build(one)
    again, _ = _build(one)
    other, other_manifest = _build(two)

    assert first == again
    assert first == other
    assert manifest == other_manifest


@pytest.mark.asyncio
async def test_workspace_snapshot_digest_matches_archive_and_is_stable(tmp_path: Path) -> None:
    _populate(tmp_path, ["z.py", "a/b.py", "a/c/d.txt", "m.sh"], 0o644)
    async with workspace_snapshot(tmp_path) as first:
        data = first.archive_path.read_bytes()
        assert hashlib.sha256(data).hexdigest() == first.sha256
        assert stat.S_IMODE(first.archive_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(first.archive_path.parent.stat().st_mode) == 0o700
    async with workspace_snapshot(tmp_path) as second:
        assert second.sha256 == first.sha256


# ── Workspace changes while building ───────────────────────────────────────────


def _swap_after_scan(monkeypatch: pytest.MonkeyPatch, swap: Any) -> None:
    original = snapshot_mod._Scan.walk

    def walk(self: Any, dir_fd: int, prefix: tuple[str, ...]) -> None:
        original(self, dir_fd, prefix)
        if prefix == ():
            swap()

    monkeypatch.setattr(snapshot_mod._Scan, "walk", walk)


def test_file_swapped_for_symlink_after_scan_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text(CANARY)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("a" * len(CANARY))

    def swap() -> None:
        (ws / "a.py").unlink()
        (ws / "a.py").symlink_to(secret)

    _swap_after_scan(monkeypatch, swap)
    out = io.BytesIO()
    with pytest.raises(SnapshotError):
        write_snapshot(ws, out)
    assert CANARY.encode() not in out.getvalue()


def test_directory_swapped_for_symlink_after_scan_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.py").write_text(CANARY)
    ws = tmp_path / "ws"
    (ws / "pkg").mkdir(parents=True)
    (ws / "pkg" / "a.py").write_text("a" * len(CANARY))

    def swap() -> None:
        (ws / "pkg").rename(tmp_path / "moved")
        (ws / "pkg").symlink_to(outside, target_is_directory=True)

    _swap_after_scan(monkeypatch, swap)
    out = io.BytesIO()
    with pytest.raises(SnapshotError):
        write_snapshot(ws, out)
    assert CANARY.encode() not in out.getvalue()


def test_file_that_grows_after_scan_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.py").write_text("a\n")

    def swap() -> None:
        (tmp_path / "a.py").write_text("a much longer replacement\n")

    _swap_after_scan(monkeypatch, swap)
    with pytest.raises(SnapshotError, match="changed during snapshot"):
        write_snapshot(tmp_path, io.BytesIO())


def test_unreadable_entries_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # chmod 000 would not stop a root test runner, so deny the open directly.
    (tmp_path / "a.py").write_text("a\n")
    real_open = os.open

    def deny(path: Any, *args: Any, **kwargs: Any) -> int:
        if path == "a.py":
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(snapshot_mod.os, "open", deny)
    with pytest.raises(SnapshotError, match="Cannot read a.py: Permission denied"):
        write_snapshot(tmp_path, io.BytesIO())


# ── Extraction ─────────────────────────────────────────────────────────────────


def test_round_trip_extracts_only_inside_the_target(tmp_path: Path) -> None:
    ws, target = tmp_path / "ws", tmp_path / "target"
    target.mkdir()
    _populate(ws, ["z.py", "a/b.py", "a/c/d.txt", "m.sh"], 0o644)
    data, _ = _build(ws)
    before = set(tmp_path.iterdir())

    extract_snapshot(io.BytesIO(data), target)

    assert set(tmp_path.iterdir()) == before
    assert (target / "a" / "c" / "d.txt").read_text() == "d\n"
    assert os.readlink(target / "link") == "z.py"
    assert stat.S_IMODE((target / "m.sh").stat().st_mode) == 0o755
    assert int((target / "z.py").stat().st_mtime) == SNAPSHOT_MTIME
    rebuilt, _ = _build(target)
    assert rebuilt == data


def _craft(*members: tuple[tarfile.TarInfo, bytes | None]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for info, payload in members:
            if payload is not None:
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            else:
                tar.addfile(info)
    return out.getvalue()


def _file(name: str, payload: bytes = b"x", mode: int = 0o644) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.mode = mode
    return info, payload


def _link(name: str, target: str, kind: bytes = tarfile.SYMTYPE) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info, None


def _special(name: str, kind: bytes) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    return info, None


@pytest.mark.parametrize(
    "members",
    [
        pytest.param([_file("/abs.py")], id="absolute-name"),
        pytest.param([_file("../evil.py")], id="parent-name"),
        pytest.param([_file("a/../../evil.py")], id="traversal-name"),
        pytest.param([_file("a//b.py")], id="empty-component"),
        pytest.param([_file("./a.py")], id="dot-component"),
        pytest.param([_file("bad\nname.py")], id="control-character"),
        pytest.param([_file(".git/config")], id="git-config"),
        pytest.param([_file("sub/.GIT/hooks/pre-commit")], id="nested-git"),
        pytest.param([_file("a.py"), _file("a.py")], id="duplicate"),
        pytest.param([_file("run.sh", mode=0o4755)], id="setuid"),
        pytest.param([_file("a.py"), _link("l", "/etc/passwd")], id="absolute-symlink"),
        pytest.param([_file("a.py"), _link("l", "../outside")], id="escaping-symlink"),
        pytest.param([_file("a.py"), _link("l", "missing.py")], id="dangling-symlink"),
        pytest.param([_file("d/a.py"), _link("l", "d")], id="directory-symlink"),
        pytest.param([_file("a.py"), _link("l", "a.py"), _file("l/x.py")], id="write-through-symlink"),
        pytest.param([_file("a.py"), _file("a.py/x.py")], id="nested-under-file"),
        pytest.param([_file("a.py"), _link("h", "a.py", tarfile.LNKTYPE)], id="hardlink"),
        pytest.param([_special("dev", tarfile.CHRTYPE)], id="char-device"),
        pytest.param([_special("blk", tarfile.BLKTYPE)], id="block-device"),
        pytest.param([_special("fifo", tarfile.FIFOTYPE)], id="fifo"),
        pytest.param([_special("dir", tarfile.DIRTYPE)], id="directory"),
    ],
)
def test_unsafe_archives_are_rejected_before_anything_is_written(
    tmp_path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]]
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    before = set(tmp_path.iterdir())

    with pytest.raises(SnapshotError):
        extract_snapshot(io.BytesIO(_craft(*members)), target)

    assert list(target.iterdir()) == []
    assert set(tmp_path.iterdir()) == before


def test_archive_limits_are_enforced_on_extraction(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    big = _craft(_file("a.bin", b"x" * 200))
    with pytest.raises(SnapshotLimitError):
        extract_snapshot(io.BytesIO(big), target, SnapshotLimits(max_file_bytes=100))
    many = _craft(_file("a"), _file("b"), _file("c"))
    with pytest.raises(SnapshotLimitError):
        extract_snapshot(io.BytesIO(many), target, SnapshotLimits(max_files=2))
    total = _craft(_file("a", b"x" * 60), _file("b", b"x" * 60))
    with pytest.raises(SnapshotLimitError):
        extract_snapshot(io.BytesIO(total), target, SnapshotLimits(max_total_bytes=100))
    assert list(target.iterdir()) == []


def test_extraction_target_must_be_an_empty_real_directory(tmp_path: Path) -> None:
    data = _craft(_file("a.py"))
    full = tmp_path / "full"
    full.mkdir()
    (full / "existing").write_text("x")
    with pytest.raises(SnapshotError, match="not empty"):
        extract_snapshot(io.BytesIO(data), full)
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    with pytest.raises(SnapshotError, match="not a directory"):
        extract_snapshot(io.BytesIO(data), tmp_path / "alias")
    assert list(real.iterdir()) == []


# ── Executor boundary: no copy-back into the trusted workspace ─────────────────


_HOSTILE_SUITE = """
import os, pathlib, json
def test_tamper():
    cwd = pathlib.Path.cwd()
    seen = {
        "cwd": str(cwd),
        "git_visible": (cwd / ".git").exists(),
        "env_visible": (cwd / ".env").exists(),
        "canary_in_tree": any(
            CANARY in p.read_text(errors="ignore") for p in cwd.rglob("*") if p.is_file()
        ),
    }
    pathlib.Path(REPORT).write_text(json.dumps(seen))
    (cwd / "app.py").write_text("tampered = True\\n")
    (cwd / "new_file.py").write_text("planted\\n")
    (cwd / "victim.py").unlink()
    (cwd / ".git" / "hooks").mkdir(parents=True)
    (cwd / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\\nexit 1\\n")
    (cwd / ".git" / "config").write_text("[core]\\n\\thooksPath = /tmp\\n")
    (cwd / ".github" / "workflows").mkdir(parents=True)
    (cwd / ".github" / "workflows" / "x.yml").write_text("on: push\\n")
    os.symlink("/etc/passwd", cwd / "planted_link")
"""


def _trusted_workspace(root: Path) -> None:
    (root / ".git" / "hooks").mkdir(parents=True)
    (root / ".git" / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
    (root / ".git" / "HEAD").write_text("ref: refs/heads/codeforge/task-1\n")
    (root / ".env").write_text(f"GITHUB_TOKEN={CANARY}\n")
    (root / "app.py").write_text("tampered = False\n")
    (root / "victim.py").write_text("keep me\n")


@pytest.mark.asyncio
async def test_host_executor_changes_never_reach_the_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _trusted_workspace(ws)
    report = tmp_path / "report.json"
    # The canary is split so the test file itself does not contain it.
    (ws / "test_hostile.py").write_text(
        f"CANARY = {CANARY[:9]!r} + {CANARY[9:]!r}\nREPORT = {str(report)!r}\n" + _HOSTILE_SUITE
    )
    before = _tree_digest(ws)

    result = await run_tests_in_snapshot(HostTestExecutor(), ws)

    assert result.passed, result.stdout + result.stderr
    assert _tree_digest(ws) == before
    seen = json.loads(report.read_text())
    assert seen["git_visible"] is False
    assert seen["env_visible"] is False
    assert seen["canary_in_tree"] is False
    assert not Path(seen["cwd"]).exists()  # the extraction was removed


@pytest.mark.asyncio
async def test_extraction_is_removed_after_timeout_and_cancellation(tmp_path: Path) -> None:
    cwd_file = tmp_path / "cwd.txt"
    (tmp_path / "test_slow.py").write_text(
        "import os, time\n"
        f"def test_slow():\n    open({str(cwd_file)!r}, 'w').write(os.getcwd())\n    time.sleep(300)\n"
    )

    result = await run_tests_in_snapshot(HostTestExecutor(timeout_seconds=3), tmp_path)
    assert "timed out" in result.stderr
    assert not Path(cwd_file.read_text()).exists()

    cwd_file.unlink()
    task = asyncio.create_task(run_tests_in_snapshot(HostTestExecutor(timeout_seconds=120), tmp_path))
    deadline = time.monotonic() + 15
    while not (cwd_file.exists() and cwd_file.read_text()) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not Path(cwd_file.read_text()).exists()


@pytest.mark.asyncio
async def test_extraction_survives_tests_that_lock_down_permissions(tmp_path: Path) -> None:
    cwd_file = tmp_path / "cwd.txt"
    (tmp_path / "test_lock.py").write_text(
        "import os\n"
        "def test_lock():\n"
        f"    open({str(cwd_file)!r}, 'w').write(os.getcwd())\n"
        "    os.makedirs('locked/inner')\n"
        "    open('locked/inner/f', 'w').write('x')\n"
        "    os.chmod('locked/inner', 0)\n"
        "    os.chmod('locked', 0)\n"
    )
    result = await run_tests_in_snapshot(HostTestExecutor(), tmp_path)
    assert result.passed, result.stdout + result.stderr
    assert not Path(cwd_file.read_text()).exists()


class _RecordingExecutor:
    __test__ = False

    def __init__(self, behaviour: str = "pass") -> None:
        self.behaviour = behaviour
        self.snapshots: list[WorkspaceSnapshot] = []
        self.started = asyncio.Event()

    @property
    def isolation(self) -> IsolationLevel:
        return IsolationLevel.NONE

    async def run(self, snapshot: WorkspaceSnapshot) -> TestResult:
        self.snapshots.append(snapshot)
        assert snapshot.archive_path.exists()
        self.started.set()
        if self.behaviour == "raise":
            raise RuntimeError("executor exploded")
        if self.behaviour == "hang":
            await asyncio.sleep(300)
        return TestResult(passed=True, exit_code=0, stdout="", stderr="", duration_seconds=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("behaviour", ["pass", "raise", "hang"])
async def test_snapshot_archive_is_removed_on_every_exit_path(tmp_path: Path, behaviour: str) -> None:
    (tmp_path / "a.py").write_text("a\n")
    executor = _RecordingExecutor(behaviour)
    task = asyncio.create_task(run_tests_in_snapshot(executor, tmp_path))
    if behaviour == "hang":
        await executor.started.wait()
        task.cancel()
    expected = (
        pytest.raises((RuntimeError, asyncio.CancelledError))
        if behaviour != "pass"
        else contextlib.nullcontext()
    )
    with expected:
        await task
    [snap] = executor.snapshots
    assert not snap.archive_path.parent.exists()
    assert tmp_path not in snap.archive_path.parents


@pytest.mark.asyncio
async def test_cancellation_during_build_waits_for_the_builder_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.py").write_text("a\n")
    started, finished = threading.Event(), threading.Event()
    holders: list[Path] = []
    real_build = snapshot_mod._build_archive

    def slow_build(root: Path, archive_path: Path, limits: SnapshotLimits) -> Any:
        holders.append(archive_path.parent)
        started.set()
        time.sleep(0.5)
        result = real_build(root, archive_path, limits)  # must not race the cleanup
        finished.set()
        return result

    monkeypatch.setattr(snapshot_mod, "_build_archive", slow_build)
    executor = _RecordingExecutor()
    task = asyncio.create_task(run_tests_in_snapshot(executor, tmp_path))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert finished.is_set()
    assert executor.snapshots == []
    assert not holders[0].exists()


@pytest.mark.asyncio
async def test_run_blocking_keeps_the_event_loop_running() -> None:
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        assert await run_blocking(lambda: time.sleep(0.6) or 42) == 42
    finally:
        beat.cancel()
    assert ticks >= 5


# ── Fail closed ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unavailable_executor_fails_closed_without_building_a_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("snapshot built for an executor that cannot run")

    monkeypatch.setattr("app.execution.test_executor.workspace_snapshot", forbidden)
    with pytest.raises(ExecutionUnavailableError, match="disabled"):
        await run_tests_in_snapshot(UnavailableTestExecutor("Test execution is disabled."), tmp_path)


def _tiny_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.execution.test_executor.workspace_snapshot",
        functools.partial(workspace_snapshot, limits=SnapshotLimits(max_files=1)),
    )


@pytest.mark.asyncio
async def test_testing_stage_fails_closed_when_the_snapshot_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.py").write_text("a\n")
    (tmp_path / "b.py").write_text("b\n")
    _tiny_limits(monkeypatch)
    executor = _RecordingExecutor()

    with pytest.raises(TestingStageError, match="snapshot rejected"):
        await run_testing_stage(WorkspaceManager(tmp_path), executor)
    assert executor.snapshots == []


@pytest.mark.asyncio
async def test_run_tests_tool_reports_a_rejected_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agents.tool_context import ToolContext
    from app.agents.coding_agent import make_coding_agent

    (tmp_path / "a.py").write_text("a\n")
    (tmp_path / "b.py").write_text("b\n")
    _tiny_limits(monkeypatch)
    executor = _RecordingExecutor()
    agent = make_coding_agent(workspace=WorkspaceManager(tmp_path), executor=executor, model="gpt-4o")
    tool: Any = next(t for t in agent.tools if t.name == "run_tests")

    ctx = ToolContext(context=None, tool_name="run_tests", tool_call_id="1", tool_arguments="{}")
    output = str(await tool.on_invoke_tool(ctx, "{}"))

    assert "snapshot rejected" in output
    assert executor.snapshots == []


def test_no_app_code_hands_an_executor_the_workspace() -> None:
    """Every executor.run call goes through run_tests_in_snapshot."""
    app_dir = Path(__file__).resolve().parents[2] / "app"
    callers = sorted(
        str(p.relative_to(app_dir))
        for p in app_dir.rglob("*.py")
        if "executor.run(" in p.read_text()
    )
    assert callers == ["execution/test_executor.py"]


# ── Retry copies share the policy ──────────────────────────────────────────────


def test_retry_copy_uses_the_snapshot_policy(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text(CANARY)
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    _trusted_workspace(src)
    (src / "pkg").mkdir()
    (src / "pkg" / "link.py").symlink_to("../app.py")
    (src / "escape").symlink_to(outside)
    os.link(outside, src / "hard.txt")

    _copy_isolated_workspace(src, dst)

    copied = sorted(str(p.relative_to(dst)) for p in dst.rglob("*"))
    assert copied == ["app.py", "pkg", "pkg/link.py", "victim.py"]
    assert os.readlink(dst / "pkg" / "link.py") == "../app.py"
    assert (dst / "pkg" / "link.py").read_text() == "tampered = False\n"


def test_retry_copy_fails_closed_on_limits(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "big.bin").write_bytes(b"x" * 10)
    with pytest.raises(SnapshotLimitError):
        snapshot_mod.copy_workspace(src, dst, SnapshotLimits(max_total_bytes=5))
    assert list(dst.iterdir()) == []
