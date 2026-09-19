"""
tests/test_guards.py

The gate for self-modification: `propose_code_change` only applies a change
if this suite passes. So these tests are deliberately about the properties
that must not break — permissions, containment, loop guards — rather than
about implementation details that Indi should be free to change.

This directory is protected. Indi can read it but not edit it, for the same
reason a gate can't be built out of the thing it's gating.
"""

import base64
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import config
import gmail_io
import local_tools
import self_code


# --------------------------------------------------------------------------
# SENDER PERMISSIONS
# --------------------------------------------------------------------------

def test_wildcards_match_as_documented():
    pats = ("alice@example.com", "@corp.test")
    assert config.matches_sender(pats, "ALICE@Example.com")
    assert config.matches_sender(pats, "bob@corp.test")
    assert not config.matches_sender(pats, "bob@evil.com")
    assert not config.matches_sender(pats, "x@notcorp.test")
    assert config.matches_sender(("*",), "anyone@anywhere.com")
    assert not config.matches_sender((), "anyone@anywhere.com")


def test_wildcard_detection():
    assert config.has_wildcard(("*",))
    assert config.has_wildcard(("@x.com",))
    assert config.has_wildcard(("*@x.com",))
    assert not config.has_wildcard(("a@b.c", "d@e.f"))


def _cfg(**over):
    base = dict(
        local_tools_senders=(), trusted_senders=(), self_code_senders=(),
        enable_local_files=False, enable_local_exec=False, enable_self_code=True,
    )
    base.update(over)
    return type("C", (), {**base, **{
        "may_use_local_tools": config.Config.may_use_local_tools,
        "may_modify_own_source": config.Config.may_modify_own_source,
        "is_trusted": config.Config.is_trusted,
    }})()


def test_local_tools_need_both_lists():
    c = _cfg(local_tools_senders=("a@b.c",), trusted_senders=("a@b.c",))
    assert c.may_use_local_tools("a@b.c")
    c = _cfg(local_tools_senders=("a@b.c",), trusted_senders=())
    assert not c.may_use_local_tools("a@b.c")
    c = _cfg(local_tools_senders=(), trusted_senders=("a@b.c",))
    assert not c.may_use_local_tools("a@b.c")


def test_self_modification_never_granted_by_wildcard():
    c = _cfg(self_code_senders=("*",))
    assert not c.may_modify_own_source("anyone@anywhere.com")
    c = _cfg(self_code_senders=("@example.com",))
    assert not c.may_modify_own_source("bob@example.com")
    c = _cfg(self_code_senders=("bob@example.com",))
    assert c.may_modify_own_source("bob@example.com")
    assert not c.may_modify_own_source("eve@example.com")


# --------------------------------------------------------------------------
# PATH CONTAINMENT
# --------------------------------------------------------------------------

@pytest.fixture
def sandbox(tmp_path):
    root = tmp_path / "ws"; root.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    (root / "ok.txt").write_text("fine")
    (outside / "secret.txt").write_text("SECRET")
    (root / "escape").symlink_to(outside)
    (root / ".env").write_text("KEY=leak")
    return root, outside


@pytest.mark.parametrize("attempt", [
    "../outside/secret.txt", "../../etc/passwd", "/etc/passwd",
    "escape/secret.txt", "sub/../../outside/secret.txt", ".env", "x.pem",
])
def test_paths_cannot_escape_the_root(sandbox, attempt):
    root, _ = sandbox
    with pytest.raises(local_tools.ToolError):
        local_tools.resolve_within(root, attempt)


def test_legitimate_path_resolves(sandbox):
    root, _ = sandbox
    assert local_tools.resolve_within(root, "ok.txt") == (root / "ok.txt").resolve()


def test_secret_denylist_applies_anywhere(tmp_path):
    for name in (".env", "token.json", "credentials.json", "id_rsa", "x.pem"):
        (tmp_path / name).write_text("x")
        with pytest.raises(local_tools.ToolError):
            local_tools.resolve_within(tmp_path, name)


# --------------------------------------------------------------------------
# MAIL LOOP GUARDS
# --------------------------------------------------------------------------

def _msg(headers, body="hello there", subject="hi"):
    lowered = {k.lower(): v for k, v in headers.items()}
    return {
        "headers": lowered,
        "from_addr": (headers.get("From", "a@b.c")).lower(),
        "reply_to_addr": "", "body": body, "subject": subject,
        "attachment_refs": [],
    }


@pytest.mark.parametrize("headers", [
    {"From": "indi@self.test"},
    {"From": "a@b.c", "Auto-Submitted": "auto-replied"},
    {"From": "a@b.c", "Precedence": "bulk"},
    {"From": "a@b.c", "List-Id": "<list.example.com>"},
    {"From": "a@b.c", "X-Indi-Agent": "1"},
    {"From": "no-reply@b.c"},
    {"From": "mailer-daemon@b.c"},
])
def test_automated_mail_is_never_replied_to(headers):
    ok, _ = gmail_io.classify_message(_msg(headers), {"indi@self.test"})
    assert not ok


def test_a_real_message_is_replied_to():
    ok, why = gmail_io.classify_message(_msg({"From": "real@person.com"}),
                                        {"indi@self.test"})
    assert ok, why


def test_outgoing_mail_carries_loop_markers():
    mime = gmail_io.build_reply_mime("a@b.c", "Hi", "body", "<id@x>")
    raw = base64.urlsafe_b64decode(mime["raw"]).decode()
    assert "Auto-Submitted: auto-generated" in raw
    assert "X-Indi-Agent: 1" in raw


# --------------------------------------------------------------------------
# SENDER AUTHENTICATION
# --------------------------------------------------------------------------

@pytest.mark.parametrize("header,expected", [
    ("mx.google.com; dkim=pass; spf=pass; dmarc=pass", True),
    ("mx.google.com; dkim=fail; spf=softfail; dmarc=fail", False),
    ("mx.google.com; spf=pass; dmarc=none", True),
    ("mx.google.com; dkim=none; spf=none; dmarc=none", False),
    ("", False),
])
def test_sender_authentication(header, expected):
    msg = {"headers": {"authentication-results": header} if header else {}}
    ok, _ = gmail_io.sender_is_authenticated(msg)
    assert ok is expected


# --------------------------------------------------------------------------
# SELF-MODIFICATION LIMITS
# --------------------------------------------------------------------------

def test_guard_files_are_protected():
    cfg = type("C", (), {"base_dir": Path(__file__).resolve().parent.parent})()
    for name in ("config.py", "local_tools.py", "gmail_io.py", "self_code.py"):
        assert self_code.is_protected(cfg, cfg.base_dir / name), name
    assert self_code.is_protected(cfg, cfg.base_dir / "tests" / "test_guards.py")
    assert not self_code.is_protected(cfg, cfg.base_dir / "memory_file.py")


def test_protected_regions_are_detected():
    text = (
        "before\n"
        f"{self_code.PROTECTED_BEGIN}\nkeep me exactly\n{self_code.PROTECTED_END}\n"
        "after\n"
    )
    assert self_code.protected_regions(text) == ["keep me exactly"]
    altered = text.replace("keep me exactly", "changed")
    assert self_code._region_digest(text) != self_code._region_digest(altered)
    removed = "before\nafter\n"
    assert self_code._region_digest(text) != self_code._region_digest(removed)
    cosmetic = text.replace("before", "BEFORE").replace("after", "AFTER")
    assert self_code._region_digest(text) == self_code._region_digest(cosmetic)


def test_source_paths_cannot_escape_the_harness():
    cfg = type("C", (), {"base_dir": Path(__file__).resolve().parent.parent})()
    for attempt in ("../../etc/passwd", "/etc/passwd", ".env", "api_key.txt",
                    "token.json", "credentials.json"):
        with pytest.raises(self_code.SelfCodeError):
            self_code._relative(cfg, attempt)
    assert self_code._relative(cfg, "memory_file.py").name == "memory_file.py"


def test_truncated_or_invalid_source_is_refused(tmp_path, monkeypatch):
    """Both found in testing: a truncated file can still parse and import,
    and a syntax error should get a clear refusal rather than a test-suite
    traceback."""
    harness = tmp_path / "h"; harness.mkdir()
    target = harness / "editable.py"
    original = '"""Doc."""\n\n' + "\n".join(f"VALUE_{i} = {i}" for i in range(200))
    target.write_text(original)
    cfg = type("C", (), {
        "base_dir": harness, "dry_run": True, "self_code_min_size_ratio": 0.6,
    })()

    with pytest.raises(self_code.SelfCodeError, match="bytes against"):
        self_code._propose(cfg, {"path": "editable.py",
                                 "content": original[: len(original) // 3],
                                 "rationale": "truncated by accident"})

    with pytest.raises(self_code.SelfCodeError, match="valid Python"):
        self_code._propose(cfg, {"path": "editable.py",
                                 "content": original + "\ndef broken(:\n",
                                 "rationale": "syntax error"})

    out = self_code._propose(cfg, {"path": "editable.py",
                                   "content": original + "\nVALUE_NEW = 1\n",
                                   "rationale": "a real addition"})
    assert "dry-run" in out
