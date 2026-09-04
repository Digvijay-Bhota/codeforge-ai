from __future__ import annotations

import re

from app.workspace.manager import WorkspaceManager

from .models import RelevantFile, RepositoryContext, RepositoryMap
from .scanner import GitScanner, RepositoryScanner

MAX_RELEVANT_FILES = 20
MAX_METADATA_ITEMS = 50

def rank_files(
    task_description: str, repo_map: RepositoryMap, max_files: int = MAX_RELEVANT_FILES
) -> list[RelevantFile]:
    """Deterministically score files against the task description."""
    # Extract simple words > 3 chars
    words = {w for w in re.findall(r"\w+", task_description.lower()) if len(w) > 3}

    # Deterministic mapping for common conceptual relationships
    aliases = {
        "authentication": "auth",
        "authorization": "auth",
        "authenticated": "auth",
        "testing": "test",
        "tests": "test",
        "timeout": "timeout",
        "timeouts": "timeout",
    }

    expanded_words = set(words)
    for w in words:
        if w in aliases:
            expanded_words.add(aliases[w])

    scored_files: list[RelevantFile] = []

    all_known = set(repo_map.source_files + repo_map.test_files + repo_map.important_files)

    for path in all_known:
        score = 0
        reasons = []
        path_lower = path.lower()

        for word in expanded_words:
            if word in path_lower:
                score += 5
                reasons.append(f"matches keyword '{word}'")

        if path in repo_map.important_files:
            score += 1
            reasons.append("important file")

        if score > 0:
            scored_files.append(
                RelevantFile(path=path, score=score, reason=", ".join(reasons))
            )

    # Sort descending by score, then alphabetically
    scored_files.sort(key=lambda x: (-x.score, x.path))
    return scored_files[:max_files]

def build_repository_context(
    workspace: WorkspaceManager, task_description: str
) -> RepositoryContext:
    scanner = RepositoryScanner(workspace)
    repo_map = scanner.scan()

    git_scanner = GitScanner(workspace)
    git_meta = git_scanner.scan()

    relevant_files = rank_files(task_description, repo_map)

    return RepositoryContext(
        repository_map=repo_map,
        git_metadata=git_meta,
        relevant_files=relevant_files,
    )

def format_context_for_prompt(context: RepositoryContext) -> str:
    lines = []
    lines.append("## Repository Context")
    lines.append(f"Root: {context.repository_map.root}")

    if context.git_metadata.is_available:
        lines.append(f"Git Branch: {context.git_metadata.branch}")
        lines.append(f"Git Commit: {context.git_metadata.commit_sha}")
        lines.append(f"Git Dirty: {context.git_metadata.is_dirty}")

    lines.append("\n### Languages")
    if context.repository_map.languages:
        for lang, count in sorted(context.repository_map.languages.items()):
            lines.append(f"- {lang}: {count} files")
    else:
        lines.append("- None detected")

    lines.append("\n### Frameworks & Tools")
    lines.append(f"- Frameworks: {', '.join(context.repository_map.frameworks) or 'None detected'}")
    lines.append(f"- Package Managers: {', '.join(context.repository_map.package_managers) or 'None detected'}")

    lines.append("\n### Important Files")
    if context.repository_map.important_files:
        for f in context.repository_map.important_files[:MAX_METADATA_ITEMS]:
            lines.append(f"- {f}")
    else:
        lines.append("- None")

    lines.append("\n### Relevant Files (Predicted)")
    if context.relevant_files:
        for rf in context.relevant_files:
            lines.append(f"- {rf.path} (score: {rf.score}) - {rf.reason}")
    else:
        lines.append("- None")

    return "\n".join(lines)
