"""
claude_code.py

Hands Indi a full Claude Code session over its own harness.

`self_code.propose_code_change` edits one file at a time, refuses the
guard files, and cannot run anything. This is the other end of that
scale: a real coding agent, working in this directory, with the Bash tool
and permission checks turned off. It can read every file including the
credentials, run commands, install things, create and delete files, and
rewrite the permission tiers that decide who is allowed to ask it to.

WHAT THIS REMOVES
-----------------
`self_code.py` argues at length that a gate the model can rewrite is not
a gate, and keeps `config.py`, `local_tools.py`, `gmail_io.py`, the tests
and itself out of Indi's reach for that reason. This module removes that
property on purpose, at Steve's explicit instruction. Nothing here is
protected. A Claude Code session started from this tool can edit the
allowlists in `config.py` and the suite in `tests/` in the same run, and
the merge happens if the suite it just rewrote passes.

So the only thing standing between an inbound email and full control of
this machine is the sender gate: `INDI_CLAUDE_CODE_SENDERS`, which takes
named addresses only, requires the sender to be trusted, and requires the
message to have passed SPF/DKIM/DMARC. That check lives in `config.py`
and `check_and_reply.py` — and yes, a session started through this tool
can edit both. Treat this tool as equivalent to giving its senders an
unsupervised root shell, because that is what it is.

WHAT IS STILL TRUE
------------------
Not guardrails, just properties of the mechanism worth knowing:

  * Work happens on a branch. The session runs against a fresh
    `indi/claude/<timestamp>` branch, the test suite runs after it, and
    the branch merges only on green. On red the branch is left for
    inspection and the working tree goes back to where it was.
  * Nothing reloads live. Changes land in files that the *next* scheduled
    run imports. A session that breaks the harness costs one cycle.
  * Untracked files are outside all of that. `.env`, `api_key.txt`,
    `token.json` and `credentials.json` are gitignored, so a change to
    them is neither visible in the diff nor undone by abandoning the
    branch. They are hashed before and after and any change is reported —
    reported, not prevented.
  * There is a spend ceiling (`--max-budget-usd`) and a wall-clock
    timeout, and the run is recorded in the cost ledger.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import costs
import self_code

log = logging.getLogger("indi.claudecode")

TOOL = "run_claude_code"

# Gitignored, therefore invisible to the branch-and-revert mechanism.
# Hashed either side of a session so a change at least gets named.
UNTRACKED_SENSITIVE = (".env", "api_key.txt", "token.json", "credentials.json")

# Handed to the session as extra system prompt. This is context, not
# containment — the session can ignore every word of it. `{test_command}`
# is filled in from the config rather than hardcoded: a bare `python3` on
# this box has no pytest, so naming the wrong command would send the
# session chasing an import error it did not cause.
BRIEFING = """\
You are running as Claude Code inside Indi's own harness, at the request of \
Indi itself, which is an email agent whose source this directory is.

Context you need:
  * Changes take effect on Indi's NEXT scheduled run, not this one. There \
is no live reload, so you cannot test your change end to end.
  * `{test_command}` must pass when you are done. Your work is \
committed to a branch and merged only if it does. Use that command exactly; \
a bare `python3` is not the interpreter this harness runs under and does not \
have pytest installed.
  * `.env`, `api_key.txt`, `token.json` and `credentials.json` hold live \
credentials. Read or change them only if the task actually requires it; \
they are outside version control, so a mistake there is not revertible.
  * Match the surrounding style. This codebase explains its reasoning in \
prose and expects the same of new code.
  * If the task turns out to be a bad idea, do not do it. Say so instead — \
your final message is what gets reported back.
"""


class ClaudeCodeError(Exception):
    pass


# Where the CLI installs itself, searched when PATH doesn't have it. See
# `_binary` for why PATH alone cannot be trusted in this process.
CLI_SEARCH_DIRS = (
    "~/.local/bin",         # the native installer's default
    "~/.claude/local",      # older local installs
    "~/.npm-global/bin",    # npm with a user prefix
    "/usr/local/bin",       # npm -g as root
    "/opt/homebrew/bin",    # macOS
)


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _git(cfg, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cfg.base_dir),
                          capture_output=True, text=True, timeout=timeout)


def _commits_ahead(cfg, base: str, ref: str = "HEAD") -> int:
    """How many commits `ref` has that `base` does not.

    The other half of "did the session change anything". Looking at the
    index alone answers only "is there uncommitted work", and a session
    that committed its own work leaves none: clean tree, empty index,
    every line of the work sitting in commits. Reading that as an empty
    session and force-deleting the branch is how a day's work once ended
    up reachable only from the reflog.

    A failed count reports 1 rather than 0 on purpose. Everything that
    consults this treats a zero as permission to throw the branch away,
    and git being unable to answer is not evidence that there is nothing
    there."""
    out = _git(cfg, "rev-list", "--count", f"{base}..{ref}")
    if out.returncode != 0:
        log.warning("Could not count commits on %s against %s: %s", ref, base,
                    (out.stderr or out.stdout).strip()[:200])
        return 1
    try:
        return int(out.stdout.strip())
    except ValueError:
        return 1


def _digest_sensitive(cfg) -> dict[str, str]:
    out = {}
    for name in UNTRACKED_SENSITIVE:
        path = cfg.base_dir / name
        if path.exists():
            out[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            out[name] = "(absent)"
    return out


def _sensitive_changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return [name for name in UNTRACKED_SENSITIVE if before.get(name) != after.get(name)]


def _report(*parts: str) -> str:
    """Join the paragraphs of a result, dropping the ones that turned out
    to be empty. Several are conditional — the session's own message, the
    credential-file note — and blank-joining them leaves a result full of
    gaps that Indi then has to describe to someone."""
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def _binary(cfg) -> str:
    """Resolve the CLI to an absolute path, looking past PATH if it has to.

    `shutil.which` on its own is not enough here. The harness runs from
    systemd, which hands the service the bare
    /usr/local/sbin:...:/bin and nothing else — no `~/.local/bin`, which
    is exactly where the native installer puts `claude`. The CLI is then
    on PATH in the service user's login shell and missing from the
    service, and the failure reads as "it isn't installed" when it is."""
    name = cfg.claude_code_binary

    found = shutil.which(name)
    if found:
        return found

    # An explicit path in the config first, then the places the various
    # installers actually use.
    candidates = [Path(name).expanduser()]
    if os.sep not in name:
        candidates += [Path(d).expanduser() / name for d in CLI_SEARCH_DIRS]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            log.info("Claude Code CLI is not on PATH; using %s.", candidate)
            return str(candidate)

    looked = ", ".join(str(Path(d).expanduser()) for d in CLI_SEARCH_DIRS)
    raise ClaudeCodeError(
        f"The Claude Code CLI ({name}) isn't on PATH, and isn't in any of "
        f"the usual install locations either ({looked}). Steve needs to "
        "install it, or set INDI_CLAUDE_CODE_BINARY to its absolute path. "
        "Worth knowing: the harness runs under systemd with a minimal PATH, "
        "so the CLI being on PATH in a login shell doesn't mean it is on "
        "PATH here."
    )


def _invoke(cfg, task: str) -> dict:
    """Run one headless Claude Code session. Returns the parsed JSON result."""
    binary = _binary(cfg)
    args = [
        binary,
        "-p", task,
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--append-system-prompt",
        BRIEFING.format(test_command=cfg.self_code_test_command),
        "--max-budget-usd", f"{cfg.claude_code_max_usd:.2f}",
    ]
    if cfg.claude_code_model:
        args += ["--model", cfg.claude_code_model]

    env = dict(os.environ)
    # The CLI's own credential store is separate from the harness's. Give
    # it the harness key explicitly so this doesn't depend on whatever
    # `claude login` last wrote.
    if cfg.api_key:
        env["ANTHROPIC_API_KEY"] = cfg.api_key

    # The session inherits this process's PATH, which under systemd is the
    # minimal one — no `pytest`, and none of the tools sitting next to
    # `claude`. Add the CLI's directory and the interpreter's, appended so
    # that nothing already on PATH gets shadowed by them.
    path = env.get("PATH", os.defpath).split(os.pathsep)
    for extra in (str(Path(binary).parent),
                  str(Path(sys.executable).parent)):
        if extra not in path:
            path.append(extra)
    env["PATH"] = os.pathsep.join(path)

    log.warning("CLAUDE CODE starting in %s (timeout %ss, ceiling $%.2f): %s",
                cfg.base_dir, cfg.claude_code_timeout, cfg.claude_code_max_usd,
                task[:300])
    try:
        proc = subprocess.run(args, cwd=str(cfg.base_dir), env=env,
                              capture_output=True, text=True,
                              timeout=cfg.claude_code_timeout)
    except subprocess.TimeoutExpired:
        raise ClaudeCodeError(
            f"The Claude Code session ran past {cfg.claude_code_timeout}s and "
            "was killed. Any edits it had already written are still on the "
            "branch; nothing was merged."
        )

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    log.info("CLAUDE CODE exited %s (%d bytes stdout)", proc.returncode, len(stdout))

    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        # A non-JSON stdout means the CLI fell over before it got going —
        # a bad flag, a missing key, a refused workspace. Its stderr is
        # the only useful thing we have.
        raise ClaudeCodeError(
            "The Claude Code session produced no usable result "
            f"(exit {proc.returncode}). Output:\n"
            f"{(stderr or stdout)[-1500:]}"
        )
    if isinstance(payload, list):  # defensive: some versions wrap in a list
        payload = payload[-1] if payload else {}
    if not isinstance(payload, dict):
        raise ClaudeCodeError(
            "The Claude Code session returned something this harness doesn't "
            f"understand (exit {proc.returncode}): {str(payload)[:500]}"
        )
    payload.setdefault("_returncode", proc.returncode)
    return payload


def _record_cost(cfg, payload: dict, task: str) -> float | None:
    """Put the session's spend in the ledger. The CLI reports dollars
    directly, so this bypasses the token pricing in costs.py."""
    usd = payload.get("total_cost_usd")
    if not isinstance(usd, (int, float)):
        return None
    try:
        costs.record_usd("claude_code", cfg.claude_code_model or "claude-code",
                         float(usd), note=f"claude code: {task[:120]}")
    except Exception:
        log.exception("Could not record the Claude Code session cost.")
    return float(usd)


# ---------------------------------------------------------------------------
# TOOL SCHEMA
# ---------------------------------------------------------------------------

def schemas(cfg) -> list[dict]:
    return [{
        "name": TOOL,
        "description": (
            "Run a full Claude Code session on your own harness. Unlike "
            "propose_code_change, this is a coding agent with a shell: it can "
            "work across several files at once, create and delete files, run "
            "commands and tests, and change any part of the harness including "
            "the permission checks and the test suite. Nothing is off limits. "
            "\n\nThe session works on a git branch and is merged only if the "
            "test suite passes afterwards; the result takes effect on your "
            "next run, not this one. It costs real money and takes minutes, so "
            "use it for work that genuinely needs a coding agent and use "
            "propose_code_change for a single-file edit. Describe the task the "
            "way you would to a competent engineer who has not seen this "
            "codebase: what to change, and what 'done' looks like."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "The full instruction for the session, including any "
                        "constraints and what the finished state should be."
                    ),
                },
                "rationale": {
                    "type": "string",
                    "description": "Why this is worth a full coding session.",
                },
            },
            "required": ["task", "rationale"],
        },
    }]


# ---------------------------------------------------------------------------
# THE RUN
# ---------------------------------------------------------------------------

def run(cfg, tool_input: dict) -> str:
    task = (tool_input.get("task") or "").strip()
    rationale = (tool_input.get("rationale") or "").strip()

    if not task:
        raise ClaudeCodeError("No task given; describe what the session should do.")
    if not rationale:
        raise ClaudeCodeError("A rationale is required for a Claude Code session.")
    if len(task) > cfg.max_claude_code_task_chars:
        raise ClaudeCodeError(
            f"That task is {len(task)} characters against a limit of "
            f"{cfg.max_claude_code_task_chars}. Say it more briefly."
        )

    _binary(cfg)  # fail early and clearly if the CLI isn't there

    if cfg.dry_run:
        return (f"[dry-run] Would run Claude Code in {cfg.base_dir}.\n"
                f"Task: {task[:1000]}")

    self_code.ensure_repo(cfg)

    dirty = _git(cfg, "status", "--porcelain").stdout.strip()
    if dirty:
        raise ClaudeCodeError(
            "The harness working tree has uncommitted changes. Refusing to "
            "start a session on top of them — Steve should commit or discard "
            "them first."
        )

    base = _git(cfg, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "master"
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    branch = f"indi/claude/{stamp}"

    created = _git(cfg, "checkout", "-q", "-b", branch)
    if created.returncode != 0:
        raise ClaudeCodeError(
            f"Could not create branch {branch}: "
            f"{(created.stderr or created.stdout)[:300]}"
        )

    before = _digest_sensitive(cfg)
    try:
        payload = _invoke(cfg, task)
    except Exception:
        # Abandon whatever the session had left uncommitted and go back.
        # Commits are a different matter: a session that timed out or
        # crashed after committing still did the work, and deleting the
        # branch would be the same silent loss as the one guarded against
        # further down. Count before moving off the branch, and if there
        # is anything on it, leave it standing for Steve to look at.
        committed = _commits_ahead(cfg, base)
        _git(cfg, "checkout", "-q", "--", ".")
        _git(cfg, "clean", "-qfd")       # -x omitted: never touches .env & co.
        _git(cfg, "checkout", "-q", base)
        if committed:
            log.warning("The session failed after committing %d change(s); "
                        "branch %s is left in place.", committed, branch)
        else:
            _git(cfg, "branch", "-qD", branch)
        raise
    after = _digest_sensitive(cfg)

    usd = _record_cost(cfg, payload, task)
    answer = str(payload.get("result") or "").strip()
    spend = f" (${usd:.2f})" if usd is not None else ""

    notes = []
    touched = _sensitive_changes(before, after)
    if touched:
        # Outside git, so this is a statement of fact, not something the
        # merge decision can act on.
        log.warning("CLAUDE CODE changed untracked credential files: %s",
                    ", ".join(touched))
        notes.append(
            "NOTE: the session changed these files, which are outside version "
            f"control and were not reverted: {', '.join(touched)}."
        )

    # "Did the session change anything" is two questions, not one.
    #
    # The index answers only the first: is there uncommitted work. The
    # briefing tells the session its work will be committed to a branch,
    # and it has a shell, so a session that commits as it goes is
    # ordinary rather than exceptional — and it leaves nothing at all in
    # the index. Asking the index alone therefore reads a finished piece
    # of work as an empty session, and the empty-session path below ends
    # in `branch -D`, which will happily throw away commits. That is not
    # hypothetical; it has already cost one session's work.
    #
    # So the commits are counted too, and the branch is only deleted when
    # both answers are no. The branch ref is counted as well as HEAD: a
    # session that committed and then checked something else out would
    # otherwise look empty from HEAD, which is the same mistake wearing a
    # different hat.
    _git(cfg, "add", "-A")
    staged = _git(cfg, "diff", "--cached", "--stat").stdout.strip()
    committed = _commits_ahead(cfg, base)
    on_branch = _commits_ahead(cfg, base, branch)

    if not staged and not committed and not on_branch:
        _git(cfg, "checkout", "-q", base)
        # Provably empty by both measures, so there is nothing to lose.
        # This is the only place a branch is deleted after a session that
        # ran to completion.
        _git(cfg, "branch", "-qD", branch)
        return _report(
            f"The Claude Code session finished without changing any files{spend}.",
            f"It reported:\n{answer[:3000]}" if answer else "",
            *notes,
        )

    if staged:
        # Leftovers go on top of whatever the session committed itself,
        # never instead of it. When it committed nothing this is the whole
        # of the work and reads exactly as it always did; when it
        # committed as it went, this is the remainder, and the message
        # says so rather than claiming to be the change.
        leftover = ("Working-tree changes left over by a session that "
                    "committed its own work.\n\n") if committed else ""
        _git(cfg, "commit", "-q", "-m",
             f"Indi (Claude Code): {rationale[:120]}\n\n{leftover}{task[:1500]}")

    # What gets reported back is the branch against base, not the commit
    # made a moment ago. With the session committing its own work that
    # last commit is the leftovers — or there is no new commit at all —
    # and neither describes what merging this branch would actually
    # bring in, which is the only thing the summary is for.
    summary = _git(cfg, "diff", "--stat", f"{base}...HEAD").stdout.strip()

    passed, output = self_code.run_tests(cfg)
    if not passed:
        _git(cfg, "checkout", "-q", base)
        return _report(
            f"The Claude Code session made changes{spend} but the test suite "
            f"FAILED afterwards, so nothing was merged. The work is on branch "
            f"{branch}.",
            f"Changed:\n{summary}",
            f"It reported:\n{answer[:1500]}" if answer else "",
            f"Test output:\n{output[-1500:]}",
            *notes,
        )

    if not cfg.claude_code_auto_apply:
        _git(cfg, "checkout", "-q", base)
        return _report(
            f"The Claude Code session passed the tests{spend} and is waiting on "
            f"branch {branch}. Auto-apply is off, so Steve merges it.",
            f"Changed:\n{summary}",
            f"It reported:\n{answer[:1500]}" if answer else "",
            *notes,
        )

    _git(cfg, "checkout", "-q", base)
    merge = _git(cfg, "merge", "--no-ff", "-q", "-m",
                 f"Merge Indi Claude Code session: {rationale[:100]}", branch)
    if merge.returncode != 0:
        return (f"Tests passed but the merge failed: "
                f"{(merge.stderr or merge.stdout)[:500]}\n"
                f"The work is still on branch {branch}.")

    log.warning("CLAUDE CODE merged %s into %s", branch, base)
    return _report(
        f"Applied{spend}: {rationale[:200]}",
        f"Changed:\n{summary}",
        f"It reported:\n{answer[:2000]}" if answer else "",
        f"Tests passed; merged from {branch}. It takes effect on the next run.",
        *notes,
    )


# ---------------------------------------------------------------------------
# DISPATCH
# ---------------------------------------------------------------------------

class ClaudeCodeRunner:
    """Only constructed when the sender has cleared every gate; see
    `Config.may_run_claude_code` and its caller in check_and_reply.py."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.runs = 0

    @property
    def tool_names(self) -> set[str]:
        return {TOOL}

    def schemas(self) -> list[dict]:
        return schemas(self.cfg)

    def handle(self, name: str, tool_input: dict) -> tuple[str, bool]:
        if name != TOOL:
            return f"{name} is not available.", True
        try:
            if self.runs >= self.cfg.max_claude_code_runs_per_reply:
                return ("Reached the Claude Code session limit for this reply. "
                        "Anything further waits for the next message.", True)
            self.runs += 1
            return run(self.cfg, tool_input), False
        except ClaudeCodeError as exc:
            log.warning("CLAUDE CODE refused: %s", exc)
            return str(exc), True
        except Exception as exc:
            log.exception("CLAUDE CODE failed")
            return f"The Claude Code session failed: {exc}", True
