"""Workspace Manager — sandboxed file access layer for the coding agent.

All file operations are validated against the workspace root.
Path traversal attempts (e.g. ../../) raise WorkspaceError before any I/O.
"""

from __future__ import annotations

import difflib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Hard file-size ceiling to prevent the agent reading huge binaries.
MAX_FILE_SIZE: int = 512 * 1024  # 512 KB

# Directories to skip when listing or searching files.
IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
        "dist",
        "build",
        ".eggs",
    }
)


class WorkspaceError(Exception):
    """Raised when a workspace operation fails or violates boundaries."""


class WorkspaceManager:
    """Provides sandboxed file-system access for the coding agent.

    Every path supplied by the agent is resolved and validated to remain
    strictly within ``root_path``.  Any attempt to escape the sandbox raises
    :class:`WorkspaceError` before any I/O is performed.
    """

    def __init__(self, root_path: Path, enforced_root: Path | None = None) -> None:
        self._root: Path = root_path.resolve()
        if not self._root.exists():
            raise WorkspaceError(f"Workspace root does not exist: {self._root}")

        # Enforce WORKSPACE_ROOT boundary if provided
        if enforced_root is not None:
            enforced = enforced_root.resolve()
            try:
                self._root.relative_to(enforced)
            except ValueError:
                raise WorkspaceError(
                    f"Workspace root {self._root} is outside enforced root {enforced}"
                ) from None

        # Maps rel_path -> original content (None = new file).
        self._originals: dict[str, str | None] = {}
        self._modified: set[str] = set()
        logger.debug("WorkspaceManager initialised at %s", self._root)

    # ── Public properties ──────────────────────────────────────────────────────

    @property
    def root(self) -> Path:
        """The resolved workspace root directory."""
        return self._root

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _snapshot(self, resolved: Path) -> str | None:
        """Read existing file content for diff generation, subject to size limits."""
        if not resolved.exists():
            return None
        if not resolved.is_file():
            raise WorkspaceError(f"Cannot snapshot non-file: {resolved}")
        size = resolved.stat().st_size
        if size > MAX_FILE_SIZE:
            raise WorkspaceError(
                f"File exceeds size limit ({size} bytes > {MAX_FILE_SIZE} bytes): {resolved}"
            )
        return resolved.read_text(encoding="utf-8", errors="replace")

    def _resolve(self, rel_path: str) -> Path:
        """Resolve *rel_path* relative to the workspace root.

        Raises :class:`WorkspaceError` if the result escapes the root
        (path-traversal protection).
        """
        # Strip leading slashes to force relative interpretation.
        safe = rel_path.lstrip("/")
        resolved = (self._root / safe).resolve()
        try:
            resolved.relative_to(self._root)
        except ValueError:
            raise WorkspaceError(
                f"Path traversal detected: {rel_path!r} resolves outside workspace root"
            ) from None
        return resolved

    def _get_action(self, path: str) -> str:
        """Return 'created', 'modified', or 'deleted' for a modified path."""
        original = self._originals.get(path)
        resolved = self._root / path
        if original is None:
            return "created"
        if not resolved.exists():
            return "deleted"
        return "modified"

    # ── Read operations ────────────────────────────────────────────────────────

    def list_files(self, path: str = ".") -> list[str]:
        """Return sorted workspace-relative paths of all files under *path*."""
        target = self._resolve(path)
        if not target.exists():
            raise WorkspaceError(f"Path does not exist: {path!r}")
        results: list[str] = []
        for entry in sorted(target.rglob("*")):
            if any(part in IGNORED_DIRS for part in entry.parts):
                continue
            if entry.is_file():
                results.append(str(entry.relative_to(self._root)))
        return results

    def read_file(self, path: str) -> str:
        """Return UTF-8 text content of *path*."""
        resolved = self._resolve(path)
        if not resolved.exists():
            raise WorkspaceError(f"File not found: {path!r}")
        if not resolved.is_file():
            raise WorkspaceError(f"Not a regular file: {path!r}")
        size = resolved.stat().st_size
        if size > MAX_FILE_SIZE:
            raise WorkspaceError(
                f"File exceeds size limit ({size} bytes > {MAX_FILE_SIZE} bytes): {path!r}"
            )
        return resolved.read_text(encoding="utf-8", errors="replace")

    def search_files(self, query: str, path: str = ".") -> list[dict[str, object]]:
        """Return files containing *query*, with matching line numbers and content."""
        results: list[dict[str, object]] = []
        for rel in self.list_files(path):
            try:
                content = self.read_file(rel)
            except WorkspaceError:
                continue
            if query not in content:
                continue
            matches: list[dict[str, object]] = [
                {"line": i, "content": line}
                for i, line in enumerate(content.splitlines(), 1)
                if query in line
            ]
            if matches:
                results.append({"file": rel, "matches": matches})
        return results

    # ── Write operations ───────────────────────────────────────────────────────

    def write_file(self, path: str, content: str) -> None:
        """Write *content* to *path*, creating parent directories as needed."""
        if len(content.encode("utf-8")) > MAX_FILE_SIZE:
            raise WorkspaceError(f"Write content exceeds maximum file size limit of {MAX_FILE_SIZE} bytes.")
        resolved = self._resolve(path)
        # Snapshot the original before first modification.
        if path not in self._originals:
            self._originals[path] = self._snapshot(resolved)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        self._modified.add(path)
        logger.info("WorkspaceManager.write_file: %s", path)

    def delete_file(self, path: str) -> None:
        """Delete *path* from the workspace."""
        resolved = self._resolve(path)
        if not resolved.exists():
            raise WorkspaceError(f"File not found for deletion: {path!r}")
        if path not in self._originals:
            self._originals[path] = self._snapshot(resolved)
        resolved.unlink()
        self._modified.add(path)
        logger.info("WorkspaceManager.delete_file: %s", path)

    # ── Change tracking ────────────────────────────────────────────────────────

    def get_modified_paths(self) -> list[str]:
        """Return sorted list of paths modified in this session."""
        return sorted(self._modified)

    def generate_diff(self) -> str:
        """Return a unified diff of all modifications made in this session."""
        parts: list[str] = []
        for path in sorted(self._modified):
            original = self._originals.get(path)
            resolved = self._root / path
            original_lines = (original or "").splitlines(keepends=True)
            current_lines = (
                resolved.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
                if resolved.exists()
                else []
            )
            diff = list(
                difflib.unified_diff(
                    original_lines,
                    current_lines,
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
            )
            parts.extend(diff)
        return "".join(parts)
