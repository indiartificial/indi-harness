"""
git_sync.py

Pushes the harness's own commits to GitHub at the end of each run, so the
copy on GitHub keeps up with the server without anyone remembering to.

What gets there by this route: Indi's self-modifications once they are
merged, Claude Code sessions' merged work, and anything committed by hand
on the server and not pushed. Only the branch the checkout tracks is
pushed (master, in practice), and only by fast-forward. Branches Indi
leaves for review — tests failed, or auto-apply is off — stay on the
server.

Never forced. If GitHub has commits the server doesn't, the push is
refused, nothing is lost, and the operator hears about it once (an ERROR
line, which alerts.py turns into a phone notification) rather than every
five minutes. Fixing that needs a person: pull, look, push.

Off in staging (its remote is the live checkout, not GitHub), in dry
runs, and when INDI_GIT_AUTO_PUSH=false. Indi can read this file but not
change it (self_code.PROTECTED_FILES).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

import clock

log = logging.getLogger("indi.git_sync")

TIMEOUT_SECONDS = 60
STATE_FILE = "git_sync.json"


def _setting(name: str) -> str:
    try:
        import gmail_reads
        return gmail_reads._setting(name)
    except Exception:  # pragma: no cover
        return (os.environ.get(name) or "").strip()


def enabled() -> bool:
    return _setting("INDI_GIT_AUTO_PUSH").lower() not in ("0", "false", "no", "off")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Never wait on a prompt: a run is unattended, and a hung push would
    # hold the run lock until systemd kills it.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=15")
    return subprocess.run(["git", *args], cwd=str(repo), env=env,
                          capture_output=True, text=True, timeout=TIMEOUT_SECONDS)


def _state_path(repo: Path) -> Path:
    return repo / "data" / STATE_FILE


def _last_failed(repo: Path) -> str:
    try:
        return json.loads(_state_path(repo).read_text()).get("failed_head", "")
    except Exception:
        return ""


def _remember_failed(repo: Path, head: str) -> None:
    try:
        path = _state_path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"failed_head": head}))
    except Exception:
        log.debug("Could not record the failed push", exc_info=True)


def push_if_ahead(cfg) -> str:
    """Push the tracked branch if it has commits GitHub lacks.

    Returns a short description of what happened, for the log. Never
    raises.
    """
    try:
        if not enabled():
            return "auto-push off"
        if getattr(cfg, "dry_run", False):
            return "dry run: not pushing"
        if clock.staging():
            return "staging copy: not pushing"
        repo = Path(cfg.base_dir)
        if not (repo / ".git").exists():
            return "not a git checkout"

        branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        upstream = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
                        "@{u}")
        if branch in ("", "HEAD") or upstream.returncode != 0:
            # Detached, or a branch with no upstream (a review branch Indi
            # was left on): nothing of ours to push.
            return f"no upstream for {branch or 'HEAD'}"
        remote, _, remote_branch = upstream.stdout.strip().partition("/")

        ahead = _git(repo, "rev-list", "--count", "@{u}..HEAD")
        if ahead.returncode != 0 or ahead.stdout.strip() in ("", "0"):
            return "GitHub is up to date"
        count = ahead.stdout.strip()
        head = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()

        pushed = _git(repo, "push", "--quiet", remote, f"HEAD:{remote_branch}")
        if pushed.returncode == 0:
            log.info("Pushed %s commit(s) to %s/%s (now at %s).",
                     count, remote, remote_branch, head)
            if _last_failed(repo):
                _remember_failed(repo, "")
            return f"pushed {count} commit(s)"

        why = (pushed.stderr or pushed.stdout).strip().splitlines()
        why = why[-1] if why else "no output"
        if _last_failed(repo) != head:
            log.error("Could not push %s commit(s) to %s/%s: %s. The commits are "
                      "safe on the server; push by hand once fixed.",
                      count, remote, remote_branch, why)
            _remember_failed(repo, head)
        else:
            log.warning("Push to %s/%s still failing: %s", remote, remote_branch, why)
        return f"push failed: {why}"
    except subprocess.TimeoutExpired:
        log.warning("Push to GitHub timed out after %ss.", TIMEOUT_SECONDS)
        return "push timed out"
    except Exception:
        log.warning("Push to GitHub failed unexpectedly.", exc_info=True)
        return "push failed unexpectedly"
