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
* A file over the per-file limit is classified (:func:`classify_required_file`).
  Source code, tests, test inputs/fixtures and build or test configuration
  fail the build with :class:`RequiredFileTooLargeError`: testing without them
  would not be a validation of the repository.  Anything else is treated as a
  non-code asset, excluded and reported in the exclusion notice.
* The tree is walked with directory file descriptors and ``O_NOFOLLOW``, and
  each file's identity is re-checked when it is read, so swapping a path for a
  symlink mid-build cannot pull in content from outside the workspace.
* After the archive is written, every walked directory is listed again and
  every archived entry is re-checked.  An entry added to, removed from or
  renamed within a walked directory, or an archived file or symlink that was
  replaced, retargeted or whose size, mtime or ctime changed, fails the
  build.  Not detected: a same-size in-place rewrite of an archived file that
  lands within the filesystem's timestamp granularity of the scan, an
  excluded entry replaced under the same name, and changes inside excluded
  directories (none of which are archived).  The snapshot is a point-in-time
  copy; changes after the build are not its concern.
* Extraction validates every member before writing anything, and refuses to
  write outside the target directory.

Exclusions other than routine ones (``.git``, bytecode, cache directories)
are reported by :func:`exclusion_notice`, so a test run on an incomplete
snapshot is never presented as a full validation.  Secret-looking and unsafe
names are counted but never named.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import logging
import os
import posixpath
import re
import stat
import tarfile
import tempfile
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
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
    # Per file.  A larger source/test/fixture/config file fails the build
    # (RequiredFileTooLargeError); a larger non-code asset is excluded.
    max_file_bytes: int = 10 * 1024 * 1024
    # Every directory entry looked at, including ones later excluded.
    max_scanned_entries: int = 100_000
    # Directory nesting: a directory whose path has N components is allowed
    # when N <= max_depth, so a file or symlink inside it may have
    # max_depth + 1 components.
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


class RequiredFileTooLargeError(SnapshotLimitError):
    """A source, test, fixture or configuration file exceeds the per-file limit."""


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


# ── Oversized-file classification ──────────────────────────────────────────────
#
# Conservative by design: when a large file could plausibly be read or
# imported by the tests, it is required, and an oversized required file fails
# the build rather than producing a test run that silently skipped it.  Only
# files matching none of the rules below are treated as non-code assets.
# All comparisons are case-insensitive.

# Any file below one of these directories is a test input.
_FIXTURE_DIRS: frozenset[str] = frozenset(
    {
        "fixtures", "fixture", "__fixtures__", "test_fixtures", "test-fixtures",
        "testdata", "test_data", "test-data", "__snapshots__",
        "cassettes", "golden", "goldens",
    }
)
_TEST_DIRS: frozenset[str] = frozenset({"test", "tests", "__tests__", "__test__", "spec", "specs"})
# test_x, x_test(.ext), x.test.ext, x.spec.ext, x_spec.ext; *Test.java etc.
# are covered by their source extension.
_TEST_NAME = re.compile(r"^test_|_test(\.|$)|\.test\.|\.spec\.|_spec\.", re.IGNORECASE)
_SOURCE_SUFFIXES: frozenset[str] = frozenset(
    {
        # Python
        ".py", ".pyi", ".pyw", ".pyx", ".pxd",
        # JavaScript / TypeScript / web components
        ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue", ".svelte",
        ".html", ".htm", ".css", ".scss", ".sass", ".less",
        # Systems and JVM / .NET languages
        ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx", ".m", ".mm",
        ".java", ".kt", ".kts", ".scala", ".groovy", ".gradle", ".cs", ".fs", ".vb", ".swift",
        # Scripting languages
        ".rb", ".rake", ".php", ".pl", ".pm", ".lua", ".r", ".jl", ".dart", ".ex", ".exs",
        ".erl", ".hs", ".clj", ".sh", ".bash", ".zsh", ".fish", ".ps1",
        # Schemas, queries and templates read by code
        ".sql", ".proto", ".graphql", ".gql", ".j2", ".jinja", ".jinja2",
        # Configuration formats
        ".toml", ".cfg", ".ini", ".yaml", ".yml", ".properties",
    }
)
# Entry and configuration files identified by full name.
_REQUIRED_NAMES: frozenset[str] = frozenset(
    {
        "makefile", "gnumakefile", "dockerfile", "containerfile", "jenkinsfile",
        "rakefile", "gemfile", "procfile", "vagrantfile", "justfile", "pipfile",
        "snakefile", "build", "workspace", "cmakelists.txt", "package.json",
        "tsconfig.json", "jsconfig.json", "deno.json", "composer.json", "manage.py",
    }
)
_REQUIREMENTS_NAME = re.compile(r"^(requirements|constraints)([-_.][^/]*)?\.(txt|in)$", re.IGNORECASE)


def classify_required_file(parts: Sequence[str], *, is_script: bool = False) -> str | None:
    """Return why the file at *parts* is required by tests, or None for an asset.

    *parts* are the path components relative to the workspace root.
    *is_script* marks an extensionless file that starts with ``#!``.
    """
    lowered = [p.lower() for p in parts]
    directories, name = lowered[:-1], lowered[-1]
    if any(d in _FIXTURE_DIRS for d in directories):
        return "test fixture"
    if any(d in _TEST_DIRS for d in directories):
        return "test input"
    if _TEST_NAME.search(name):
        return "test"
    if posixpath.splitext(name)[1] in _SOURCE_SUFFIXES:
        return "source"
    if name in _REQUIRED_NAMES or _REQUIREMENTS_NAME.match(name):
        return "build or test configuration"
    if is_script and not posixpath.splitext(name)[1]:
        return "source"
    return None


def _starts_with_shebang(dir_fd: int, name: str) -> bool:
    """Whether an extensionless file is a script; unreadable files count as scripts."""
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    except OSError:
        return True
    try:
        return os.read(fd, 2) == b"#!"
    except OSError:
        return True
    finally:
        os.close(fd)


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
        # Oversized files the tests need: (path, why it is required, size).
        self.required_too_large: list[tuple[str, str, int]] = []
        # Every directory walked, with its identity and sorted entry names.
        self.dirs: list[tuple[tuple[str, ...], os.stat_result, list[str]]] = []

    def exclude(self, parts: tuple[str, ...], reason: ExclusionReason) -> None:
        self.excluded.append(SnapshotExclusion(_display("/".join(parts)), reason))

    def walk(self, dir_fd: int, prefix: tuple[str, ...]) -> None:
        names = _list_dir(dir_fd)
        self.dirs.append((prefix, os.fstat(dir_fd), names))
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
                is_script = "." not in name and _starts_with_shebang(dir_fd, name)
                kind = classify_required_file(parts, is_script=is_script)
                if kind is None:
                    self.exclude(parts, ExclusionReason.TOO_LARGE)
                else:
                    self.required_too_large.append((_display("/".join(parts)), kind, st.st_size))
            else:
                self.files.append(_Candidate(parts, st))


def _list_dir(dir_fd: int) -> list[str]:
    with os.scandir(dir_fd) as it:
        return sorted(entry.name for entry in it)


def _same_file(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _same_state(a: os.stat_result, b: os.stat_result) -> bool:
    """Same inode and no observable change to type, links, size or timestamps."""
    return (
        a.st_dev, a.st_ino, a.st_mode, a.st_nlink, a.st_size, a.st_mtime_ns, a.st_ctime_ns
    ) == (
        b.st_dev, b.st_ino, b.st_mode, b.st_nlink, b.st_size, b.st_mtime_ns, b.st_ctime_ns
    )


def _changed(path: str) -> SnapshotError:
    return SnapshotError(f"Workspace changed during snapshot: {_display(path)}")


@contextlib.contextmanager
def _open_dir_chain(root_fd: int, parts: tuple[str, ...]) -> Iterator[int]:
    """Open the directory *parts* below *root_fd* without following any symlink."""
    fds: list[int] = []
    try:
        dir_fd = root_fd
        for part in parts:
            dir_fd = os.open(part, _DIR_FLAGS, dir_fd=dir_fd)
            fds.append(dir_fd)
        yield dir_fd
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _read_verified(root_fd: int, cand: _Candidate) -> bytes:
    """Re-open *cand* without following any symlink and check it is unchanged."""
    with _open_dir_chain(root_fd, cand.parts[:-1]) as dir_fd:
        try:
            fd = os.open(cand.parts[-1], _FILE_FLAGS, dir_fd=dir_fd)
        except OSError as exc:
            raise SnapshotError(f"Cannot read {_display(cand.path)}: {exc.strerror}") from None
        try:
            if not _same_state(os.fstat(fd), cand.st):
                raise _changed(cand.path)
            os.set_blocking(fd, True)
            chunks: list[bytes] = []
            remaining = cand.st.st_size
            while remaining > 0:
                chunk = os.read(fd, min(remaining, 1024 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if remaining != 0 or os.read(fd, 1):
                raise _changed(cand.path)
            return b"".join(chunks)
        finally:
            os.close(fd)


def _verify_unchanged(root_fd: int, scan: _Scan, links: list[_Candidate]) -> None:
    """Fail if the walked tree differs from what was scanned.

    Runs after the archive is written, so it covers changes made at any point
    between a directory being listed and the end of the build.
    """
    for parts, st, names in scan.dirs:
        with _open_dir_chain(root_fd, parts) as dir_fd:
            if not _same_file(os.fstat(dir_fd), st) or _list_dir(dir_fd) != names:
                raise _changed("/".join(parts) or ".")
    for cand in (*scan.files, *links):
        with _open_dir_chain(root_fd, cand.parts[:-1]) as dir_fd:
            name = cand.parts[-1]
            if not _same_state(os.stat(name, dir_fd=dir_fd, follow_symlinks=False), cand.st):
                raise _changed(cand.path)
            if cand.target and os.readlink(name, dir_fd=dir_fd) != cand.target:
                raise _changed(cand.path)


def _required_too_large(
    found: list[tuple[str, str, int]], limits: SnapshotLimits
) -> RequiredFileTooLargeError:
    shown = "; ".join(f"{path} ({kind}, {size} bytes)" for path, kind, size in found[:5])
    more = f"; and {len(found) - 5} more" if len(found) > 5 else ""
    return RequiredFileTooLargeError(
        f"{len(found)} required file(s) exceed the {limits.max_file_bytes}-byte per-file "
        f"limit, so the tests cannot be run on a complete snapshot: {shown}{more}"
    )


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
        if scan.required_too_large:
            raise _required_too_large(scan.required_too_large, limits)

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
        _verify_unchanged(root_fd, scan, links)
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
        # Same semantics as the builder: the member's directory may be at
        # most max_depth components deep; the leaf name itself does not count.
        if len(parts) - 1 > limits.max_depth:
            raise SnapshotLimitError(
                f"Snapshot member nested deeper than {limits.max_depth} directories: {shown}"
            )
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


def verify_snapshot_archive(
    snapshot: WorkspaceSnapshot, limits: SnapshotLimits = DEFAULT_LIMITS
) -> None:
    """Check that *snapshot*'s archive is unchanged and every member is safe.

    For executors that hand the archive to another extractor (for example
    inside a container): the archive must still have the digest recorded when
    it was built, and must pass the same member validation as
    :func:`extract_snapshot`.  Nothing is written.
    """
    digest = hashlib.sha256()
    with open(snapshot.archive_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
        if digest.hexdigest() != snapshot.sha256:
            raise SnapshotError("Snapshot archive changed after it was built")
        fh.seek(0)
        with tarfile.open(fileobj=fh, mode="r:") as tar:
            _validate_members(tar.getmembers(), limits)


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


# Directories that only ever hold tool caches, never sources or fixtures.
_CACHE_DIRS: frozenset[str] = frozenset(
    {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache"}
)
# Names that are never repeated in notices: secret-looking names can be
# sensitive themselves, and unsafe names are attacker-chosen text.
_WITHHELD: frozenset[ExclusionReason] = frozenset(
    {ExclusionReason.SECRET, ExclusionReason.UNSAFE_NAME}
)
_REASON_TEXT: dict[ExclusionReason, str] = {
    ExclusionReason.TOO_LARGE: "non-code asset larger than the per-file size limit",
    ExclusionReason.TOO_DEEP: "nested deeper than the directory limit",
    ExclusionReason.IGNORED_DIRECTORY: "ignored directory",
    ExclusionReason.HARDLINK: "hardlinked file",
    ExclusionReason.SPECIAL_FILE: "not a regular file",
    ExclusionReason.SYMLINK_ABSOLUTE: "absolute symlink",
    ExclusionReason.SYMLINK_ESCAPE: "symlink leaving the workspace",
    ExclusionReason.SYMLINK_TARGET: "symlink not pointing at an included file",
    ExclusionReason.SECRET: "secret-looking name; names withheld",
    ExclusionReason.UNSAFE_NAME: "unsafe name; names withheld",
}
_MAX_NAMED = 20


def _is_routine(exclusion: SnapshotExclusion) -> bool:
    if exclusion.reason in (ExclusionReason.GIT_METADATA, ExclusionReason.BYTECODE):
        return True
    return (
        exclusion.reason is ExclusionReason.IGNORED_DIRECTORY
        and posixpath.basename(exclusion.path) in _CACHE_DIRS
    )


def exclusion_notice(manifest: SnapshotManifest) -> str | None:
    """Describe exclusions that may matter to a test result, or None.

    Routine exclusions (``.git``, bytecode, cache directories) are left out.
    Everything else could be source code, a fixture or configuration the
    tests needed, so it is reported: by path where the path is not itself
    sensitive, by count only for secret-looking and unsafe names.  Never
    includes file contents.
    """
    material = [e for e in manifest.excluded if not _is_routine(e)]
    if not material:
        return None
    by_reason: dict[ExclusionReason, list[str]] = {}
    for e in material:
        by_reason.setdefault(e.reason, []).append(e.path)
    lines = [
        f"Snapshot incomplete: {len(material)} workspace entr"
        f"{'y was' if len(material) == 1 else 'ies were'} excluded and not available "
        "to the tests, so this test result does not cover them."
    ]
    budget = _MAX_NAMED
    for reason in sorted(by_reason, key=lambda r: r.value):
        paths = by_reason[reason]
        line = f"- {reason.value} ({_REASON_TEXT[reason]}): {len(paths)}"
        if reason not in _WITHHELD and budget > 0:
            shown = paths[:budget]
            budget -= len(shown)
            line += " - " + ", ".join(shown)
            if len(paths) > len(shown):
                line += f", and {len(paths) - len(shown)} more"
        lines.append(line)
    return "\n".join(lines)


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
        notice = exclusion_notice(manifest)
        if notice:
            logger.warning("Workspace snapshot is incomplete.\n%s", notice)
        yield WorkspaceSnapshot(archive_path, manifest, digest)


def copy_workspace(src: Path, dst: Path, limits: SnapshotLimits = DEFAULT_LIMITS) -> SnapshotManifest:
    """Copy *src* into the empty directory *dst* through the snapshot policy.

    Callers should report :func:`exclusion_notice` for the returned manifest:
    excluded entries are absent from *dst*, not merely hidden.
    """
    with tempfile.TemporaryFile() as buf:
        manifest = write_snapshot(src, buf, limits)
        buf.seek(0)
        extract_snapshot(buf, dst, limits)
    return manifest
