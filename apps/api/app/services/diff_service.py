"""Service for parsing and bounding unified diffs into structured DiffResponse models."""

from __future__ import annotations

from app.schemas.task import DiffResponse, FileDiff

MAX_FILES_DEFAULT = 100
MAX_PATCH_LINES_PER_FILE = 2000
MAX_TOTAL_DIFF_BYTES = 10 * 1024 * 1024  # 10 MB limit for parsing safety


def parse_unified_diff(
    task_id: str,
    raw_diff: str | None,
    max_files: int = MAX_FILES_DEFAULT,
    max_patch_lines: int = MAX_PATCH_LINES_PER_FILE,
) -> DiffResponse:
    """Parse a unified diff into structured FileDiff models with bounds safety."""
    if not raw_diff or not raw_diff.strip():
        return DiffResponse(
            task_id=task_id,
            files_changed_count=0,
            additions=0,
            deletions=0,
            files=[],
        )

    # Protect against huge inputs
    if len(raw_diff.encode("utf-8")) > MAX_TOTAL_DIFF_BYTES:
        raw_diff = raw_diff[:MAX_TOTAL_DIFF_BYTES]

    lines = raw_diff.splitlines()

    # Split into per-file chunks
    # Files typically start with `diff --git` or `--- `
    file_chunks: list[list[str]] = []
    current_chunk: list[str] = []

    for line in lines:
        if line.startswith("diff --git ") or (
            line.startswith("--- ")
            and (
                not current_chunk
                or not any(chunk_line.startswith("--- ") for chunk_line in current_chunk)
            )
        ):
            if current_chunk and any(
                chunk_line.startswith("+++ ") or chunk_line.startswith("@@ ")
                for chunk_line in current_chunk
            ):
                file_chunks.append(current_chunk)
                current_chunk = []
        current_chunk.append(line)

    if current_chunk:
        file_chunks.append(current_chunk)

    files: list[FileDiff] = []
    total_additions = 0
    total_deletions = 0

    for chunk in file_chunks[:max_files]:
        from_file = ""
        to_file = ""
        file_additions = 0
        file_deletions = 0
        patch_lines: list[str] = []

        is_new_file = False
        is_deleted_file = False

        for line in chunk:
            if line.startswith("--- "):
                from_file = line[4:].strip()
                if from_file == "/dev/null":
                    is_new_file = True
                elif from_file.startswith("a/"):
                    from_file = from_file[2:]
            elif line.startswith("+++ "):
                to_file = line[4:].strip()
                if to_file == "/dev/null":
                    is_deleted_file = True
                elif to_file.startswith("b/"):
                    to_file = to_file[2:]
            elif line.startswith("new file mode"):
                is_new_file = True
            elif line.startswith("deleted file mode"):
                is_deleted_file = True
            elif line.startswith("+") and not line.startswith("+++"):
                file_additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                file_deletions += 1

            if len(patch_lines) < max_patch_lines:
                patch_lines.append(line)

        if len(chunk) > max_patch_lines:
            patch_lines.append(
                f"... [diff truncated: {len(chunk) - max_patch_lines} lines omitted]"
            )

        # Determine path and status
        if is_new_file:
            path = to_file or from_file
            status = "added"
        elif is_deleted_file:
            path = from_file or to_file
            status = "deleted"
        else:
            path = to_file or from_file or "unknown"
            status = "modified"

        # Clean any remaining /dev/null markers
        if path == "/dev/null":
            path = from_file if from_file != "/dev/null" else to_file

        files.append(
            FileDiff(
                path=path,
                status=status,
                additions=file_additions,
                deletions=file_deletions,
                patch="\n".join(patch_lines),
            )
        )
        total_additions += file_additions
        total_deletions += file_deletions

    return DiffResponse(
        task_id=task_id,
        files_changed_count=len(files),
        additions=total_additions,
        deletions=total_deletions,
        files=files,
    )
