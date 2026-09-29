"""
tests/test_settings_read.py

.env through the self-code tools: readable and changeable only when the
runner is told so (an authenticated sender who may modify the harness,
replying to them alone), secret-looking values redacted on the way out and
kept on the way back in, the old file backed up, and a new one that
doesn't load as a configuration rolled back. The credential files stay
unreadable to everyone.

This directory is protected. Indi can read it but not edit it.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import self_code

ENV = ("# settings\nINDI_REPLY_ALL=true\nINDI_CLAUDE_CODE_SENDERS=steve@example.com\n"
       "SOME_API_KEY=abc123\nOAUTH_TOKEN=\nDB_PASSWORD=hunter2\n")


def _runner(tmp_path, **kw):
    (tmp_path / ".env").write_text(ENV)
    (tmp_path / "token.json").write_text("{}")
    (tmp_path / "a.py").write_text("x = 1\n")
    return self_code.SelfCodeRunner(SimpleNamespace(base_dir=tmp_path,
                                                    max_self_code_changes_per_run=1), **kw)


def test_settings_are_not_readable_by_default(tmp_path):
    text, err = _runner(tmp_path, can_modify=True).handle(self_code.READ_TOOL, {"path": ".env"})
    assert err and "abc123" not in text and "steve@example.com" not in text


def test_settings_are_readable_when_allowed_with_secrets_redacted(tmp_path):
    r = _runner(tmp_path, can_modify=True, can_read_settings=True)
    text, err = r.handle(self_code.READ_TOOL, {"path": ".env"})
    assert not err
    assert "INDI_REPLY_ALL=true" in text and "steve@example.com" in text
    assert "abc123" not in text and "hunter2" not in text
    assert "SOME_API_KEY=[redacted]" in text and "# settings" in text
    listing, _ = r.handle(self_code.LIST_TOOL, {})
    assert ".env" in listing and "read-only" in listing


def _change(r, content, rationale="turn reply-all off"):
    return r.handle(self_code.PROPOSE_TOOL, {"path": ".env", "content": content,
                                             "rationale": rationale})


def test_settings_cannot_be_changed_without_permission(tmp_path):
    r = _runner(tmp_path, can_modify=True)
    text, err = _change(r, "INDI_REPLY_ALL=false\n")
    assert err and (tmp_path / ".env").read_text() == ENV


def test_reading_settings_needs_modify_permission_too(tmp_path):
    r = _runner(tmp_path, can_modify=False, can_read_settings=True)
    assert r.handle(self_code.READ_TOOL, {"path": ".env"})[1]


def test_a_change_keeps_redacted_values_and_a_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(self_code, "validate_settings", lambda cfg: (True, "ok"))
    r = _runner(tmp_path, can_modify=True, can_read_settings=True)
    shown, _ = r.handle(self_code.READ_TOOL, {"path": ".env"})
    edited = shown.split("\n", 1)[1].replace("INDI_REPLY_ALL=true", "INDI_REPLY_ALL=false")
    text, err = _change(r, edited)
    assert not err and "INDI_REPLY_ALL=false" in text and "abc123" not in text
    now = (tmp_path / ".env").read_text()
    assert "INDI_REPLY_ALL=false" in now
    assert "SOME_API_KEY=abc123" in now and "DB_PASSWORD=hunter2" in now
    [backup] = list(tmp_path.glob(".env.bak-*"))
    assert backup.read_text() == ENV


def test_a_file_that_does_not_load_is_rolled_back(tmp_path, monkeypatch):
    monkeypatch.setattr(self_code, "validate_settings", lambda cfg: (False, "ValueError: bad"))
    r = _runner(tmp_path, can_modify=True, can_read_settings=True)
    text, err = _change(r, "INDI_MAX_TOKENS=lots\n")
    assert err and "put back" in text and (tmp_path / ".env").read_text() == ENV


def test_malformed_lines_and_the_api_key_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(self_code, "validate_settings", lambda cfg: (True, "ok"))
    r = _runner(tmp_path, can_modify=True, can_read_settings=True)
    assert _change(r, "this is not a setting\n")[1]
    assert _change(r, "ANTHROPIC_API_KEY=sk-123\n")[1]
    assert (tmp_path / ".env").read_text() == ENV


def test_backups_are_settings_too(tmp_path):
    (tmp_path / ".env.bak-20260101-000000").write_text(ENV)
    (tmp_path / ".env.example").write_text("INDI_REPLY_ALL=false\n")
    r = _runner(tmp_path, can_modify=True)
    assert r.handle(self_code.READ_TOOL, {"path": ".env.bak-20260101-000000"})[1]
    assert not r.handle(self_code.READ_TOOL, {"path": ".env.example"})[1]


def test_credentials_stay_unreadable(tmp_path):
    r = _runner(tmp_path, can_modify=True, can_read_settings=True)
    text, err = r.handle(self_code.READ_TOOL, {"path": "token.json"})
    assert err and "credentials" in text


def test_the_tool_description_says_so_only_when_allowed():
    cfg = SimpleNamespace()
    on = self_code.schemas(cfg, False, can_read_settings=True)
    off = self_code.schemas(cfg, False)
    read = lambda tools: next(t for t in tools if t["name"] == self_code.READ_TOOL)
    assert ".env" in read(on)["description"] and ".env" not in read(off)["description"]
