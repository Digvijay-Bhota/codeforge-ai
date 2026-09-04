"""Unit tests for WorkspaceManager.

Covers: path traversal protection, file listing, reading, writing,
deletion, search, and diff generation.
No LLM calls are made in this module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.workspace.manager import WorkspaceError, WorkspaceManager

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def workspace(tmp_path: Path) -> WorkspaceManager:
    """A fresh WorkspaceManager rooted at a temporary directory."""
    # Seed with a couple of files.
    (tmp_path / "hello.py").write_text("print('hello')\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "world.py").write_text("x = 1\n")
    return WorkspaceManager(tmp_path)


# ── Construction ──────────────────────────────────────────────────────────────


def test_constructor_raises_for_missing_root(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="does not exist"):
        WorkspaceManager(tmp_path / "nonexistent")


# ── Path traversal protection ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_path",
    [
        "../../etc/passwd",
        "../outside.txt",
        "sub/../../outside.txt",
    ],
)
def test_traversal_blocked_on_read(workspace: WorkspaceManager, bad_path: str) -> None:
    """Path traversal attempts must raise WorkspaceError before any I/O."""
    with pytest.raises(WorkspaceError):
        workspace.read_file(bad_path)


@pytest.mark.parametrize(
    "bad_path",
    [
        "../../evil.py",
        "../sibling.py",
    ],
)
def test_traversal_blocked_on_write(workspace: WorkspaceManager, bad_path: str) -> None:
    with pytest.raises(WorkspaceError):
        workspace.write_file(bad_path, "malicious")


# ── list_files ────────────────────────────────────────────────────────────────


def test_list_files_returns_all_seeded_files(workspace: WorkspaceManager) -> None:
    files = workspace.list_files()
    assert "hello.py" in files
    assert "sub/world.py" in files


def test_list_files_subdirectory(workspace: WorkspaceManager) -> None:
    files = workspace.list_files("sub")
    assert files == ["sub/world.py"]


def test_list_files_ignores_cache_dirs(tmp_path: Path) -> None:
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "cached.pyc").write_bytes(b"")
    (tmp_path / "main.py").write_text("")
    wm = WorkspaceManager(tmp_path)
    files = wm.list_files()
    assert not any("__pycache__" in f for f in files)
    assert "main.py" in files


def test_list_files_missing_path_raises(workspace: WorkspaceManager) -> None:
    with pytest.raises(WorkspaceError, match="does not exist"):
        workspace.list_files("nonexistent_dir")


# ── read_file ─────────────────────────────────────────────────────────────────


def test_read_file_returns_content(workspace: WorkspaceManager) -> None:
    content = workspace.read_file("hello.py")
    assert content == "print('hello')\n"


def test_read_file_missing_raises(workspace: WorkspaceManager) -> None:
    with pytest.raises(WorkspaceError, match="not found"):
        workspace.read_file("missing.py")


def test_read_file_size_limit(tmp_path: Path) -> None:
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (512 * 1024 + 1))
    wm = WorkspaceManager(tmp_path)
    with pytest.raises(WorkspaceError, match="size limit"):
        wm.read_file("big.bin")


# ── write_file ────────────────────────────────────────────────────────────────


def test_write_file_creates_new_file(workspace: WorkspaceManager, tmp_path: Path) -> None:
    workspace.write_file("new.py", "x = 42\n")
    assert (tmp_path / "new.py").read_text() == "x = 42\n"


def test_write_file_overwrites_existing(workspace: WorkspaceManager, tmp_path: Path) -> None:
    workspace.write_file("hello.py", "print('updated')\n")
    assert (tmp_path / "hello.py").read_text() == "print('updated')\n"


def test_write_file_creates_parents(workspace: WorkspaceManager, tmp_path: Path) -> None:
    workspace.write_file("deep/nested/file.py", "pass\n")
    assert (tmp_path / "deep" / "nested" / "file.py").exists()


def test_write_file_tracks_modification(workspace: WorkspaceManager) -> None:
    workspace.write_file("hello.py", "updated")
    assert "hello.py" in workspace.get_modified_paths()


def test_write_file_tracks_creation(workspace: WorkspaceManager) -> None:
    workspace.write_file("brand_new.py", "pass")
    assert "brand_new.py" in workspace.get_modified_paths()


# ── delete_file ───────────────────────────────────────────────────────────────


def test_delete_file_removes_file(workspace: WorkspaceManager, tmp_path: Path) -> None:
    workspace.delete_file("hello.py")
    assert not (tmp_path / "hello.py").exists()


def test_delete_file_tracks_deletion(workspace: WorkspaceManager) -> None:
    workspace.delete_file("hello.py")
    assert "hello.py" in workspace.get_modified_paths()


def test_delete_file_missing_raises(workspace: WorkspaceManager) -> None:
    with pytest.raises(WorkspaceError, match="not found"):
        workspace.delete_file("ghost.py")


# ── search_files ──────────────────────────────────────────────────────────────


def test_search_files_finds_match(workspace: WorkspaceManager) -> None:
    results = workspace.search_files("print")
    assert any(r["file"] == "hello.py" for r in results)


def test_search_files_no_match(workspace: WorkspaceManager) -> None:
    results = workspace.search_files("zzz_definitely_not_present_zzz")
    assert results == []


# ── diff generation ───────────────────────────────────────────────────────────


def test_diff_reflects_modification(workspace: WorkspaceManager) -> None:
    workspace.write_file("hello.py", "print('modified')\n")
    diff = workspace.generate_diff()
    assert "-print('hello')" in diff
    assert "+print('modified')" in diff


def test_diff_reflects_creation(workspace: WorkspaceManager) -> None:
    workspace.write_file("created.py", "new = True\n")
    diff = workspace.generate_diff()
    assert "+new = True" in diff


def test_diff_empty_when_no_changes(workspace: WorkspaceManager) -> None:
    assert workspace.generate_diff() == ""


def test_diff_reflects_deletion(workspace: WorkspaceManager) -> None:
    workspace.delete_file("hello.py")
    diff = workspace.generate_diff()
    assert "-print('hello')" in diff


# ── _get_action helper ────────────────────────────────────────────────────────


def test_get_action_created(workspace: WorkspaceManager) -> None:
    workspace.write_file("fresh.py", "pass")
    assert workspace._get_action("fresh.py") == "created"


def test_get_action_modified(workspace: WorkspaceManager) -> None:
    workspace.write_file("hello.py", "new")
    assert workspace._get_action("hello.py") == "modified"


def test_get_action_deleted(workspace: WorkspaceManager) -> None:
    workspace.delete_file("hello.py")
    assert workspace._get_action("hello.py") == "deleted"
