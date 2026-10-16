"""Deterministic, policy-filtered snapshots of a workspace.

Repository-controlled code (the test suite, conftest.py, plugins) must never
run against the trusted workspace the worker later commits and pushes from.
Instead it gets a *snapshot*: a tar archive built from the workspace under a
fixed policy, extracted into a throwaway directory, and discarded afterwards.
Nothing is ever copied back.

A snapshot is a content boundary, not an OS-level isolation boundary.  An
executor that runs on the worker host (``host_unsafe``) still runs as the
worker's UID and can reach the original workspace by absolute path; only a
sandboxed executor makes the separation enforceable.

Policy
------
* Only regular files and validated symlinks are archived.  Modes are
  normalised to 0644/0755 (no setuid/setgid/sticky), owners to 0:0 and mtimes
  to a fixed date, and entries are sorted, so the same tree always produces a
  byte-identical archive.
* Excluded and recorded in the manifest with a reason: ``.git`` (file or
  directory, any depth, any case), the workspace's ignored directories,
  secret-looking names, FIFOs/sockets/devices, hardlinked files
  (``st_nlink > 1``), names with control characters or invalid UTF-8,
  over-deep paths, oversized files, and symlinks unless they are relative,
  stay inside the workspace and point at a regular file that is itself in the
  snapshot.
* Aggregate limits (entries scanned, files archived, total bytes) fail closed
  with :class:`SnapshotLimitError`.
* The tree is walked with directory file descriptors and ``O_NOFOLLOW``, and
  each file's identity is re-checked when it is read, so swapping a path for a
  symlink mid-build cannot pull in content from outside the workspace.
* Extraction validates every member before writing anything, and refuses to
  write outside the target directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import logging
import os
import posixpath
import stat
import tarfile
import tempfile
from collections import Counter
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import IO, Any, TypeVar

from app.workspace.manager import IGNORED_DIRS

logger = logging.getLogger(__name__)

# 1980-01-02 UTC: a fixed mtime that is valid for zip archives in any timezone.
SNAPSHOT_MTIME: int = 315_619_200

_SECRET_PREFIXES: tuple[str, ...] = (".env", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")
_SECRET_SUFFIXES: tuple[str, ...] = (".pem", ".key", ".pfx", ".p12", ".token")
_SECRET_NAMES: frozenset[str] = frozenset({"credentials", "secrets", ".credentials", ".secrets"})
_BYTECODE_SUFFIXES: tuple[str, ...] = (".pyc", ".pyo", ".pyd")

_NOFOLLOW = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
_DIR_FLAGS = _NOFOLLOW | os.O_DIRECTORY
# O_NONBLOCK: a path swapped for a FIFO must not hang the open.
_FILE_FLAGS = _NOFOLLOW | os.O_NONBLOCK


@dataclass(frozen=True)
class SnapshotLimits:
    max_files: int = 20_000
    max_total_bytes: int = 200 * 1024 * 1024
    max_file_bytes: int = 10 * 1024 * 1024
    # Every directory entry looked at, including ones later excluded.
    max_scanned_entries: int = 100_000
    max_depth: int = 64


DEFAULT_LIMITS = SnapshotLimits()


class ExclusionReason(str, Enum):
    GIT_METADATA = "git_metadata"
    IGNORED_DIRECTORY = "ignored_directory"
    # An exclusion-reason label, not a credential.
    SECRET = "secret"  # nosec B105
    BYTECODE = "bytecode"
    SPECIAL_FILE = "special_file"
    HARDLINK = "hardlink"
    UNSAFE_NAME = "unsafe_name"
    TOO_DEEP = "too_deep"
    TOO_LARGE = "too_large"
    SYMLINK_ABSOLUTE = "symlink_absolute"
    SYMLINK_ESCAPE = "symlink_escape"
    SYMLINK_TARGET = "symlink_target"


class SnapshotError(Exception):
    """The workspace cannot be snapshotted, or an archive is unsafe to extract."""


class SnapshotLimitError(SnapshotError):
    """An aggregate snapshot limit was exceeded."""


@dataclass(frozen=True)
class SnapshotEntry:
    path: str
    kind: str  # "file" or "symlink"
    mode: int
    size: int = 0
    sha256: str = ""
    target: str = ""


@dataclass(frozen=True)
class SnapshotExclusion:
    path: str
    reason: ExclusionReason


@dataclass(frozen=True)
class SnapshotManifest:
    entries: tuple[SnapshotEntry, ...]
    excluded: tuple[SnapshotExclusion, ...]
    total_bytes: int


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """A built snapshot archive.  Executors receive this, never the workspace."""

    archive_path: Path
    manifest: SnapshotManifest
    sha256: str


# ── Policy ─────────────────────────────────────────────────────────────────────


def _display_char(ch: str) -> str:
    code = ord(ch)
    if 0xDC80 <= code <= 0xDCFF:  # an undecodable byte (surrogateescape)
        return f"\\x{code - 0xDC00:02x}"
    if ch.isprintable():
        return ch
    return ch.encode("unicode_escape", errors="backslashreplace").decode("ascii")


def _display(name: str) -> str:
    """A printable rendering of a (possibly hostile) name for logs and manifests."""
    return "".join(_display_char(ch) for ch in name)


def _unsafe_name(name: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F or 0xD800 <= ord(ch) <= 0xDFFF for ch in name)


def _is_secret(name: str) -> bool:
    lower = name.lower()
    return (
        lower in _SECRET_NAMES
        or lower.startswith(_SECRET_PREFIXES)
        or lower.endswith(_SECRET_SUFFIXES)
    )


def _name_exclusion(name: str, is_dir: bool) -> ExclusionReason | None:
    if _unsafe_name(name):
        return ExclusionReason.UNSAFE_NAME
    if name.lower() == ".git":
        return ExclusionReason.GIT_METADATA
    if is_dir and name in IGNORED_DIRS:
        return ExclusionReason.IGNORED_DIRECTORY
    if _is_secret(name):
        return ExclusionReason.SECRET
    if not is_dir and name.lower().endswith(_BYTECODE_SUFFIXES):
        return ExclusionReason.BYTECODE
    return None


def _symlink_exclusion(path: str, target: str) -> tuple[ExclusionReason | None, str]:
    """Validate a symlink's text; return a reason, or the in-tree path it names."""
    if not target or _unsafe_name(target):
        return ExclusionReason.UNSAFE_NAME, ""
    if target.startswith("/"):
        return ExclusionReason.SYMLINK_ABSOLUTE, ""
    # Only "../" steps followed by plain names: no "." / "" components and no
    # ".." after a name, so the link text means the same thing lexically and
    # on disk.
    parts = target.split("/")
    ups = 0
    while ups < len(parts) and parts[ups] == "..":
        ups += 1
    if ups == len(parts) or any(p in ("", ".", "..") for p in parts[ups:]):
        return ExclusionReason.SYMLINK_ESCAPE, ""
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    if resolved == ".." or resolved.startswith("../") or resolved == ".":
        return ExclusionReason.SYMLINK_ESCAPE, ""
    return None, resolved


# ── Building ───────────────────────────────────────────────────────────────────


@dataclass
class _Candidate:
    parts: tuple[str, ...]
    st: os.stat_result
    target: str = ""

    @property
    def path(self) -> str:
        return "/".join(self.parts)


class _Scan:
    def __init__(self, limits: SnapshotLimits) -> None:
        self.limits = limits
        self.scanned = 0
        self.files: list[_Candidate] = []
        self.links: list[_Candidate] = []
        self.excluded: list[SnapshotExclusion] = []

    def exclude(self, parts: tuple[str, ...], reason: ExclusionReason) -> None:
        self.excluded.append(SnapshotExclusion(_display("/".join(parts)), reason))

    def walk(self, dir_fd: int, prefix: tuple[str, ...]) -> None:
        with os.scandir(dir_fd) as it:
            names = sorted(entry.name for entry in it)
        for name in names:
            self.scanned += 1
            if self.scanned > self.limits.max_scanned_entries:
                raise SnapshotLimitError(
                    f"Workspace has more than {self.limits.max_scanned_entries} entries"
                )
            parts = (*prefix, name)
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            reason = _name_exclusion(name, stat.S_ISDIR(st.st_mode))
            if reason is not None:
                self.exclude(parts, reason)
            elif stat.S_ISDIR(st.st_mode):
                if len(parts) > self.limits.max_depth:
                    self.exclude(parts, ExclusionReason.TOO_DEEP)
                    continue
                child = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
                try:
                    if not _same_file(os.fstat(child), st):
                        raise SnapshotError(f"Workspace changed during snapshot: {_display(name)}")
                    self.walk(child, parts)
                finally:
                    os.close(child)
            elif stat.S_ISLNK(st.st_mode):
                self.links.append(_Candidate(parts, st, os.readlink(name, dir_fd=dir_fd)))
            elif not stat.S_ISREG(st.st_mode):
                self.exclude(parts, ExclusionReason.SPECIAL_FILE)
            elif st.st_nlink > 1:
                self.exclude(parts, ExclusionReason.HARDLINK)
            elif st.st_size > self.limits.max_file_bytes:
                self.exclude(parts, ExclusionReason.TOO_LARGE)
            else:
                self.files.append(_Candidate(parts, st))


def _same_file(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _read_verified(root_fd: int, cand: _Candidate) -> bytes:
    """Re-open *cand* without following any symlink and check it is unchanged."""
    fds: list[int] = []
    try:
        dir_fd = root_fd
        for part in cand.parts[:-1]:
            dir_fd = os.open(part, _DIR_FLAGS, dir_fd=dir_fd)
            fds.append(dir_fd)
        try:
            fd = os.open(cand.parts[-1], _FILE_FLAGS, dir_fd=dir_fd)
        except OSError as exc:
            raise SnapshotError(f"Cannot read {_display(cand.path)}: {exc.strerror}") from None
        fds.append(fd)
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or not _same_file(st, cand.st)
            or st.st_nlink > 1
            or st.st_size != cand.st.st_size
        ):
            raise SnapshotError(f"Workspace changed during snapshot: {_display(cand.path)}")
        os.set_blocking(fd, True)
        chunks: list[bytes] = []
        remaining = st.st_size
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining != 0 or os.read(fd, 1):
            raise SnapshotError(f"Workspace changed during snapshot: {_display(cand.path)}")
        return b"".join(chunks)
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _tarinfo(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.mtime = SNAPSHOT_MTIME
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def write_snapshot(
    workspace_root: Path, out: IO[bytes], limits: SnapshotLimits = DEFAULT_LIMITS
) -> SnapshotManifest:
    """Write a snapshot of *workspace_root* to *out* as an uncompressed tar."""
    try:
        return _write_snapshot(workspace_root, out, limits)
    except OSError as exc:
        raise SnapshotError(f"Cannot snapshot workspace: {exc.strerror or exc}") from None


def _write_snapshot(
    workspace_root: Path, out: IO[bytes], limits: SnapshotLimits
) -> SnapshotManifest:
    root_fd = os.open(workspace_root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        scan = _Scan(limits)
        scan.walk(root_fd, ())

        file_paths = {cand.path for cand in scan.files}
        links: list[_Candidate] = []
        for cand in scan.links:
            reason, resolved = _symlink_exclusion(cand.path, cand.target)
            if reason is None and resolved not in file_paths:
                reason = ExclusionReason.SYMLINK_TARGET
            if reason is None:
                links.append(cand)
            else:
                scan.exclude(cand.parts, reason)

        if len(scan.files) + len(links) > limits.max_files:
            raise SnapshotLimitError(f"Workspace has more than {limits.max_files} files")
        total = sum(cand.st.st_size for cand in scan.files)
        if total > limits.max_total_bytes:
            raise SnapshotLimitError(f"Workspace exceeds {limits.max_total_bytes} bytes")

        entries: list[SnapshotEntry] = []
        members = sorted([*scan.files, *links], key=lambda c: c.parts)
        with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            for cand in members:
                info = _tarinfo(cand.path)
                if stat.S_ISLNK(cand.st.st_mode):
                    info.type = tarfile.SYMTYPE
                    info.linkname = cand.target
                    info.mode = 0o777
                    tar.addfile(info)
                    entries.append(SnapshotEntry(cand.path, "symlink", 0o777, target=cand.target))
                    continue
                data = _read_verified(root_fd, cand)
                info.size = len(data)
                info.mode = 0o755 if cand.st.st_mode & 0o111 else 0o644
                tar.addfile(info, io.BytesIO(data))
                entries.append(
                    SnapshotEntry(
                        cand.path, "file", info.mode, len(data), hashlib.sha256(data).hexdigest()
                    )
                )
    finally:
        os.close(root_fd)

    excluded = tuple(sorted(scan.excluded, key=lambda e: e.path))
    return SnapshotManifest(tuple(entries), excluded, total)


# ── Extraction ─────────────────────────────────────────────────────────────────


def _validate_members(
    members: list[tarfile.TarInfo], limits: SnapshotLimits
) -> None:
    if len(members) > limits.max_files:
        raise SnapshotLimitError(f"Snapshot has more than {limits.max_files} members")
    names: set[str] = set()
    files: set[str] = set()
    total = 0
    for m in members:
        name = m.name
        shown = _display(name)
        if not (m.isreg() or m.issym()):
            raise SnapshotError(f"Unsupported snapshot member type: {shown}")
        parts = name.split("/")
        if (
            not name
            or name.startswith("/")
            or posixpath.normpath(name) != name
            or any(p in ("", ".", "..") for p in parts)
            or _unsafe_name(name)
            or any(p.lower() == ".git" for p in parts)
        ):
            raise SnapshotError(f"Unsafe snapshot member name: {shown}")
        if name in names:
            raise SnapshotError(f"Duplicate snapshot member: {shown}")
        if m.mode & 0o7000:
            raise SnapshotError(f"Snapshot member has special mode bits: {shown}")
        names.add(name)
        if m.isreg():
            if m.size > limits.max_file_bytes:
                raise SnapshotLimitError(f"Snapshot member too large: {shown}")
            total += m.size
            files.add(name)
    if total > limits.max_total_bytes:
        raise SnapshotLimitError(f"Snapshot exceeds {limits.max_total_bytes} bytes")
    for m in members:
        # No member may live "inside" another file or symlink member.
        parts = m.name.split("/")
        if any("/".join(parts[:i]) in names for i in range(1, len(parts))):
            raise SnapshotError(f"Snapshot member nested under a file: {_display(m.name)}")
        if m.issym():
            reason, resolved = _symlink_exclusion(m.name, m.linkname)
            if reason is not None or resolved not in files:
                raise SnapshotError(f"Unsafe snapshot symlink: {_display(m.name)}")


def extract_snapshot(
    archive: IO[bytes], target_dir: Path, limits: SnapshotLimits = DEFAULT_LIMITS
) -> None:
    """Extract a snapshot archive into the empty directory *target_dir*.

    Every member is validated before anything is written; any unsafe member
    rejects the whole archive.  Python's ``data`` extraction filter is applied
    on top as a second line of defence.
    """
    st = os.lstat(target_dir)
    if not stat.S_ISDIR(st.st_mode):
        raise SnapshotError("Snapshot target is not a directory")
    if any(target_dir.iterdir()):
        raise SnapshotError("Snapshot target directory is not empty")
    with tarfile.open(fileobj=archive, mode="r:") as tar:
        members = tar.getmembers()
        _validate_members(members, limits)
        try:
            tar.extractall(target_dir, members=members, filter="data")
        except tarfile.FilterError as exc:
            raise SnapshotError(f"Snapshot rejected by extraction filter: {exc}") from None


# ── Lifecycle helpers ──────────────────────────────────────────────────────────

_T = TypeVar("_T")


async def run_blocking(fn: Callable[..., _T], *args: Any) -> _T:
    """Run *fn* in a thread without blocking the event loop.

    If the caller is cancelled, the thread is allowed to finish before the
    cancellation propagates, so the caller's cleanup never races it.
    """
    fut = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        await asyncio.wait([fut])
        if not fut.cancelled():
            fut.exception()  # mark retrieved; the cancellation wins
        raise


def private_tempdir(prefix: str) -> tempfile.TemporaryDirectory[str]:
    """A 0700 temporary directory whose cleanup survives chmod-ed contents.

    The standard library's cleanup resets permissions without following
    symlinks; leftovers (for example, files a daemon is still writing) are
    ignored rather than masking the caller's result.
    """
    return tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True)


def _summarise(manifest: SnapshotManifest) -> str:
    reasons = Counter(e.reason.value for e in manifest.excluded)
    return ", ".join(f"{reason}={n}" for reason, n in sorted(reasons.items())) or "none"


def _build_archive(workspace_root: Path, archive_path: Path, limits: SnapshotLimits) -> tuple[SnapshotManifest, str]:
    fd = os.open(archive_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as out:
        manifest = write_snapshot(workspace_root, out, limits)
    digest = hashlib.sha256()
    with open(archive_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return manifest, digest.hexdigest()


@contextlib.asynccontextmanager
async def workspace_snapshot(
    workspace_root: Path, limits: SnapshotLimits = DEFAULT_LIMITS
) -> AsyncIterator[WorkspaceSnapshot]:
    """Build a snapshot of *workspace_root* in a private temporary directory.

    The archive is deleted when the context exits, including on cancellation.
    """
    with private_tempdir("codeforge-snapshot-") as holder:
        archive_path = Path(holder) / "workspace.tar"
        manifest, digest = await run_blocking(_build_archive, workspace_root, archive_path, limits)
        logger.info(
            "Workspace snapshot built: files=%d bytes=%d excluded=%d (%s) sha256=%s",
            len(manifest.entries),
            manifest.total_bytes,
            len(manifest.excluded),
            _summarise(manifest),
            digest,
        )
        yield WorkspaceSnapshot(archive_path, manifest, digest)


def copy_workspace(src: Path, dst: Path, limits: SnapshotLimits = DEFAULT_LIMITS) -> SnapshotManifest:
    """Copy *src* into the empty directory *dst* through the snapshot policy."""
    with tempfile.TemporaryFile() as buf:
        manifest = write_snapshot(src, buf, limits)
        buf.seek(0)
        extract_snapshot(buf, dst, limits)
    return manifest
