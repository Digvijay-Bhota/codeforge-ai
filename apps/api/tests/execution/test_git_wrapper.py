"""Tests for SafeGitWrapper."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.github.git import GitError, SafeGitWrapper


def test_push_safety():
    wrapper = SafeGitWrapper(Path("/tmp"), "fake-token")

    # Should reject arbitrary branches
    with pytest.raises(GitError, match="Refusing to push unsafe branch name"):
        wrapper.push("main")

    with pytest.raises(GitError, match="Refusing to push unsafe branch name"):
        wrapper.push("master")

    with pytest.raises(GitError, match="Refusing to push unsafe branch name"):
        wrapper.push("base-branch")

    # Should allow codeforge branch
    with patch("subprocess.run") as mock_run:
        mock_proc = MagicMock()
        mock_proc.stdout = ""
        mock_run.return_value = mock_proc
        wrapper.push("codeforge/task-1234")
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        # Prove no --force is used
        assert "--force" not in args
        assert "-f" not in args
        assert "origin" in args
        assert "codeforge/task-1234" in args

def test_clone_auth_not_in_argv():
    wrapper = SafeGitWrapper(Path("/tmp"), "fake-token")
    with patch("subprocess.run") as mock_run:
        mock_proc = MagicMock()
        mock_proc.stdout = ""
        mock_run.return_value = mock_proc
        wrapper.clone("owner", "repo")

        args = mock_run.call_args[0][0]
        env = mock_run.call_args[1].get("env", {})

        # Prove token is NOT in args
        args_str = " ".join(args)
        assert "fake-token" not in args_str
        assert wrapper._auth_header not in args_str

        # Prove token IS in environment config
        assert env.get("GIT_CONFIG_COUNT") == "1"
        assert env.get("GIT_CONFIG_KEY_0") == "http.extraHeader"
        assert env.get("GIT_CONFIG_VALUE_0") == wrapper._auth_header

def test_clone_no_credential_persistence(tmp_path):
    wrapper = SafeGitWrapper(tmp_path, "secret-token")

    with patch("subprocess.run") as mock_run:
        wrapper.clone("owner", "repo")

        args = mock_run.call_args[0][0]
        args_str = " ".join(args)

        # Ensure the clone URL does not contain the token
        assert "secret-token" not in args_str
        assert "x-access-token" not in args_str
        assert "https://github.com/owner/repo.git" in args_str
