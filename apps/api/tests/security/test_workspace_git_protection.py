"""Phase 10B.5 Stage 0 — WorkspaceManager must never write under ``.git/``.

Writing ``.git/config`` or ``.git/hooks/*`` would let a prompt-injected agent
plant hooks, ``core.fsmonitor`` programs or URL rewrites that the worker's
own git commands later execute on the host.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.workspace.manager import WorkspaceError, WorkspaceManager


@pytest.fixture
def ws(tmp_path: Path) -> WorkspaceManager:
    root = tmp_path / "repo"
    (root / ".git" / "hooks").mkdir(parents=True)
    (root / ".git" / "config").write_text("[core]\n\tbare = false\n")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("x = 1\n")
    return WorkspaceManager(root)


GIT_PATHS = [
    ".git/config",
    ".git/hooks/pre-commit",
    ".git/hooks/pre-push",
    ".git/info/attributes",
    ".git/new-file",
    "/.git/config",
    "./.git/config",
    "src/../.git/config",
    ".git",
    "src/.git",
    "src/.git/config",
    "vendor/lib/.git/hooks/pre-commit",
    ".GIT/config",
    ".Git/hooks/pre-commit",
]


@pytest.mark.parametrize("path", GIT_PATHS)
def test_write_under_git_dir_is_rejected(ws: WorkspaceManager, path: str) -> None:
    before = (ws.root / ".git" / "config").read_text()
    with pytest.raises(WorkspaceError, match=r"\.git"):
        ws.write_file(path, "[core]\n\tfsmonitor = touch /tmp/pwned\n")
    assert (ws.root / ".git" / "config").read_text() == before
    assert not (ws.root / ".git" / "hooks" / "pre-commit").exists()
    assert ws.get_modified_paths() == []


@pytest.mark.parametrize("path", [".git/config", "src/../.git/config", ".GIT/config"])
def test_delete_under_git_dir_is_rejected(ws: WorkspaceManager, path: str) -> None:
    with pytest.raises(WorkspaceError, match=r"\.git"):
        ws.delete_file(path)
    assert (ws.root / ".git" / "config").exists()
    assert ws.get_modified_paths() == []


def test_write_through_symlink_into_git_dir_is_rejected(ws: WorkspaceManager) -> None:
    (ws.root / "innocent").symlink_to(ws.root / ".git", target_is_directory=True)
    with pytest.raises(WorkspaceError, match=r"\.git"):
        ws.write_file("innocent/hooks/pre-commit", "#!/bin/sh\ntouch /tmp/pwned\n")
    assert not (ws.root / ".git" / "hooks" / "pre-commit").exists()


def test_write_through_file_symlink_into_git_dir_is_rejected(ws: WorkspaceManager) -> None:
    (ws.root / "cfg").symlink_to(ws.root / ".git" / "config")
    before = (ws.root / ".git" / "config").read_text()
    with pytest.raises(WorkspaceError, match=r"\.git"):
        ws.write_file("cfg", "[core]\n\thooksPath = /tmp\n")
    assert (ws.root / ".git" / "config").read_text() == before


def test_lookalike_names_are_still_writable(ws: WorkspaceManager) -> None:
    for path in (".gitignore", ".github/CODEOWNERS", "docs/.gitkeep", "git/config", ".git-blame-ignore-revs"):
        ws.write_file(path, "ok\n")
    assert ws.get_modified_paths() == sorted(
        [".git-blame-ignore-revs", ".github/CODEOWNERS", ".gitignore", "docs/.gitkeep", "git/config"]
    )


def test_modified_paths_are_canonical(ws: WorkspaceManager) -> None:
    """Recorded paths must be canonical so publication checks cannot be dodged."""
    ws.write_file("./src/../src/new.py", "y = 2\n")
    ws.write_file("/src/app.py", "x = 2\n")
    assert ws.get_modified_paths() == ["src/app.py", "src/new.py"]
    diff = ws.generate_diff()
    assert "b/src/new.py" in diff and "b/src/app.py" in diff


def test_existing_traversal_protection_preserved(ws: WorkspaceManager) -> None:
    with pytest.raises(WorkspaceError, match="traversal"):
        ws.write_file("../escape.txt", "x")
    assert not (ws.root.parent / "escape.txt").exists()


def test_existing_symlink_escape_protection_preserved(ws: WorkspaceManager, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws.root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="traversal"):
        ws.write_file("link/evil.txt", "x")
    assert not (outside / "evil.txt").exists()
