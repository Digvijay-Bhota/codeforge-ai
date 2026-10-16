"""Phase 10B.5 Stage 1A — GitScanner must not execute repository-controlled programs.

GitScanner backs the analysis stage and the agent's ``git.status`` tool.  Once
repository tests have run, ``.git/config`` may contain a ``core.fsmonitor``
program (or filters, includes, hooksPath ...).  Plain ``git status`` would run
it inside the trusted worker.  Each attack is paired with a control proving
it fires under unhardened git.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import app.mcp.tools.git  # noqa: F401  (registers git.status, as app.mcp.server does)
from app.github.git import SafeGitWrapper
from app.mcp.permissions import ToolPermission
from app.mcp.registry import registry
from app.repository.scanner import GitScanner
from app.workspace.manager import WorkspaceManager


def _env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Setup",
        "GIT_AUTHOR_EMAIL": "setup@example.com",
        "GIT_COMMITTER_NAME": "Setup",
        "GIT_COMMITTER_EMAIL": "setup@example.com",
    }


def _git(cwd: Path, home: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", *args], cwd=cwd, env=_env(home), capture_output=True, text=True, check=check
    )


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path]:
    """A clone-like working copy with one commit; returns (work, home)."""
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    _git(work, home, "init", "-q", "-b", "main")
    (work / "tracked.py").write_text("x = 1\n")
    _git(work, home, "add", "tracked.py")
    _git(work, home, "commit", "-q", "-m", "init")
    return work, home


def _plant_fsmonitor(work: Path, home: Path, marker: Path) -> None:
    _git(work, home, "config", "--local", "core.fsmonitor", f"echo pwned > '{marker}'; false")
    (work / "tracked.py").write_text("x = 2\n")  # make the index stale so status consults it


def test_scan_reports_metadata_for_clean_clone(repo: tuple[Path, Path]) -> None:
    work, home = repo
    (work / "tracked.py").write_text("x = 2\n")
    (work / "new.py").write_text("y = 1\n")
    meta = GitScanner(WorkspaceManager(work)).scan()

    assert meta.is_available is True
    assert meta.branch == "main"
    assert meta.commit_sha == _git(work, home, "rev-parse", "HEAD").stdout.strip()
    assert meta.is_dirty is True
    # " M tracked.py": the leading space is significant and must survive parsing.
    assert meta.modified_files == ["tracked.py"]
    assert meta.untracked_files == ["new.py"]


def test_scan_never_runs_repository_fsmonitor(repo: tuple[Path, Path], tmp_path: Path) -> None:
    work, home = repo
    marker = tmp_path / "fsmonitor-ran"
    _plant_fsmonitor(work, home, marker)

    meta = GitScanner(WorkspaceManager(work)).scan()

    assert meta.is_available is False  # tampered config: refuse, report no metadata
    assert not marker.exists()

    # Control: unhardened git status executes the planted program.
    _git(work, home, "status", "--porcelain", check=False)
    assert marker.exists()


def test_fsmonitor_override_holds_without_config_allowlist(
    repo: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: command-scope core.fsmonitor=false alone neutralises it."""
    monkeypatch.setattr(SafeGitWrapper, "_verify_repository_config", lambda self: None)
    work, home = repo
    marker = tmp_path / "fsmonitor-ran"
    _plant_fsmonitor(work, home, marker)

    meta = GitScanner(WorkspaceManager(work)).scan()

    assert meta.is_available is True
    assert meta.modified_files == ["tracked.py"]
    assert not marker.exists()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("filter.evil.clean", "touch {marker}"),
        ("core.hooksPath", "{base}"),
        ("include.path", "{base}/x.gitconfig"),
        ("core.pager", "touch {marker}"),
    ],
)
def test_scan_refuses_other_dangerous_repository_config(
    repo: tuple[Path, Path], tmp_path: Path, key: str, value: str
) -> None:
    work, home = repo
    marker = tmp_path / "ran"
    _git(work, home, "config", "--local", key, value.format(marker=marker, base=tmp_path))
    (work / ".gitattributes").write_text("*.py filter=evil\n")

    meta = GitScanner(WorkspaceManager(work)).scan()

    assert meta.is_available is False
    assert not marker.exists()


def test_scan_ignores_host_global_and_system_config(
    repo: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, home = repo
    marker = tmp_path / "global-fsmonitor"
    evil = tmp_path / "evil.gitconfig"
    evil.write_text(f"[core]\n\tfsmonitor = echo pwned > '{marker}'; false\n")
    (home / ".gitconfig").write_text(evil.read_text())
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(evil))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(evil))
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    (work / "tracked.py").write_text("x = 2\n")

    meta = GitScanner(WorkspaceManager(work)).scan()

    assert meta.is_available is True
    assert not marker.exists()

    # Control: git honouring that global config runs it.
    subprocess.run(  # noqa: S603
        ["git", "status", "--porcelain"],
        cwd=work,
        env={**_env(home), "GIT_CONFIG_GLOBAL": str(evil)},
        capture_output=True,
        check=False,
    )
    assert marker.exists()


def test_scan_does_not_discover_a_parent_repository(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _git(tmp_path, home, "init", "-q")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".git").mkdir()  # an empty, invalid .git directory
    marker = tmp_path / "parent-fsmonitor"
    _git(tmp_path, home, "config", "core.fsmonitor", f"echo pwned > '{marker}'")

    meta = GitScanner(WorkspaceManager(ws)).scan()

    assert meta.is_available is False
    assert not marker.exists()

    # Control: unhardened git walks up into the parent and runs its fsmonitor.
    (ws / "f.txt").write_text("x")
    _git(ws, home, "status", "--porcelain", check=False)
    assert marker.exists()


@pytest.mark.asyncio
async def test_agent_git_status_tool_never_runs_fsmonitor(
    repo: tuple[Path, Path], tmp_path: Path
) -> None:
    work, home = repo
    marker = tmp_path / "fsmonitor-ran"
    _plant_fsmonitor(work, home, marker)

    res = await registry.execute_tool(
        "git.status", {}, ToolPermission.READ, workspace=WorkspaceManager(work)
    )

    assert json.loads(res[0].text)["is_available"] is False
    assert not marker.exists()
