"""Phase 10B.5 Stage 0 — host-side Git hardening regression tests.

These tests run real ``git`` against throw-away repositories that contain
attacker-controlled hooks and configuration.  Each attack is paired with a
control that runs plain, unhardened git to prove the attack is live, so a
passing test means SafeGitWrapper blocked it — not that the setup was inert.
"""

from __future__ import annotations

import base64
import logging
import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.github.git import GitError, SafeGitWrapper

TOKEN = "fake-installation-token-for-tests-0123456789abcdef"
BRANCH = "codeforge/task-1234"


# ── Helpers ────────────────────────────────────────────────────────────────────


def _plain_env(home: Path) -> dict[str, str]:
    """Environment for *unhardened* git used by setup and controls."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Setup",
        "GIT_AUTHOR_EMAIL": "setup@example.com",
        "GIT_COMMITTER_NAME": "Setup",
        "GIT_COMMITTER_EMAIL": "setup@example.com",
    }


def _git(cwd: Path, home: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", *args], cwd=cwd, env=_plain_env(home), capture_output=True, text=True, check=check
    )


def _install_hook(hooks_dir: Path, name: str, marker: Path) -> None:
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / name
    hook.write_text(f"#!/bin/sh\necho pwned > '{marker}'\nexit 0\n")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class Repo:
    """A cloned working copy plus its bare remote, as left by SafeGitWrapper.clone."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.home = base / "home"
        self.home.mkdir()
        self.remote = base / "remote.git"
        seed = base / "seed"
        seed.mkdir()
        _git(seed, self.home, "init", "-q", "-b", "main")
        (seed / "README.md").write_text("hello\n")
        _git(seed, self.home, "add", "README.md")
        _git(seed, self.home, "commit", "-q", "-m", "init")
        _git(base, self.home, "clone", "-q", "--bare", str(seed), str(self.remote))
        self.work = base / "work"
        _git(base, self.home, "clone", "-q", str(self.remote), str(self.work))
        self.base_sha = _git(self.work, self.home, "rev-parse", "HEAD").stdout.strip()
        self.markers = base / "markers"
        self.markers.mkdir()

    def marker(self, name: str) -> Path:
        return self.markers / name

    def config(self, key: str, value: str) -> None:
        _git(self.work, self.home, "config", "--local", key, value)

    def wrapper(self) -> SafeGitWrapper:
        return SafeGitWrapper(self.work, TOKEN)

    def remote_branches(self) -> str:
        return _git(self.remote, self.home, "branch", "--list").stdout


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


@pytest.fixture
def file_push(monkeypatch: pytest.MonkeyPatch, repo: Repo):  # type: ignore[no-untyped-def]
    """Let the wrapper push to the local bare remote so push-side hooks are reachable.

    Production only allows https to github.com; this widens the protocol
    allow-list for the test so we can prove pre-push hooks never run once
    git actually reaches the transport.
    """
    monkeypatch.setattr(SafeGitWrapper, "ALLOWED_PROTOCOLS", ("file",))

    def _make() -> SafeGitWrapper:
        w = repo.wrapper()
        w._remote_url = repo.remote.as_uri()
        return w

    return _make


# ── Hooks ──────────────────────────────────────────────────────────────────────


def test_malicious_pre_commit_hooks_do_not_execute(repo: Repo) -> None:
    hooks = repo.work / ".git" / "hooks"
    for name in ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "post-checkout"):
        _install_hook(hooks, name, repo.marker(name))

    w = repo.wrapper()
    w.checkout_new_branch(BRANCH, repo.base_sha)
    (repo.work / "a.txt").write_text("change\n")
    w.commit_files(["a.txt"], "CodeForge: change")

    assert sorted(p.name for p in repo.markers.iterdir()) == []

    # Control: the very same hooks fire under plain git.
    _git(repo.work, repo.home, "commit", "-q", "--allow-empty", "-m", "control")
    assert repo.marker("pre-commit").exists()
    assert repo.marker("commit-msg").exists()


def test_malicious_pre_push_hook_does_not_execute(repo: Repo, file_push) -> None:  # type: ignore[no-untyped-def]
    _install_hook(repo.work / ".git" / "hooks", "pre-push", repo.marker("pre-push"))

    w = file_push()
    w.checkout_new_branch(BRANCH, repo.base_sha)
    (repo.work / "a.txt").write_text("change\n")
    w.commit_files(["a.txt"], "CodeForge: change")
    w.push(BRANCH)

    assert BRANCH in repo.remote_branches()
    assert not repo.marker("pre-push").exists()

    # Control: plain git push runs the hook.
    _git(repo.work, repo.home, "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}-control")
    assert repo.marker("pre-push").exists()


def test_repository_hooks_path_is_refused(repo: Repo) -> None:
    evil_hooks = repo.base / "evil-hooks"
    _install_hook(evil_hooks, "pre-commit", repo.marker("pre-commit"))
    repo.config("core.hooksPath", str(evil_hooks))
    (repo.work / "a.txt").write_text("change\n")

    with pytest.raises(GitError, match="disallowed"):
        repo.wrapper().commit_files(["a.txt"], "CodeForge: change")
    assert not repo.marker("pre-commit").exists()


def test_global_and_system_config_are_ignored(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator/host gitconfig must not leak hooks or rewrites into the wrapper."""
    evil_hooks = repo.base / "global-hooks"
    _install_hook(evil_hooks, "pre-commit", repo.marker("global-pre-commit"))
    evil_cfg = repo.base / "evil.gitconfig"
    evil_cfg.write_text(f"[core]\n\thooksPath = {evil_hooks}\n")
    (repo.home / ".gitconfig").write_text(evil_cfg.read_text())
    monkeypatch.setenv("HOME", str(repo.home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(repo.home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(evil_cfg))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(evil_cfg))
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)

    w = repo.wrapper()
    w.checkout_new_branch(BRANCH, repo.base_sha)
    (repo.work / "a.txt").write_text("change\n")
    w.commit_files(["a.txt"], "CodeForge: change")
    assert not repo.marker("global-pre-commit").exists()

    # Control: git honouring that global config runs the hook.
    subprocess.run(  # noqa: S603
        ["git", "commit", "-q", "--allow-empty", "-m", "control"],
        cwd=repo.work,
        env={**_plain_env(repo.home), "GIT_CONFIG_GLOBAL": str(evil_cfg)},
        check=True,
        capture_output=True,
    )
    assert repo.marker("global-pre-commit").exists()


# ── core.fsmonitor ─────────────────────────────────────────────────────────────


def test_fsmonitor_cannot_execute_repository_program(repo: Repo) -> None:
    marker = repo.marker("fsmonitor")
    repo.config("core.fsmonitor", f"echo pwned > '{marker}'; false")
    (repo.work / "a.txt").write_text("change\n")
    w = repo.wrapper()

    with pytest.raises(GitError, match="disallowed"):
        w.has_changes()
    with pytest.raises(GitError, match="disallowed"):
        w.commit_files(["a.txt"], "CodeForge: change")
    assert not marker.exists()

    # Control: plain git status runs the fsmonitor program.
    _git(repo.work, repo.home, "status", "--porcelain", check=False)
    assert marker.exists()


def test_fsmonitor_override_holds_even_without_config_allowlist(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: the command-scope override alone neutralises fsmonitor."""
    monkeypatch.setattr(SafeGitWrapper, "_verify_repository_config", lambda self: None)
    marker = repo.marker("fsmonitor")
    repo.config("core.fsmonitor", f"echo pwned > '{marker}'; false")
    (repo.work / "a.txt").write_text("change\n")

    assert repo.wrapper().has_changes() is True
    assert not marker.exists()


def test_hooks_override_holds_even_without_config_allowlist(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SafeGitWrapper, "_verify_repository_config", lambda self: None)
    evil_hooks = repo.base / "evil-hooks"
    _install_hook(evil_hooks, "pre-commit", repo.marker("pre-commit"))
    repo.config("core.hooksPath", str(evil_hooks))

    w = repo.wrapper()
    w.checkout_new_branch(BRANCH, repo.base_sha)
    (repo.work / "a.txt").write_text("change\n")
    w.commit_files(["a.txt"], "CodeForge: change")
    assert not repo.marker("pre-commit").exists()


# ── URL rewriting and redirection of authenticated operations ──────────────────


@pytest.mark.parametrize("key", ["insteadOf", "pushInsteadOf"])
def test_url_rewrite_cannot_redirect_authenticated_push(
    repo: Repo, file_push, key: str  # type: ignore[no-untyped-def]
) -> None:
    attacker = repo.base / "attacker.git"
    _git(repo.base, repo.home, "init", "-q", "--bare", str(attacker))

    w = file_push()
    w.checkout_new_branch(BRANCH, repo.base_sha)
    # Planted after checkout, as the agent's test run could do mid-task.
    repo.config(f"url.{attacker.as_uri()}.{key}", repo.remote.as_uri())
    (repo.work / "a.txt").write_text("change\n")
    with pytest.raises(GitError, match="disallowed"):
        w.commit_files(["a.txt"], "CodeForge: change")
    with pytest.raises(GitError, match="disallowed"):
        w.push(BRANCH)

    assert _git(attacker, repo.home, "branch", "--list").stdout == ""
    assert BRANCH not in repo.remote_branches()


def test_url_rewrite_to_github_lookalike_is_refused(repo: Repo) -> None:
    repo.config("url.https://evil.example/.insteadOf", "https://github.com/")
    w = repo.wrapper()
    w._remote_url = "https://github.com/owner/repo.git"
    with patch("subprocess.run", wraps=subprocess.run) as run:
        with pytest.raises(GitError, match="disallowed"):
            w.push(BRANCH)
    # Only the config inspection ran; no authenticated command was started.
    for call in run.call_args_list:
        argv = call.args[0]
        assert "push" not in argv
        env = call.kwargs["env"]
        assert not any(TOKEN in v or "Authorization" in v for v in env.values())


def test_included_config_file_is_refused(repo: Repo) -> None:
    extra = repo.base / "extra.gitconfig"
    extra.write_text('[url "https://evil.example/"]\n\tinsteadOf = https://github.com/\n')
    repo.config("include.path", str(extra))
    with pytest.raises(GitError, match="disallowed"):
        repo.wrapper().has_changes()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("core.sshCommand", "touch /tmp/pwned"),
        ("core.askPass", "/tmp/evil-askpass"),
        ("core.editor", "touch /tmp/pwned"),
        ("core.pager", "touch /tmp/pwned"),
        ("credential.helper", "!touch /tmp/pwned"),
        ("http.proxy", "http://127.0.0.1:9"),
        ("http.sslVerify", "false"),
        ("http.https://github.com/owner/repo.git.sslVerify", "false"),
        ("http.extraHeader", "X-Evil: 1"),
        ("remote.origin.pushurl", "https://evil.example/repo.git"),
        ("filter.evil.clean", "touch /tmp/pwned"),
        ("diff.evil.textconv", "touch /tmp/pwned"),
        ("alias.status", "!touch /tmp/pwned"),
        ("protocol.allow", "always"),
        ("extensions.worktreeConfig", "true"),
        ("gpg.program", "/tmp/evil-gpg"),
        ("commit.gpgSign", "true"),
    ],
)
def test_dangerous_repository_config_is_refused(repo: Repo, key: str, value: str) -> None:
    repo.config(key, value)
    with pytest.raises(GitError, match="disallowed"):
        repo.wrapper().has_changes()


def test_local_identity_config_is_accepted(repo: Repo) -> None:
    """user.name/user.email are inert strings, common in real repositories."""
    repo.config("user.name", "Someone")
    repo.config("user.email", "someone@example.com")
    assert repo.wrapper().has_changes() is False


@pytest.mark.parametrize("key", ["user.signingKey", "user.useConfigOnly"])
def test_other_user_config_is_still_refused(repo: Repo, key: str) -> None:
    repo.config(key, "x")
    with pytest.raises(GitError, match="disallowed"):
        repo.wrapper().has_changes()


def test_clean_clone_config_is_accepted(repo: Repo) -> None:
    w = repo.wrapper()
    assert w.has_changes() is False
    w.checkout_new_branch(BRANCH, repo.base_sha)
    (repo.work / "a.txt").write_text("change\n")
    sha = w.commit_files(["a.txt"], "CodeForge: change")
    assert len(sha) == 40


def test_file_protocol_push_is_blocked_by_default(repo: Repo) -> None:
    """Without the test-only widening, only https transports are permitted."""
    w = repo.wrapper()
    w._remote_url = repo.remote.as_uri()
    w.checkout_new_branch(BRANCH, repo.base_sha)
    (repo.work / "a.txt").write_text("change\n")
    w.commit_files(["a.txt"], "CodeForge: change")
    with pytest.raises(GitError):
        w.push(BRANCH)
    assert BRANCH not in repo.remote_branches()


@pytest.mark.parametrize(
    ("owner", "name"),
    [
        ("..", "repo"),
        (".", "repo"),
        ("owner", ".."),
        ("", "repo"),
        ("owner", ""),
        ("evil.example@owner", "repo"),
        ("owner", "repo?x=1"),
        ("owner", "repo#frag"),
        ("owner", "a/b"),
        ("owner", "re po"),
        ("owner", "repo%2f..%2fx"),
        ("owner:pw", "repo"),
    ],
)
def test_clone_rejects_names_that_could_alter_destination(
    tmp_path: Path, owner: str, name: str
) -> None:
    with patch("subprocess.run") as run:
        with pytest.raises(GitError, match="Invalid repository"):
            SafeGitWrapper(tmp_path, TOKEN).clone(owner, name)
    run.assert_not_called()


def test_clone_accepts_valid_github_names(tmp_path: Path) -> None:
    calls = _capture(lambda: SafeGitWrapper(tmp_path, TOKEN).clone("my-org_1", "repo.name-2_x"))
    assert "https://github.com/my-org_1/repo.name-2_x.git" in calls[0][0]


def test_push_requires_known_remote(repo: Repo) -> None:
    with pytest.raises(GitError, match="clone"):
        repo.wrapper().push(BRANCH)


def test_parent_repository_is_not_discovered(tmp_path: Path) -> None:
    """A workspace nested inside another repo must not operate on the parent."""
    home = tmp_path / "home"
    home.mkdir()
    _git(tmp_path, home, "init", "-q")
    _git(tmp_path, home, "config", "core.fsmonitor", f"echo pwned > '{tmp_path / 'm'}'")
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(GitError):
        SafeGitWrapper(ws, TOKEN).has_changes()
    assert not (tmp_path / "m").exists()


# ── Environment and argv contract (no subprocess side effects) ─────────────────


def _capture(fn) -> list[tuple[list[str], dict[str, str]]]:  # type: ignore[no-untyped-def]
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((argv, kwargs["env"]))
        proc = MagicMock()
        proc.stdout = ""
        return proc

    with patch("subprocess.run", side_effect=fake_run):
        fn()
    return calls


def _config_pairs(env: dict[str, str]) -> dict[str, list[str]]:
    pairs: dict[str, list[str]] = {}
    for i in range(int(env.get("GIT_CONFIG_COUNT", "0"))):
        pairs.setdefault(env[f"GIT_CONFIG_KEY_{i}"], []).append(env[f"GIT_CONFIG_VALUE_{i}"])
    return pairs


def _all_operations(tmp_path: Path) -> list[tuple[list[str], dict[str, str]]]:
    w = SafeGitWrapper(tmp_path, TOKEN)
    calls = _capture(lambda: w.clone("owner", "repo"))
    calls += _capture(lambda: w.checkout_new_branch(BRANCH, "a" * 40))
    calls += _capture(w.has_changes)
    calls += _capture(lambda: w.commit_files(["a.txt"], "msg"))
    calls += _capture(lambda: w.push(BRANCH))
    return calls


def test_every_git_invocation_is_hardened(tmp_path: Path) -> None:
    calls = _all_operations(tmp_path)
    assert calls
    for argv, env in calls:
        assert argv[0] == "git"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_ALLOW_PROTOCOL"] == "https"
        assert env["GIT_CEILING_DIRECTORIES"]
        assert "HOME" not in env and "XDG_CONFIG_HOME" not in env
        cfg = _config_pairs(env)
        assert cfg["core.hooksPath"] == [os.devnull]
        assert cfg["core.fsmonitor"] == ["false"]
        assert cfg["protocol.allow"] == ["never"]
        assert cfg["protocol.https.allow"] == ["always"]
        assert cfg["credential.helper"] == [""]
        assert cfg["core.askPass"] == [""]
        assert cfg["http.followRedirects"] == ["false"]


def test_commit_and_push_skip_hooks_and_never_force(tmp_path: Path) -> None:
    calls = _all_operations(tmp_path)
    commits = [a for a, _ in calls if "commit" in a]
    pushes = [a for a, _ in calls if "push" in a]
    assert commits and all("--no-verify" in a for a in commits)
    assert len(pushes) == 1
    push = pushes[0]
    assert "--no-verify" in push
    assert not {"--force", "-f", "--force-with-lease", "--mirror", "--all"} & set(push)
    assert not any(a.startswith("+") for a in push)
    assert "origin" not in push
    assert "https://github.com/owner/repo.git" in push
    assert f"refs/heads/{BRANCH}:refs/heads/{BRANCH}" in push


def test_auth_header_is_scoped_to_github_and_only_on_network_operations(tmp_path: Path) -> None:
    calls = _all_operations(tmp_path)
    expected = "Authorization: Basic " + base64.b64encode(
        f"x-access-token:{TOKEN}".encode()
    ).decode()
    for argv, env in calls:
        assert TOKEN not in " ".join(argv)
        cfg = _config_pairs(env)
        assert "http.extraHeader" not in cfg  # never unscoped
        if "clone" in argv or "push" in argv:
            assert cfg["http.https://github.com/.extraHeader"] == [expected]
        else:
            assert "http.https://github.com/.extraHeader" not in cfg
            assert not any(TOKEN in v or expected in v for v in env.values())


# ── Credential redaction ───────────────────────────────────────────────────────


def test_git_failure_never_logs_token(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    w = SafeGitWrapper(tmp_path, TOKEN)
    w._remote_url = "https://github.com/owner/repo.git"
    b64 = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    leaky = (
        f"fatal: unable to access 'https://x-access-token:{TOKEN}@github.com/': 403\n"
        f"> Authorization: Basic {b64}\n"
    )

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        if "push" in argv:
            raise subprocess.CalledProcessError(128, argv, output=leaky, stderr=leaky)
        proc = MagicMock()
        proc.stdout = ""
        return proc

    caplog.set_level(logging.DEBUG)
    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(GitError) as exc:
            w.push(BRANCH)

    for text in (caplog.text, str(exc.value), repr(exc.value)):
        assert TOKEN not in text
        assert b64 not in text
    assert "***" in caplog.text
    # The raw CalledProcessError (with unredacted stderr) must not ride along
    # as the exception cause, where a traceback logger would print it.
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__ is True


def test_unexpected_git_error_never_exposes_token(tmp_path: Path) -> None:
    w = SafeGitWrapper(tmp_path, TOKEN)

    def boom(argv, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError(f"exec failed with env {kwargs['env']}")

    with patch("subprocess.run", side_effect=boom):
        with pytest.raises(GitError) as exc:
            w.clone("owner", "repo")
    assert TOKEN not in str(exc.value)
    assert base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode() not in str(exc.value)
    assert exc.value.__cause__ is None
