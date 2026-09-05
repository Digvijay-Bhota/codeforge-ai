from __future__ import annotations

from pydantic import BaseModel, Field


class GitMetadata(BaseModel):
    is_available: bool = False
    branch: str | None = None
    commit_sha: str | None = None
    is_dirty: bool = False
    modified_files: list[str] = Field(default_factory=list)
    untracked_files: list[str] = Field(default_factory=list)

class RelevantFile(BaseModel):
    path: str
    score: int
    reason: str

class RepositoryMap(BaseModel):
    root: str
    languages: dict[str, int] = Field(default_factory=dict)
    frameworks: list[str] = Field(default_factory=list)
    package_managers: list[str] = Field(default_factory=list)
    source_files: list[str] = Field(default_factory=list)
    test_files: list[str] = Field(default_factory=list)
    config_files: list[str] = Field(default_factory=list)
    important_files: list[str] = Field(default_factory=list)
    directories: list[str] = Field(default_factory=list)

class RepositoryContext(BaseModel):
    repository_map: RepositoryMap
    git_metadata: GitMetadata
    relevant_files: list[RelevantFile] = Field(default_factory=list)
