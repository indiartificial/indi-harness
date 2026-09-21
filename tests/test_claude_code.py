"""
tests/test_claude_code.py

The sender gate on `run_claude_code`, and the bits of the run flow that
can be checked without spending money.

A Claude Code session is the largest thing this harness can do: an agent
with a shell, in this directory, with no file held back. There is no
containment layer to test — that is the deliberate design — so what is
left to test is who gets in. These tests exist so that a later change
which loosens the gate fails loudly instead of quietly.

Like the rest of this directory, `propose_code_change` cannot edit this
file. A Claude Code session can, which is exactly why the gate is worth
pinning down here.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import claude_code
import config


def _cfg(**over):
    base = dict(
        enable_claude_code=True,
        claude_code_senders=("steve@example.com",),
        trusted_senders=("steve@example.com",),
        claude_code_timeout=900,
        claude_code_max_usd=5.0,
        max_claude_code_runs_per_reply=1,
        max_claude_code_task_chars=6000,
        claude_code_binary="claude",
        claude_code_model="",
        claude_code_auto_apply=True,
        self_code_test_command="/home/indi/venv/bin/python -m pytest tests -q",
        api_key="",
        dry_run=True,
    )
    base.update(over)
    return type("C", (), {**base, **{
        "may_run_claude_code": config.Config.may_run_claude_code,
        "is_trusted": config.Config.is_trusted,
        "validate_claude_code": config.Config.validate_claude_code,
    }})()


# --------------------------------------------------------------------------
# THE GATE
# --------------------------------------------------------------------------

def test_the_happy_path_is_the_only_happy_path():
    c = _cfg()
    assert c.may_run_claude_code("steve@example.com", True)
    assert c.may_run_claude_code("STEVE@Example.com", True)


def test_unauthenticated_mail_never_gets_a_session():
    """Not negotiable and not configurable: a From header is free text,
    and what is being granted is a root shell."""
    c = _cfg()
    assert not c.may_run_claude_code("steve@example.com", False)
    # Even with the global authentication requirement switched off.
    c = _cfg(require_authenticated_sender=False)
    assert not c.may_run_claude_code("steve@example.com", False)


def test_a_wildcard_grants_nothing():
    for pattern in ("*", "@example.com", "*@example.com"):
        c = _cfg(claude_code_senders=(pattern,),
                 trusted_senders=(pattern,))
        assert not c.may_run_claude_code("anyone@example.com", True), pattern


def test_trust_is_a_second_lock():
    c = _cfg(trusted_senders=())
    assert not c.may_run_claude_code("steve@example.com", True)


def test_a_stranger_is_refused():
    c = _cfg()
    assert not c.may_run_claude_code("eve@example.com", True)


def test_the_feature_switch_wins():
    c = _cfg(enable_claude_code=False)
    assert not c.may_run_claude_code("steve@example.com", True)


def test_an_empty_list_means_nobody():
    c = _cfg(claude_code_senders=())
    assert not c.may_run_claude_code("steve@example.com", True)


# --------------------------------------------------------------------------
# CONFIGURATION VALIDATION
# --------------------------------------------------------------------------

def test_a_sane_configuration_validates():
    assert _cfg().validate_claude_code() == []


def test_disabled_is_always_valid():
    assert _cfg(enable_claude_code=False, claude_code_senders=("*",)).validate_claude_code() == []


@pytest.mark.parametrize("over,fragment", [
    ({"claude_code_senders": ()}, "empty"),
    ({"claude_code_senders": ("*",)}, "wildcard"),
    ({"claude_code_senders": ("@example.com",)}, "wildcard"),
    ({"trusted_senders": ()}, "INDI_TRUSTED_SENDERS"),
    ({"trusted_senders": ("*",), "claude_code_senders": ("steve@example.com",)},
     "wildcard"),
    ({"claude_code_timeout": 5}, "TIMEOUT"),
    ({"claude_code_timeout": 99999}, "TIMEOUT"),
    ({"claude_code_max_usd": 0}, "MAX_USD"),
    ({"claude_code_max_usd": 1000}, "MAX_USD"),
    ({"max_claude_code_runs_per_reply": 0}, "RUNS_PER_REPLY"),
])
def test_unsafe_configurations_are_refused(over, fragment):
    problems = _cfg(**over).validate_claude_code()
    assert problems, over
    assert any(fragment in p for p in problems), problems


# --------------------------------------------------------------------------
# THE RUN
# --------------------------------------------------------------------------

def test_a_task_and_a_rationale_are_both_required():
    c = _cfg()
    with pytest.raises(claude_code.ClaudeCodeError):
        claude_code.run(c, {"task": "", "rationale": "because"})
    with pytest.raises(claude_code.ClaudeCodeError):
        claude_code.run(c, {"task": "do a thing", "rationale": "  "})


def test_an_overlong_task_is_refused():
    c = _cfg(max_claude_code_task_chars=10)
    with pytest.raises(claude_code.ClaudeCodeError, match="characters"):
        claude_code.run(c, {"task": "x" * 50, "rationale": "why"})


def test_a_missing_cli_is_a_clear_message():
    c = _cfg(claude_code_binary="definitely-not-a-real-binary-xyz")
    with pytest.raises(claude_code.ClaudeCodeError, match="PATH"):
        claude_code.run(c, {"task": "do a thing", "rationale": "why"})


# --------------------------------------------------------------------------
# FINDING THE CLI
#
# This cost a real session once: the CLI was installed, `which claude`
# found it from a login shell, and the harness still reported it missing,
# because systemd runs the service with a PATH that has no ~/.local/bin in
# it. Resolution now looks past PATH, and these pin that down.
# --------------------------------------------------------------------------

def test_path_wins_when_the_cli_is_on_it(monkeypatch):
    monkeypatch.setattr(claude_code.shutil, "which",
                        lambda n: "/usr/bin/claude" if n == "claude" else None)
    assert claude_code._binary(_cfg()) == "/usr/bin/claude"


def test_the_cli_is_found_off_path_where_the_installer_puts_it(tmp_path, monkeypatch):
    """The systemd case: nothing on PATH, but ~/.local/bin has it."""
    bindir = tmp_path / ".local" / "bin"
    bindir.mkdir(parents=True)
    installed = bindir / "claude"
    installed.write_text("#!/bin/sh\n")
    installed.chmod(0o755)

    monkeypatch.setattr(claude_code.shutil, "which", lambda n: None)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert claude_code._binary(_cfg()) == str(installed)


def test_a_non_executable_file_does_not_count(tmp_path, monkeypatch):
    bindir = tmp_path / ".local" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "claude").write_text("not executable")

    monkeypatch.setattr(claude_code.shutil, "which", lambda n: None)
    monkeypatch.setenv("HOME", str(tmp_path))

    with pytest.raises(claude_code.ClaudeCodeError, match="PATH"):
        claude_code._binary(_cfg())


def test_an_absolute_path_in_the_config_is_used_as_given(tmp_path, monkeypatch):
    target = tmp_path / "somewhere" / "claude"
    target.parent.mkdir()
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)

    monkeypatch.setattr(claude_code.shutil, "which", lambda n: None)
    assert claude_code._binary(_cfg(claude_code_binary=str(target))) == str(target)


def test_the_session_gets_a_path_it_can_work_with(tmp_path):
    """The same minimal-PATH problem one level down: the session inherits
    this process's environment, so without help its own shell has neither
    the CLI's directory nor the virtualenv holding pytest."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "claude"
    stub.write_text(
        '#!/bin/sh\n'
        'printf \'{"result": "%s", "total_cost_usd": 0}\\n\' "$PATH"\n'
    )
    stub.chmod(0o755)

    c = _cfg(claude_code_binary=str(stub), base_dir=tmp_path,
             claude_code_timeout=30)
    payload = claude_code._invoke(c, "a task")

    assert str(bindir) in payload["result"].split(":")
    assert str(Path(sys.executable).parent) in payload["result"].split(":")


def test_the_briefing_names_the_real_test_command(tmp_path):
    """A bare `python3` on the deployed box has no pytest, so the briefing
    has to quote the command the harness itself will grade the session on."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "claude"
    # Echo back whatever followed --append-system-prompt.
    stub.write_text(
        '#!/bin/sh\n'
        'while [ "$1" != "--append-system-prompt" ]; do shift; done\n'
        'python3 -c \'import json,sys; print(json.dumps({"result": sys.argv[1]}))\' "$2"\n'
    )
    stub.chmod(0o755)

    c = _cfg(claude_code_binary=str(stub), base_dir=tmp_path,
             claude_code_timeout=30,
             self_code_test_command="/some/venv/bin/python -m pytest tests -q")
    briefing = claude_code._invoke(c, "a task")["result"]

    assert "/some/venv/bin/python -m pytest tests -q" in briefing
    assert "{test_command}" not in briefing


def test_the_error_names_where_it_looked(monkeypatch):
    monkeypatch.setattr(claude_code.shutil, "which", lambda n: None)
    monkeypatch.setenv("HOME", "/nonexistent-home-xyz")
    with pytest.raises(claude_code.ClaudeCodeError) as exc:
        claude_code._binary(_cfg())
    assert "/nonexistent-home-xyz/.local/bin" in str(exc.value)
    assert "INDI_CLAUDE_CODE_BINARY" in str(exc.value)


def test_dry_run_does_not_start_a_session(monkeypatch):
    called = []
    monkeypatch.setattr(claude_code, "_invoke",
                        lambda *a, **k: called.append(a) or {})
    monkeypatch.setattr(claude_code, "_binary", lambda cfg: "/bin/true")
    c = _cfg(base_dir=Path("/tmp"))
    out = claude_code.run(c, {"task": "rewrite everything", "rationale": "why"})
    assert "dry-run" in out
    assert not called


def test_the_per_reply_limit_holds(monkeypatch):
    monkeypatch.setattr(claude_code, "run", lambda cfg, ti: "did it")
    runner = claude_code.ClaudeCodeRunner(_cfg(max_claude_code_runs_per_reply=1))
    text, err = runner.handle(claude_code.TOOL, {"task": "t", "rationale": "r"})
    assert not err and text == "did it"
    text, err = runner.handle(claude_code.TOOL, {"task": "t", "rationale": "r"})
    assert err and "limit" in text


def test_credential_files_are_watched_even_though_they_are_not_guarded(tmp_path):
    """They are gitignored, so the branch-and-revert mechanism cannot see
    or undo a change to them. The least we do is notice."""
    cfg = type("C", (), {"base_dir": tmp_path})()
    (tmp_path / ".env").write_text("KEY=1")
    before = claude_code._digest_sensitive(cfg)
    assert claude_code._sensitive_changes(before, before) == []
    (tmp_path / ".env").write_text("KEY=2")
    after = claude_code._digest_sensitive(cfg)
    assert claude_code._sensitive_changes(before, after) == [".env"]
    # A file appearing counts too.
    (tmp_path / "token.json").write_text("{}")
    later = claude_code._digest_sensitive(cfg)
    assert "token.json" in claude_code._sensitive_changes(after, later)


def test_every_credential_file_is_covered():
    """If .gitignore grows another secret, this list should grow with it."""
    ignored = (Path(__file__).resolve().parent.parent / ".gitignore").read_text()
    for name in (".env", "token.json", "credentials.json", "api_key.txt"):
        assert name in ignored, name
        assert name in claude_code.UNTRACKED_SENSITIVE, name


# --------------------------------------------------------------------------
# WHAT HAPPENS TO THE SESSION'S WORK
#
# A session that committed its own work once came back as "finished
# without changing any files", and the branch was force-deleted on the
# strength of an empty index. The work survived only in the reflog. These
# tests exist so the working tree is never again the only place the run
# looks for what a session did.
#
# Git is not mocked out here. The bug was a git-state bug — which ref has
# which commits — and a test that stubbed git would have passed happily
# while the branch went in the bin.
# --------------------------------------------------------------------------

def _run_git(repo, *args) -> str:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, check=True).stdout.strip()


def _branches(repo) -> list[str]:
    out = _run_git(repo, "branch", "--format=%(refname:short)")
    return [line.strip() for line in out.splitlines() if line.strip()]


def _session_branches(repo) -> list[str]:
    return [b for b in _branches(repo) if b.startswith("indi/claude/")]


def _file_on(repo, ref: str, name: str) -> str:
    """The contents of a file as of some ref, or "" if it isn't there."""
    proc = subprocess.run(["git", "show", f"{ref}:{name}"], cwd=str(repo),
                          capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


@pytest.fixture
def repo(tmp_path):
    """A one-commit repository standing in for the harness checkout."""
    root = tmp_path / "harness"
    root.mkdir()
    (root / "thing.py").write_text("original\n")
    _run_git(root, "init", "-q", "-b", "master")
    _run_git(root, "config", "user.email", "indi@localhost")
    _run_git(root, "config", "user.name", "Indi Artificial")
    _run_git(root, "add", "-A")
    _run_git(root, "commit", "-q", "-m", "Initial harness state")
    return root


def _run_session(monkeypatch, repo, session, *, tests_pass=True, **over):
    """Drive `claude_code.run` with `session` standing in for the CLI.

    `session` is handed the repo and may do to it whatever a real Claude
    Code session would: edit files, commit, both, or nothing at all."""
    def fake_invoke(cfg, task):
        session(repo)
        return {"result": "the session's own words"}

    monkeypatch.setattr(claude_code, "_binary", lambda cfg: "/bin/true")
    monkeypatch.setattr(claude_code, "_invoke", fake_invoke)
    monkeypatch.setattr(claude_code.self_code, "run_tests",
                        lambda cfg: (tests_pass, "test output"))
    cfg = _cfg(base_dir=repo, dry_run=False, **over)
    return claude_code.run(cfg, {"task": "do the thing", "rationale": "because"})


def _edit_and_commit(repo):
    (repo / "thing.py").write_text("changed by the session\n")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "The session's own commit")


# -- the case that was lost --------------------------------------------

def test_a_session_that_commits_its_own_work_is_not_an_empty_session(
        monkeypatch, repo):
    """The exact shape of the loss: clean tree, empty index, real work."""
    out = _run_session(monkeypatch, repo, _edit_and_commit)

    assert "without changing any files" not in out
    assert "Applied" in out
    assert "thing.py" in out
    assert _file_on(repo, "master", "thing.py") == "changed by the session\n"


def test_a_committed_session_never_has_its_branch_deleted(monkeypatch, repo):
    _run_session(monkeypatch, repo, _edit_and_commit, claude_code_auto_apply=False)

    branches = _session_branches(repo)
    assert len(branches) == 1, branches
    assert _file_on(repo, branches[0], "thing.py") == "changed by the session\n"


def test_leftovers_are_committed_on_top_of_what_the_session_committed(
        monkeypatch, repo):
    """Both halves survive: neither replaces the other."""
    def session(r):
        _edit_and_commit(r)
        (r / "extra.py").write_text("left in the working tree\n")

    out = _run_session(monkeypatch, repo, session)

    assert "Applied" in out
    assert _file_on(repo, "master", "thing.py") == "changed by the session\n"
    assert _file_on(repo, "master", "extra.py") == "left in the working tree\n"


def test_the_summary_describes_the_branch_not_the_last_commit(monkeypatch, repo):
    """Two commits, and the report has to mention both files. Summarising
    the last commit alone would name only the second."""
    def session(r):
        (r / "first.py").write_text("one\n")
        _run_git(r, "add", "-A")
        _run_git(r, "commit", "-q", "-m", "first")
        (r / "second.py").write_text("two\n")
        _run_git(r, "add", "-A")
        _run_git(r, "commit", "-q", "-m", "second")

    out = _run_session(monkeypatch, repo, session, claude_code_auto_apply=False)

    assert "first.py" in out
    assert "second.py" in out


def test_a_failing_suite_leaves_committed_work_on_its_branch(monkeypatch, repo):
    out = _run_session(monkeypatch, repo, _edit_and_commit, tests_pass=False)

    assert "FAILED" in out
    assert "thing.py" in out
    branches = _session_branches(repo)
    assert len(branches) == 1, branches
    assert _file_on(repo, branches[0], "thing.py") == "changed by the session\n"
    # And master is untouched, as it always was on red.
    assert _file_on(repo, "master", "thing.py") == "original\n"


def test_a_session_that_crashes_after_committing_keeps_its_branch(
        monkeypatch, repo):
    """Timing out is not the same as having done nothing."""
    def session(r):
        _edit_and_commit(r)
        raise RuntimeError("the session timed out")

    monkeypatch.setattr(claude_code, "_binary", lambda cfg: "/bin/true")
    monkeypatch.setattr(claude_code, "_invoke",
                        lambda cfg, task: session(repo))
    with pytest.raises(RuntimeError):
        claude_code.run(_cfg(base_dir=repo, dry_run=False),
                        {"task": "t", "rationale": "r"})

    branches = _session_branches(repo)
    assert len(branches) == 1, branches
    assert _file_on(repo, branches[0], "thing.py") == "changed by the session\n"
    assert _run_git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "master"


# -- the behaviour that was already right ------------------------------

def test_an_uncommitted_session_still_works_exactly_as_it_did(
        monkeypatch, repo):
    """The common case: the session edits files and leaves them."""
    out = _run_session(monkeypatch, repo,
                       lambda r: (r / "thing.py").write_text("edited\n"))

    assert "Applied" in out
    assert "thing.py" in out
    assert _file_on(repo, "master", "thing.py") == "edited\n"


def test_a_genuinely_empty_session_still_tidies_its_branch_away(
        monkeypatch, repo):
    """The one case where `branch -D` is still right, and still happens:
    nothing staged, nothing committed, nothing to lose."""
    out = _run_session(monkeypatch, repo, lambda r: None)

    assert "without changing any files" in out
    assert _session_branches(repo) == []


def test_a_crashed_empty_session_still_tidies_its_branch_away(
        monkeypatch, repo):
    def blow_up(cfg, task):
        raise RuntimeError("no")

    monkeypatch.setattr(claude_code, "_binary", lambda cfg: "/bin/true")
    monkeypatch.setattr(claude_code, "_invoke", blow_up)
    with pytest.raises(RuntimeError):
        claude_code.run(_cfg(base_dir=repo, dry_run=False),
                        {"task": "t", "rationale": "r"})

    assert _session_branches(repo) == []


def test_auto_apply_off_still_parks_the_branch(monkeypatch, repo):
    out = _run_session(monkeypatch, repo,
                       lambda r: (r / "thing.py").write_text("edited\n"),
                       claude_code_auto_apply=False)

    assert "Auto-apply is off" in out
    assert len(_session_branches(repo)) == 1
    assert _file_on(repo, "master", "thing.py") == "original\n"


# -- the count itself --------------------------------------------------

def test_commits_ahead_counts_what_the_branch_added(repo):
    cfg = _cfg(base_dir=repo)
    _run_git(repo, "checkout", "-q", "-b", "indi/claude/test")
    assert claude_code._commits_ahead(cfg, "master") == 0
    _edit_and_commit(repo)
    assert claude_code._commits_ahead(cfg, "master") == 1
    # And by name as well as through HEAD, which is what makes a session
    # that wandered off its own branch still countable.
    _run_git(repo, "checkout", "-q", "master")
    assert claude_code._commits_ahead(cfg, "master") == 0
    assert claude_code._commits_ahead(cfg, "master", "indi/claude/test") == 1


def test_an_uncountable_ref_is_assumed_to_hold_work(repo):
    """The answer that decides whether a branch gets deleted fails safe:
    git being unable to count is not evidence that there is nothing
    there."""
    assert claude_code._commits_ahead(_cfg(base_dir=repo), "no-such-ref") == 1
