from __future__ import annotations

import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest

from app.repository.context import rank_files
from app.repository.scanner import GitScanner, RepositoryScanner
from app.workspace.manager import WorkspaceManager


@pytest.fixture
def repo_workspace(tmp_path: Path) -> Generator[WorkspaceManager, None, None]:
    # Setup mixed repo
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()

    (tmp_path / "README.md").write_text("# Test Repo")
    (tmp_path / "pyproject.toml").write_text("[tool.poetry]\ndependencies = {fastapi = \"*\"}")
    (tmp_path / "package.json").write_text('{"dependencies": {"react": "*"}}')

    (tmp_path / "src" / "main.py").write_text("print('hello')")
    (tmp_path / "src" / "auth.py").write_text("def login(): pass")
    (tmp_path / "src" / "utils.js").write_text("console.log('hi')")
    (tmp_path / "src" / "timeout.py").write_text("def set_timeout(): pass")

    (tmp_path / "tests" / "test_auth.py").write_text("def test_login(): pass")

    # ignored
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "test.js").write_text("ignore me")

    yield WorkspaceManager(tmp_path)

def test_repository_scanner(repo_workspace: WorkspaceManager) -> None:
    scanner = RepositoryScanner(repo_workspace)
    repo_map = scanner.scan()

    assert "Python" in repo_map.languages
    assert repo_map.languages["Python"] == 4 # main.py, auth.py, test_auth.py
    assert repo_map.languages["JavaScript"] == 1 # utils.js

    assert "FastAPI" in repo_map.frameworks
    assert "React" in repo_map.frameworks
    assert "poetry/pip" in repo_map.package_managers

    assert "src/auth.py" in repo_map.source_files
    assert "src/utils.js" in repo_map.source_files

    assert "tests/test_auth.py" in repo_map.test_files

    assert "README.md" in repo_map.important_files
    assert "pyproject.toml" in repo_map.important_files
    assert "package.json" in repo_map.important_files

    # node_modules should be ignored
    assert "node_modules/test.js" not in repo_map.source_files

def test_context_ranking(repo_workspace: WorkspaceManager) -> None:
    scanner = RepositoryScanner(repo_workspace)
    repo_map = scanner.scan()

    task_desc = "fix authentication timeout"
    ranked = rank_files(task_desc, repo_map)

    top_paths = [f.path for f in ranked]

    # "authentication" aliases to "auth", so auth.py scores high
    assert "src/auth.py" in top_paths
    assert "tests/test_auth.py" in top_paths

    # "timeout" explicitly matches timeout.py
    assert "src/timeout.py" in top_paths

    # auth.py (5 pts) and timeout.py (5 pts) should rank ABOVE README.md (1 pt)
    auth_idx = top_paths.index("src/auth.py")
    timeout_idx = top_paths.index("src/timeout.py")
    readme_idx = top_paths.index("README.md")

    assert auth_idx < readme_idx
    assert timeout_idx < readme_idx

    # Deterministic sorting tie-breaker:
    # Both score 5. 'src/auth.py' < 'src/timeout.py' alphabetically.
    assert auth_idx < timeout_idx

def test_git_scanner_unavailable(tmp_path: Path) -> None:
    # no .git dir
    wm = WorkspaceManager(tmp_path)
    scanner = GitScanner(wm)
    meta = scanner.scan()
    assert not meta.is_available

def test_git_scanner_available(tmp_path: Path) -> None:
    # create git repo
    subprocess.run(["git", "init"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)

    (tmp_path / "test.txt").write_text("test")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True)

    wm = WorkspaceManager(tmp_path)
    scanner = GitScanner(wm)
    meta = scanner.scan()

    assert meta.is_available
    assert meta.commit_sha
    assert not meta.is_dirty

    (tmp_path / "test.txt").write_text("modified")
    meta_dirty = scanner.scan()
    assert meta_dirty.is_dirty
