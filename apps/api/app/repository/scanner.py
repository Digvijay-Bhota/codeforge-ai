from __future__ import annotations

import os
import subprocess  # nosec B404
from collections import defaultdict
from pathlib import Path

from app.workspace.manager import WorkspaceManager

from .discovery import (
    detect_frameworks_and_package_managers,
    detect_language,
    is_important_file,
    is_test_file,
)
from .models import GitMetadata, RepositoryMap


class GitScanner:
    """Safely extracts deterministic Git metadata without an uncontrolled shell."""

    def __init__(self, workspace: WorkspaceManager) -> None:
        self.workspace = workspace

    def scan(self) -> GitMetadata:
        try:
            # Quick check if .git exists to avoid unnecessary subprocess calls
            git_dir = self.workspace.root / ".git"
            if not git_dir.is_dir():
                return GitMetadata(is_available=False)

            env = {"PATH": os.environ.get("PATH", "")}

            # Branch
            proc = subprocess.run(  # nosec B603 B607
                ["git", "branch", "--show-current"],
                cwd=self.workspace.root,
                capture_output=True,
                text=True,
                env=env,
                timeout=5,
            )
            branch = proc.stdout.strip() if proc.returncode == 0 else None

            # SHA
            proc = subprocess.run(  # nosec B603 B607
                ["git", "rev-parse", "HEAD"],
                cwd=self.workspace.root,
                capture_output=True,
                text=True,
                env=env,
                timeout=5,
            )
            sha = proc.stdout.strip() if proc.returncode == 0 else None

            # Dirty state
            proc = subprocess.run(  # nosec B603 B607
                ["git", "status", "--porcelain"],
                cwd=self.workspace.root,
                capture_output=True,
                text=True,
                env=env,
                timeout=5,
            )

            is_dirty = False
            modified = []
            untracked = []

            if proc.returncode == 0 and proc.stdout.strip():
                is_dirty = True
                for line in proc.stdout.splitlines():
                    if len(line) < 4:
                        continue
                    status = line[:2]
                    filepath = line[3:]
                    if status == "??":
                        untracked.append(filepath)
                    else:
                        modified.append(filepath)

            return GitMetadata(
                is_available=True,
                branch=branch,
                commit_sha=sha,
                is_dirty=is_dirty,
                modified_files=modified,
                untracked_files=untracked,
            )
        except Exception:
            return GitMetadata(is_available=False)

class RepositoryScanner:
    def __init__(self, workspace: WorkspaceManager) -> None:
        self.workspace = workspace

    def scan(self) -> RepositoryMap:
        try:
            all_files = self.workspace.list_files(".")
        except Exception:
            all_files = []

        languages: dict[str, int] = defaultdict(int)
        source_files = []
        test_files = []
        config_files: list[str] = []
        important_files = []
        directories = set()

        for path in all_files:
            directories.add(str(Path(path).parent))

            lang = detect_language(path)
            if lang:
                languages[lang] += 1

            important = is_important_file(path)
            if important:
                important_files.append(path)

            if is_test_file(path):
                test_files.append(path)
            elif lang and not important:
                source_files.append(path)

        frameworks, pms = detect_frameworks_and_package_managers(self.workspace)

        return RepositoryMap(
            root=str(self.workspace.root),
            languages=dict(languages),
            frameworks=frameworks,
            package_managers=pms,
            source_files=sorted(source_files),
            test_files=sorted(test_files),
            config_files=sorted(config_files),
            important_files=sorted(important_files),
            directories=sorted(list(directories)),
        )
