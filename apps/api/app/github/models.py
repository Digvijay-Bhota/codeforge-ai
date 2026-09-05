"""Strongly typed models for GitHub integration."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

# GitHub limits: owner/repo names are max 39 and 100 chars typically, alphanumeric + hyphens + dots + underscores.
# Ref: https://docs.github.com/en/get-started/getting-started-with-git/about-remote-repositories
_NAME_REGEX = re.compile(r"^[a-zA-Z0-9_.-]+$")
_BRANCH_REGEX = re.compile(r"^[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)*$")


def _validate_github_name(name: str) -> str:
    """Validate owner or repo name."""
    if not name or not name.strip():
        raise ValueError("Name cannot be empty")
    if not _NAME_REGEX.match(name):
        raise ValueError(f"Invalid characters in name: {name}")
    if ".." in name:
        raise ValueError(f"Path traversal detected in name: {name}")
    return name

def _validate_branch_name(name: str) -> str:
    """Validate branch name."""
    if not name or not name.strip():
        raise ValueError("Branch name cannot be empty")
    if not _BRANCH_REGEX.match(name):
        raise ValueError(f"Invalid characters in branch name: {name}")
    if ".." in name:
        raise ValueError(f"Path traversal detected in branch name: {name}")
    return name


class GitHubRepository(BaseModel):
    owner: str = Field(..., max_length=100)
    name: str = Field(..., max_length=100)
    full_name: str
    default_branch: str
    private: bool
    clone_url: str
    html_url: str

    @field_validator("owner", "name")
    @classmethod
    def validate_names(cls, v: str) -> str:
        return _validate_github_name(v)


class GitHubBranch(BaseModel):
    name: str = Field(..., max_length=255)
    sha: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return _validate_branch_name(v)


class GitHubCommit(BaseModel):
    sha: str
    message: str
    branch: str


class GitHubPullRequest(BaseModel):
    number: int
    title: str
    body: str
    head_branch: str
    base_branch: str
    html_url: str
    state: str


class CreateBranchRequest(BaseModel):
    owner: str
    repo: str
    branch_name: str = Field(..., max_length=255)
    base_sha: str

    @field_validator("owner", "repo")
    @classmethod
    def validate_repo_names(cls, v: str) -> str:
        return _validate_github_name(v)

    @field_validator("branch_name")
    @classmethod
    def validate_branch(cls, v: str) -> str:
        return _validate_branch_name(v)


class CreatePullRequestRequest(BaseModel):
    owner: str
    repo: str
    title: str = Field(..., min_length=1, max_length=255)
    body: str = ""
    head_branch: str
    base_branch: str

    @field_validator("owner", "repo")
    @classmethod
    def validate_repo_names(cls, v: str) -> str:
        return _validate_github_name(v)

    @field_validator("head_branch", "base_branch")
    @classmethod
    def validate_branches(cls, v: str) -> str:
        return _validate_branch_name(v)

    @field_validator("base_branch")
    @classmethod
    def validate_head_not_base(cls, v: str, info: Any) -> str:
        if "head_branch" in info.data and v == info.data["head_branch"]:
            raise ValueError("head_branch and base_branch cannot be the same")
        return v
