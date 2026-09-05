"""Tests for GitHub permission logic."""

from app.github.permissions import GitHubPermission, has_github_permission


def test_github_permissions():
    assert has_github_permission(GitHubPermission.READ, GitHubPermission.READ) is True
    assert has_github_permission(GitHubPermission.READ, GitHubPermission.WRITE) is True
    assert has_github_permission(GitHubPermission.WRITE, GitHubPermission.READ) is False
    assert has_github_permission(GitHubPermission.WRITE, GitHubPermission.WRITE) is True
