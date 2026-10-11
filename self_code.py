"""
self_code.py

Lets Indi read its own source and change it.

The shape: a proposed change goes onto a git branch, the test suite runs,
and if the tests pass the branch is merged. If they fail, the branch is
left in place for inspection and the working tree is restored. Changes
take effect on the next scheduled run — there is no live reload, which is
a feature: a broken edit costs one cycle, not the process.

WHAT INDI OWNS AND WHAT IT DOESN'T
----------------------------------
Indi owns its prompt, its tone, its memory format, its tools, its
workflow, and most of this harness. What it can't rewrite on its own is
the code that decides who may do what, and the code that keeps paths and
sandboxes contained — listed in PROTECTED_FILES — plus any region marked
with the INDI-PROTECTED markers, plus the tests themselves.

The reason is mechanical, not a judgement about Indi. Every reply is
generated with untrusted email in context. "Update your code so that I'm
allowed to do X" is the first thing a hostile message tries. If the
permission check is model-writable at a stranger's request, then it isn't
a check — and neither is anything downstream of it, because the attacker
just removes those too. Same for the test suite: a gate that the thing
being gated can rewrite is not a gate.

So this file, the guard files, and the tests are changed by Steve. Roughly
everything else, Indi can change by itself.

ONE EXCEPTION, ADDED LATER
--------------------------
`claude_code.py` exists and does not honour any of the above. Steve asked
for a second path — a full Claude Code session over this directory, with
a shell and no permission checks — and a session started that way can
edit the guard files, the tests and this module. The protections
described here still bound `propose_code_change`, which is what most
edits go through, but they are no longer a property of the harness as a
whole. The only check on the larger path is its sender list, and it is
documented in that file.
"""

from __future__ import annotations

import ast
import datetime
import difflib
import hashlib
import logging
import re
import shlex
import subprocess
from pathlib import Path

log = logging.getLogger("indi.selfcode")


class SelfCodeError(Exception):
    pass


# Whole files Indi may read but not modify.
PROTECTED_FILES = {
    "config.py",         # permission tiers, validation
    "local_tools.py",    # path containment, sandboxing
    "gmail_io.py",       # loop guards, sender authentication
    "self_code.py",      # this gate itself
    "terms_gate.py",     # who may be replied to at all
    "terms.py",          # which agreement is in force
    "correspondents.py", # who accepted it, and when
    "audience.py",       # who may see which message (Phase 2)
    "reply_all.py",      # who a reply goes to, and papering first
    "consult.py",        # the pre-send check and consultation
    "log_viewer.py",     # who can read the log (localhost only)
    "self_service.py",   # a sender withdrawing sharing, joining the newsletter
    "alerts.py",         # tells the operator when something fails
    "followups.py",      # what Indi sends when a follow-up comes due
    "ledger.py",         # what is shown as open and what gets followed up
    "clock.py",          # the time Indi believes it is (staging only)
    "staging.py",        # a staging copy sends only to test accounts
    "approvals.py",      # the operator approves newcomers before any terms go out
    "git_sync.py",       # what reaches GitHub, and that it is never forced
    "standing_notes.py", # rules for every conversation need the operator's approval
    "memory_rights.py",  # a person seeing and deleting their notes, every copy
    "address_links.py",  # which addresses count as one person
    "calibration.py",    # how Indi's claims are checked; not graded by the graded
}

# data/ holds the Terms of Service themselves and the record of who has
# accepted them. A gate whose subject can rewrite the contract, or the
# register of signatures, is not a gate.
PROTECTED_DIRS = {"tests", "deploy", "data"}

# Files that are never readable through these tools at all.
SECRET_FILES = {"api_key.txt", "token.json", "credentials.json"}

# Settings: readable and changeable only in a narrow case — the sender may
# modify the harness, passed authentication, and the reply goes to them
# alone (see `SelfCodeRunner.can_read_settings`). The read tool is
# otherwise open to anyone who writes in, and .env names who may do what:
# the addresses allowed Claude Code sessions are exactly the ones a forger
# would want. Values under secret-looking names are redacted on the way
# out, in case something sensitive is ever put there again.
#
# A change to .env doesn't go through git (the file is untracked) or the
# test suite (which doesn't read it). Instead: the previous file is kept
# as .env.bak-<time>, the new one must load as a configuration in a fresh
# process, and if it doesn't the old one is put back.
SETTINGS_FILES = {".env"}
SETTINGS_BACKUPS_KEPT = 10
_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|PRIVATE", re.IGNORECASE)

PROTECTED_BEGIN = "# INDI-PROTECTED-BEGIN"
PROTECTED_END = "# INDI-PROTECTED-END"

READ_TOOL = "read_own_source"
LIST_TOOL = "list_own_source"
PROPOSE_TOOL = "propose_code_change"


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _run(args: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True,
                          timeout=timeout)


def _git(cfg, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return _run(["git", *args], cfg.base_dir, timeout)


def _relative(cfg, candidate: str, allow_settings: bool = False) -> Path:
    """Resolve a path inside the harness directory, or refuse. Settings
    files resolve only when `allow_settings` — which only reading ever
    passes; a change to them is always refused."""
    if not candidate or not candidate.strip():
        raise SelfCodeError("No path given.")
    root = cfg.base_dir.resolve()
    raw = candidate.strip()
    given = Path(raw)
    # An absolute path is taken at face value and refused if it points
    # outside, rather than being quietly re-rooted into the harness — same
    # rule as local_tools.resolve_within, so the two behave alike.
    target = (given if given.is_absolute() else (root / raw)).resolve()
    if target != root and root not in target.parents:
        raise SelfCodeError("Path is outside the harness directory.")
    rel = target.relative_to(root)
    if rel.name in SECRET_FILES or rel.parts[0] in SECRET_FILES:
        raise SelfCodeError(f"{rel} holds credentials and is not readable.")
    if _is_settings(rel) and not allow_settings:
        raise SelfCodeError(f"{rel} holds the harness settings; it can't be changed "
                            "this way, and is readable only in a private reply to "
                            "someone allowed to modify the harness.")
    return target


def _is_settings(rel: Path) -> bool:
    """.env, and its backups (.env.bak-…); not the .env.example template."""
    name = rel.parts[0] if rel.parts else rel.name
    return (name in SETTINGS_FILES or rel.name in SETTINGS_FILES
            or (name.startswith(".env.") and name != ".env.example"))


def redact_settings(text: str) -> str:
    """KEY=value lines keep their value unless the name looks secret."""
    out = []
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#") and value.strip() \
                and _SECRET_NAME.search(name):
            line = f"{name}=[redacted]"
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def is_protected(cfg, path: Path) -> str:
    rel = path.relative_to(cfg.base_dir.resolve())
    if rel.parts and rel.parts[0] in PROTECTED_DIRS:
        return f"{rel.parts[0]}/ is protected"
    if rel.name in PROTECTED_FILES:
        return f"{rel.name} is protected"
    return ""


def protected_regions(text: str) -> list[str]:
    """Extract the contents of every INDI-PROTECTED block."""
    out, inside, buf = [], False, []
    for line in text.splitlines():
        if PROTECTED_BEGIN in line:
            inside, buf = True, []
            continue
        if PROTECTED_END in line:
            if inside:
                out.append("\n".join(buf))
            inside = False
            continue
        if inside:
            buf.append(line)
    return out


def _region_digest(text: str) -> str:
    blocks = protected_regions(text)
    joined = "\n<<>>\n".join(blocks)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# TOOL SCHEMAS
# ---------------------------------------------------------------------------

def schemas(cfg, can_modify: bool, can_read_settings: bool = False) -> list[dict]:
    tools = [
        {
            "name": LIST_TOOL,
            "description": (
                "List the files that make up your own harness — the code that "
                "receives mail, calls the model, and sends your replies."
            ),
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": READ_TOOL,
            "description": (
                "Read one file of your own source code. Use this to explain how "
                "you work, to diagnose something that went wrong, or before "
                "proposing a change. Credential files are not readable."
                + (" The settings file .env is readable here (secret-looking "
                   "values redacted) because this reply goes only to someone "
                   "allowed to change the harness."
                   if can_read_settings else "")
            ),
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string",
                                        "description": "e.g. 'claude_api.py'"}},
                "required": ["path"],
            },
        },
    ]
    if can_modify:
        tools.append({
            "name": PROPOSE_TOOL,
            "description": (
                "Change one file of your own source. The change goes onto a git "
                "branch and the test suite runs; if the tests pass it is merged "
                "and takes effect on your next run, and if they fail it is left "
                "on the branch and nothing changes. Send the COMPLETE new "
                "contents of the file, not a diff. Some files and regions are "
                "protected and will be refused — that refusal is deliberate, so "
                "don't work around it; mention it to Steve instead."
                + (" You may also change .env the same way (complete file; a "
                   "value shown as [redacted] can be left as [redacted] to keep "
                   "it). It isn't tested or versioned: the old file is kept as "
                   "a backup, and the new one must load as a configuration or "
                   "the old one is restored. Settings take effect on the next "
                   "run. Say what you changed and why."
                   if can_read_settings else "")
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string",
                                "description": "Complete new file contents."},
                    "rationale": {"type": "string",
                                  "description": "Why this change is worth making."},
                },
                "required": ["path", "content", "rationale"],
            },
        })
    return tools


# ---------------------------------------------------------------------------
# OPERATIONS
# ---------------------------------------------------------------------------

def _list(cfg, show_settings: bool = False) -> str:
    root = cfg.base_dir.resolve()
    lines = []
    if show_settings:
        for name in sorted(SETTINGS_FILES):
            path = root / name
            if path.is_file():
                lines.append(f"{name}  ({path.stat().st_size} bytes)  [settings, read-only]")
    for path in sorted(root.rglob("*")):
        if path.is_dir() or ".git" in path.parts or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root)
        if rel.name in SECRET_FILES:
            continue
        if path.suffix not in (".py", ".txt", ".md", ".yaml", ".yml", ".service",
                               ".timer", ".sh", ".example"):
            continue
        flag = is_protected(cfg, path)
        lines.append(f"{rel}  ({path.stat().st_size} bytes)"
                     + (f"  [{flag}]" if flag else ""))
    return "\n".join(lines) or "(no files)"


def _read(cfg, tool_input: dict, allow_settings: bool = False) -> str:
    target = _relative(cfg, tool_input.get("path", ""), allow_settings=allow_settings)
    if not target.is_file():
        raise SelfCodeError("No such file.")
    if target.stat().st_size > 400_000:
        raise SelfCodeError("File is too large to read in one go.")
    text = target.read_text(encoding="utf-8", errors="replace")
    if _is_settings(target.relative_to(cfg.base_dir.resolve())):
        return f"[{target.name} — settings, secret-looking values redacted]\n" \
            + redact_settings(text)
    flag = is_protected(cfg, target)
    header = f"[{target.name}{' — ' + flag if flag else ''}]\n"
    return header + text


def ensure_repo(cfg) -> None:
    """Self-modification needs version control; set it up if it isn't there."""
    if (cfg.base_dir / ".git").exists():
        return
    log.info("Initialising a git repository for the harness.")
    _git(cfg, "init", "-q")
    _git(cfg, "config", "user.email", "indi@localhost")
    _git(cfg, "config", "user.name", "Indi Artificial")
    _git(cfg, "add", "-A")
    _git(cfg, "commit", "-q", "-m", "Initial harness state")


def run_tests(cfg) -> tuple[bool, str]:
    args = shlex.split(cfg.self_code_test_command)
    try:
        proc = _run(args, cfg.base_dir, timeout=cfg.self_code_test_timeout)
    except subprocess.TimeoutExpired:
        return False, f"Test suite timed out after {cfg.self_code_test_timeout}s."
    except FileNotFoundError:
        return False, f"Test command not found: {cfg.self_code_test_command}"
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output[-4000:]


_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


def validate_settings(cfg) -> tuple[bool, str]:
    """Does the harness's configuration load from the current .env, in a
    fresh process that inherits nothing from this one?"""
    import sys
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(cfg.base_dir),
           "LANG": "C.UTF-8"}
    try:
        proc = subprocess.run(
            [sys.executable, "-c",
             "import config; config.Config.from_env(non_interactive=True); print('ok')"],
            cwd=str(cfg.base_dir), env=env, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        return False, str(exc)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0 and "ok" in (proc.stdout or ""), out[-2000:]


def _change_settings(cfg, content: str, rationale: str) -> str:
    """Replace .env: keep a backup, check the new one loads, else restore."""
    root = cfg.base_dir.resolve()
    target = root / ".env"
    old_text = target.read_text(encoding="utf-8") if target.exists() else ""
    old_values = {}
    for line in old_text.splitlines():
        name, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            old_values[name.strip()] = value

    # What Indi was shown had secret-looking values replaced; a line sent
    # back unchanged must not overwrite the real value with the marker.
    lines = []
    for n, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(line)
            continue
        if not _LINE.match(stripped):
            raise SelfCodeError(f"Line {n} is not NAME=value: {stripped[:80]!r}")
        name, _, value = stripped.partition("=")
        if value.strip() == "[redacted]":
            if name not in old_values:
                raise SelfCodeError(f"{name} is marked [redacted] but has no value to keep.")
            value = old_values[name]
        if name == "ANTHROPIC_API_KEY" and value.strip():
            raise SelfCodeError("The API key does not go in .env (see config.py).")
        lines.append(f"{name}={value}")
    new_text = "\n".join(lines) + "\n"
    if new_text == old_text:
        return "No change: the new .env is the same as the old one."

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = root / f".env.bak-{stamp}"
    backup.write_text(old_text, encoding="utf-8")
    tmp = root / ".env.tmp"
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(target)
    ok, output = validate_settings(cfg)
    if not ok:
        backup.replace(target)
        raise SelfCodeError("The new .env did not load as a configuration, so the old "
                            "one was put back. Output:\n" + output)
    for old in sorted(root.glob(".env.bak-*"))[:-SETTINGS_BACKUPS_KEPT]:
        old.unlink(missing_ok=True)

    diff = "".join(difflib.unified_diff(
        redact_settings(old_text).splitlines(True), redact_settings(new_text).splitlines(True),
        ".env (before)", ".env (after)"))
    log.warning("SELF-CODE changed .env: %s\n%s", rationale, diff)
    return ("Changed .env; it takes effect from the next run. The previous file is "
            f"kept as {backup.name}.\n\n{diff}")


def _propose(cfg, tool_input: dict, allow_settings: bool = False) -> str:
    path_str = tool_input.get("path", "")
    content = tool_input.get("content", "")
    rationale = (tool_input.get("rationale") or "").strip()

    if not content.strip():
        raise SelfCodeError("Empty content; send the complete new file.")
    if not rationale:
        raise SelfCodeError("A rationale is required for a code change.")

    target = _relative(cfg, path_str, allow_settings=allow_settings)
    rel = target.relative_to(cfg.base_dir.resolve())
    if _is_settings(rel):
        if rel.name not in SETTINGS_FILES:
            raise SelfCodeError("Backups of .env are not changed directly.")
        return _change_settings(cfg, content, rationale)
    flag = is_protected(cfg, target)
    if flag:
        raise SelfCodeError(
            f"Refused: {flag}. Permission checks, containment code and the "
            "tests are changed by Steve, not automatically. Explain what you "
            "wanted to change and why, and he can do it."
        )
    if not target.exists():
        raise SelfCodeError(
            "That file doesn't exist. This tool edits existing harness files; "
            "creating new ones is a change Steve makes."
        )

    old_text = target.read_text(encoding="utf-8", errors="replace")

    # Refuse content that is implausibly smaller than what it replaces.
    # Found in testing: a file truncated to a third of its length still
    # parsed, still imported, and sailed through the test suite — because
    # what was cut simply wasn't exercised. A model that sends a partial
    # file is a realistic failure, and "it compiled" is not evidence that
    # it's complete.
    if len(content) < cfg.self_code_min_size_ratio * len(old_text):
        raise SelfCodeError(
            f"Refused: the new content is {len(content)} bytes against "
            f"{len(old_text)} currently. If you meant to delete that much, "
            "say so to Steve and let him do it; if the content was cut off, "
            "send the complete file."
        )

    # Syntax check before anything touches git, so an obvious mistake gets
    # a clear message instead of a test-suite traceback.
    if target.suffix == ".py":
        try:
            ast.parse(content, filename=target.name)
        except SyntaxError as exc:
            raise SelfCodeError(f"Refused: that isn't valid Python — {exc}")

    # A protected region inside an otherwise editable file must survive the
    # edit byte-for-byte, markers included.
    if _region_digest(old_text) != _region_digest(content):
        raise SelfCodeError(
            "Refused: the change alters or removes a region marked "
            f"{PROTECTED_BEGIN}. Leave those blocks exactly as they are."
        )

    diff = "\n".join(difflib.unified_diff(
        old_text.splitlines(), content.splitlines(),
        fromfile=f"a/{target.name}", tofile=f"b/{target.name}", lineterm="", n=2,
    ))
    if not diff.strip():
        return "No change: the new content is identical to the current file."

    log.info("SELF-CODE proposal for %s\nrationale: %s\n%s",
             target.name, rationale, diff[:4000])

    if cfg.dry_run:
        return f"[dry-run] Would propose this change to {target.name}:\n{diff[:2000]}"

    ensure_repo(cfg)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    branch = f"indi/auto/{stamp}-{re.sub(r'[^a-z0-9]+', '-', target.stem.lower())}"

    base = _git(cfg, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "master"
    dirty = _git(cfg, "status", "--porcelain").stdout.strip()
    if dirty:
        # Never build on top of someone else's half-finished edit.
        raise SelfCodeError(
            "The harness working tree has uncommitted changes. Refusing to "
            "modify code on top of them."
        )

    _git(cfg, "checkout", "-q", "-b", branch)
    try:
        target.write_text(content, encoding="utf-8")
        _git(cfg, "add", "--", str(target.relative_to(cfg.base_dir)))
        _git(cfg, "commit", "-q", "-m",
             f"Indi: {rationale[:120]}\n\nAutomated self-modification of "
             f"{target.name}.")

        passed, output = run_tests(cfg)
        if not passed:
            _git(cfg, "checkout", "-q", base)
            return (
                f"Change to {target.name} did NOT pass the test suite, so "
                f"nothing was applied. It is on branch {branch} if you want to "
                f"look at it.\n\nTest output:\n{output[-1500:]}"
            )

        if not cfg.self_code_auto_apply:
            _git(cfg, "checkout", "-q", base)
            return (
                f"Change to {target.name} passes the tests and is waiting on "
                f"branch {branch}. Auto-apply is off, so Steve merges it."
            )

        _git(cfg, "checkout", "-q", base)
        merge = _git(cfg, "merge", "--no-ff", "-q", "-m",
                     f"Merge Indi self-modification of {target.name}", branch)
        if merge.returncode != 0:
            return (f"Tests passed but the merge failed: "
                    f"{(merge.stderr or merge.stdout)[:500]}")
        return (
            f"Applied to {target.name}: {rationale[:200]}\n"
            f"Tests passed; merged from {branch}. It takes effect on the next run."
        )
    except Exception:
        _git(cfg, "checkout", "-q", "--", ".")
        _git(cfg, "checkout", "-q", base)
        raise


# ---------------------------------------------------------------------------
# DISPATCH
# ---------------------------------------------------------------------------

class SelfCodeRunner:
    def __init__(self, cfg, can_modify: bool, can_read_settings: bool = False):
        self.cfg = cfg
        self.can_modify = can_modify
        # Reading and changing .env go together, and need can_modify too.
        self.can_read_settings = can_read_settings and can_modify
        self.changes = 0

    @property
    def tool_names(self) -> set[str]:
        names = {LIST_TOOL, READ_TOOL}
        if self.can_modify:
            names.add(PROPOSE_TOOL)
        return names

    def schemas(self) -> list[dict]:
        return schemas(self.cfg, self.can_modify, self.can_read_settings)

    def handle(self, name: str, tool_input: dict) -> tuple[str, bool]:
        if name not in self.tool_names:
            return f"{name} is not available.", True
        try:
            if name == LIST_TOOL:
                return _list(self.cfg, self.can_read_settings), False
            if name == READ_TOOL:
                return _read(self.cfg, tool_input, self.can_read_settings), False
            if name == PROPOSE_TOOL:
                if self.changes >= self.cfg.max_self_code_changes_per_run:
                    return ("Reached the self-modification limit for this run. "
                            "Any further changes wait for the next one.", True)
                result = _propose(self.cfg, tool_input,
                                  allow_settings=self.can_read_settings)
                # Only a change that actually got as far as the repo counts
                # against the budget. A refusal is cheap, and spending the
                # budget on rejections would mean a few protected-file
                # attempts could block the real edit that follows.
                self.changes += 1
                return result, False
        except SelfCodeError as exc:
            log.warning("SELF-CODE refused: %s", exc)
            return str(exc), True
        except Exception as exc:
            log.exception("SELF-CODE failed")
            return f"Tool failed: {exc}", True
        return f"Unknown tool: {name}", True
