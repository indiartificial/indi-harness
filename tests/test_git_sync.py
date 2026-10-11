"""
tests/test_git_sync.py

The end-of-run push to GitHub: real git, real repositories in a temp
directory, a bare one standing in for GitHub.
"""

import logging
import subprocess
from types import SimpleNamespace

import pytest

import git_sync


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(repo, name, text="x"):
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name)


@pytest.fixture
def repos(tmp_path, monkeypatch):
    monkeypatch.setattr(git_sync.clock, "staging", lambda: False)
    monkeypatch.delenv("INDI_GIT_AUTO_PUSH", raising=False)
    monkeypatch.setattr(git_sync, "_setting",
                        lambda name: __import__("os").environ.get(name, ""))
    hub = tmp_path / "hub.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "master", str(hub))
    server = tmp_path / "server"
    _git(tmp_path, "clone", "-q", str(hub), str(server))
    _git(server, "checkout", "-q", "-b", "master")
    _commit(server, "a.py")
    _git(server, "push", "-q", "-u", "origin", "master")
    return hub, server


def _cfg(server, **over):
    values = dict(base_dir=server, dry_run=False)
    values.update(over)
    return SimpleNamespace(**values)


def test_new_commits_are_pushed(repos):
    hub, server = repos
    _commit(server, "b.py")
    _commit(server, "c.py")
    assert git_sync.push_if_ahead(_cfg(server)) == "pushed 2 commit(s)"
    assert _git(hub, "rev-parse", "master") == _git(server, "rev-parse", "HEAD")


def test_nothing_to_push_does_nothing(repos):
    _, server = repos
    assert git_sync.push_if_ahead(_cfg(server)) == "GitHub is up to date"


def test_switches_that_turn_it_off(repos, monkeypatch):
    hub, server = repos
    _commit(server, "b.py")
    before = _git(hub, "rev-parse", "master")
    monkeypatch.setenv("INDI_GIT_AUTO_PUSH", "false")
    assert git_sync.push_if_ahead(_cfg(server)) == "auto-push off"
    monkeypatch.delenv("INDI_GIT_AUTO_PUSH")
    assert "dry run" in git_sync.push_if_ahead(_cfg(server, dry_run=True))
    monkeypatch.setattr(git_sync.clock, "staging", lambda: True)
    assert "staging" in git_sync.push_if_ahead(_cfg(server))
    assert _git(hub, "rev-parse", "master") == before


def test_a_review_branch_is_not_pushed(repos):
    hub, server = repos
    _git(server, "checkout", "-q", "-b", "indi/auto/x")
    _commit(server, "b.py")
    assert git_sync.push_if_ahead(_cfg(server)).startswith("no upstream")
    assert "indi/auto/x" not in _git(hub, "branch")


def test_a_diverged_github_is_never_overwritten_and_alerts_once(repos, tmp_path, caplog):
    hub, server = repos
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(hub), str(other))
    _commit(other, "theirs.py")
    _git(other, "push", "-q", "origin", "master")
    theirs = _git(hub, "rev-parse", "master")

    _commit(server, "ours.py")
    with caplog.at_level(logging.WARNING, logger="indi.git_sync"):
        first = git_sync.push_if_ahead(_cfg(server))
        second = git_sync.push_if_ahead(_cfg(server))
    assert first.startswith("push failed") and second.startswith("push failed")
    assert _git(hub, "rev-parse", "master") == theirs
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert git_sync._last_failed(server) == _git(server, "rev-parse", "--short", "HEAD")


def test_git_sync_is_protected_from_self_modification():
    import self_code
    assert "git_sync.py" in self_code.PROTECTED_FILES
