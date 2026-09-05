"""Tests for GitHub integration models and validation."""

import pytest
from pydantic import ValidationError

from app.github.models import (
    CreatePullRequestRequest,
    GitHubBranch,
    GitHubRepository,
)


def test_valid_github_repository():
    repo = GitHubRepository(
        owner="Digvijay-Bhota",
        name="codeforge-ai",
        full_name="Digvijay-Bhota/codeforge-ai",
        default_branch="main",
        private=False,
        clone_url="https://github.com/Digvijay-Bhota/codeforge-ai.git",
        html_url="https://github.com/Digvijay-Bhota/codeforge-ai"
    )
    assert repo.owner == "Digvijay-Bhota"
    assert repo.name == "codeforge-ai"

def test_invalid_github_repository_names():
    with pytest.raises(ValidationError, match="Invalid characters"):
        GitHubRepository(
            owner="user/name",  # slashes not allowed in owner
            name="repo",
            full_name="user/name/repo",
            default_branch="main",
            private=False,
            clone_url="",
            html_url=""
        )

    with pytest.raises(ValidationError, match="Invalid characters"):
        GitHubRepository(
            owner="user",
            name="../repo",
            full_name="user/repo",
            default_branch="main",
            private=False,
            clone_url="",
            html_url=""
        )

def test_valid_github_branch():
    branch = GitHubBranch(name="feature/cool-stuff", sha="123456")
    assert branch.name == "feature/cool-stuff"

def test_invalid_github_branch():
    with pytest.raises(ValidationError, match="Path traversal"):
        GitHubBranch(name="feature/../../etc/passwd", sha="123")

def test_create_pr_head_base_same():
    with pytest.raises(ValidationError, match="head_branch and base_branch cannot be the same"):
        CreatePullRequestRequest(
            owner="owner",
            repo="repo",
            title="Update",
            head_branch="main",
            base_branch="main"
        )
