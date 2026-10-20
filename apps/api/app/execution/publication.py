"""Publication artifacts: the exact generated change set a human must approve (CF-05).

Pushing generated changes to the target repository runs that repository's CI
(push and same-repository pull_request workflows) with its secrets, and those
workflows execute whatever code the changes contain: tests, build scripts,
package scripts, dependencies.  No file filter can make that safe, so
generated changes are never pushed until a human with write access approves
them.

The generation job captures the changes as an artifact - repository, base
commit and the full content of every changed file - and stores it.  The
approval records the artifact's digest, a sha256 over a canonical encoding of
exactly those fields.  The publication job republishes the stored files onto
the same base commit, recomputes the digest from what it wrote, and only
pushes if it equals the approved one, so what is published is what was
reviewed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.workspace.manager import WorkspaceError, WorkspaceManager

# Bounds on what can be stored for review and republished.
MAX_PUBLICATION_FILES = 200
MAX_PUBLICATION_BYTES = 2 * 1024 * 1024

WRITE = "write"
DELETE = "delete"


class PublicationError(Exception):
    """Raised when an artifact cannot be captured, stored or republished safely."""


def artifact_digest(repository: str, base_sha: str, files: list[dict[str, Any]]) -> str:
    """sha256 over the canonical encoding of what will be published."""
    canonical = json.dumps(
        {"repository": repository, "base_sha": base_sha, "files": files},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_files(files: list[dict[str, Any]]) -> int:
    """Check structure and limits; return the total content size in bytes."""
    if not files:
        raise PublicationError("There are no changes to publish.")
    if len(files) > MAX_PUBLICATION_FILES:
        raise PublicationError(
            f"Too many changed files to review and publish ({len(files)} > {MAX_PUBLICATION_FILES})."
        )
    total = 0
    for entry in files:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "action", "content"}
            or not isinstance(entry["path"], str)
            or not entry["path"]
        ):
            raise PublicationError("Malformed artifact entry.")
        if entry["action"] == WRITE and isinstance(entry["content"], str):
            total += len(entry["content"].encode("utf-8"))
        elif not (entry["action"] == DELETE and entry["content"] is None):
            raise PublicationError("Malformed artifact entry.")
    paths = [entry["path"] for entry in files]
    if paths != sorted(paths) or len(set(paths)) != len(paths):
        raise PublicationError("Artifact paths must be unique and sorted.")
    if total > MAX_PUBLICATION_BYTES:
        raise PublicationError(
            f"Changed content is too large to review and publish ({total} > {MAX_PUBLICATION_BYTES} bytes)."
        )
    return total


def capture_artifact(
    workspace: WorkspaceManager, repository: str, base_branch: str, base_sha: str
) -> dict[str, Any]:
    """Capture the workspace's net changes as a reviewable, publishable artifact."""
    files = [
        {"path": path, "action": WRITE if content is not None else DELETE, "content": content}
        for path, content in workspace.get_changes()
    ]
    total = _validate_files(files)
    return {
        "repository": repository,
        "base_branch": base_branch,
        "base_sha": base_sha,
        "files": files,
        "digest": artifact_digest(repository, base_sha, files),
        "total_bytes": total,
    }


def verify_artifact(repository: str, base_sha: str, files: list[dict[str, Any]], digest: str) -> None:
    """Raise unless the stored artifact is well formed and still matches *digest*."""
    _validate_files(files)
    if artifact_digest(repository, base_sha, files) != digest:
        raise PublicationError("The stored artifact does not match its digest.")


def apply_artifact(workspace: WorkspaceManager, files: list[dict[str, Any]]) -> None:
    """Write the artifact's files into *workspace* (a checkout of the base commit).

    Goes through WorkspaceManager, so paths cannot escape the workspace or touch
    ``.git``.
    """
    try:
        for entry in files:
            if entry["action"] == WRITE:
                workspace.write_file(entry["path"], entry["content"])
            else:
                workspace.delete_file(entry["path"])
    except WorkspaceError as exc:
        raise PublicationError(f"Cannot apply the approved changes: {exc}") from None


def digest_of_workspace(
    workspace: WorkspaceManager, repository: str, base_sha: str
) -> str:
    """The digest of the changes actually present in *workspace* after applying."""
    files = [
        {"path": path, "action": WRITE if content is not None else DELETE, "content": content}
        for path, content in workspace.get_changes()
    ]
    return artifact_digest(repository, base_sha, files)
