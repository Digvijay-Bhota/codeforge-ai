"""Publication artifacts: the exact generated change set a human must approve (CF-05).

Pushing generated changes to the target repository runs that repository's CI
(push and same-repository pull_request workflows) with its secrets, and those
workflows execute whatever code the changes contain: tests, build scripts,
package scripts, dependencies.  No file filter can make that safe, so
generated changes are never pushed until a human with write access approves
them.

The generation job captures the changes as an artifact - repository, base
branch and commit, the full content of every changed file, and the commit
message and pull request title and body that will be published - and stores
it.  The pull request text matters too: it contains agent output, and
workflows can read it (``github.event.pull_request.body``).  The approval
records the artifact's digest, a sha256 over a canonical encoding of exactly
those fields.  The publication job republishes the stored files onto the same
base commit, recomputes the digest from what it wrote, and only pushes if it
equals the approved one, so what is published is what was reviewed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from app.workspace.manager import WorkspaceError, WorkspaceManager

# Bounds on what can be stored for review and republished.
MAX_PUBLICATION_FILES = 200
MAX_PUBLICATION_BYTES = 2 * 1024 * 1024
MAX_PUBLICATION_TEXT_BYTES = 65536  # GitHub's limit for a pull request body

# Every field the digest covers, i.e. everything that publication writes.
BOUND_TEXT_FIELDS = ("repository", "base_branch", "base_sha", "commit_message",
                     "pull_request_title", "pull_request_body")  # fmt: skip

WRITE = "write"
DELETE = "delete"


class PublicationError(Exception):
    """Raised when an artifact cannot be captured, stored or republished safely."""


class PublicationUnauthorizedError(PublicationError):
    """The approver may no longer publish: inactive, or no write access now."""


def artifact_digest(artifact: Mapping[str, Any]) -> str:
    """sha256 over the canonical encoding of what will be published."""
    canonical = json.dumps(
        {**{name: artifact[name] for name in BOUND_TEXT_FIELDS}, "files": artifact["files"]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_text(artifact: Mapping[str, Any]) -> None:
    for name in BOUND_TEXT_FIELDS:
        value = artifact.get(name)
        if not isinstance(value, str) or "\x00" in value:
            raise PublicationError(f"Malformed artifact field: {name}.")
        if len(value.encode("utf-8")) > MAX_PUBLICATION_TEXT_BYTES:
            raise PublicationError(f"Artifact field is too long: {name}.")


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
            or "\x00" in entry["path"]
        ):
            raise PublicationError("Malformed artifact entry.")
        if entry["action"] == WRITE and isinstance(entry["content"], str):
            # PostgreSQL JSONB cannot store NUL characters.
            if "\x00" in entry["content"]:
                raise PublicationError(f"Cannot publish binary content: {entry['path']}")
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


def _workspace_files(workspace: WorkspaceManager) -> list[dict[str, Any]]:
    return [
        {"path": path, "action": WRITE if content is not None else DELETE, "content": content}
        for path, content in workspace.get_changes()
    ]


def capture_artifact(
    workspace: WorkspaceManager,
    *,
    repository: str,
    base_branch: str,
    base_sha: str,
    commit_message: str,
    pull_request_title: str,
    pull_request_body: str,
) -> dict[str, Any]:
    """Capture the workspace's net changes, and the text to publish them with, for review."""
    artifact: dict[str, Any] = {
        "repository": repository,
        "base_branch": base_branch,
        "base_sha": base_sha,
        "commit_message": commit_message,
        "pull_request_title": pull_request_title,
        "pull_request_body": pull_request_body,
        "files": _workspace_files(workspace),
    }
    _validate_text(artifact)
    artifact["total_bytes"] = _validate_files(artifact["files"])
    artifact["digest"] = artifact_digest(artifact)
    return artifact


def verify_artifact(artifact: Mapping[str, Any], digest: str) -> None:
    """Raise unless the stored artifact is well formed and still matches *digest*."""
    _validate_text(artifact)
    _validate_files(artifact["files"])
    if artifact_digest(artifact) != digest:
        raise PublicationError("The stored artifact does not match its digest.")


def stored_artifact(row: Any) -> dict[str, Any]:
    """The bound fields of a stored TaskPublicationArtifact row."""
    return {**{name: getattr(row, name) for name in BOUND_TEXT_FIELDS}, "files": row.files}


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


def digest_of_workspace(workspace: WorkspaceManager, artifact: Mapping[str, Any]) -> str:
    """The digest of *artifact* with its files replaced by what *workspace* now contains."""
    return artifact_digest({**artifact, "files": _workspace_files(workspace)})
