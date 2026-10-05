"""
tests/test_staging.py

A staging copy sends only to its allowed test addresses, and sends no
newsletter; production is untouched.

This directory is protected. Indi can read it but not edit it.
"""

import base64
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gmail_io
import staging


class Gmail:
    def __init__(self):
        self.sent, self.drafted = [], []

    def users(self): return self
    def messages(self): return SimpleNamespace(send=self._send, list=lambda **k: "listed")
    def drafts(self): return SimpleNamespace(create=self._draft)

    def _send(self, userId, body):
        self.sent.append(body)
        return SimpleNamespace(execute=lambda **_: {"id": "s1", "threadId": "t1"})

    def _draft(self, userId, body):
        self.drafted.append(body)
        return SimpleNamespace(execute=lambda **_: {"id": "d1"})


def _mime(to, cc=None):
    return gmail_io.build_reply_mime(to, "s", "b", "", cc=cc or [])


def test_production_is_untouched(monkeypatch):
    monkeypatch.setenv("INDI_STAGING", "false")
    g = Gmail()
    assert staging.guard(g) is g


def test_staging_sends_only_to_allowed_addresses(monkeypatch):
    monkeypatch.setenv("INDI_STAGING", "true")
    monkeypatch.setenv("INDI_STAGING_ALLOWED", "ann@test.com, bob@test.com")
    g = Gmail()
    svc = staging.guard(g)
    ok = svc.users().messages().send(userId="me", body=_mime("ann@test.com", ["bob@test.com"])).execute()
    assert ok["id"] == "s1" and len(g.sent) == 1
    no = svc.users().messages().send(userId="me", body=_mime("ann@test.com", ["boss@real.com"])).execute()
    assert no["id"] == "" and len(g.sent) == 1
    svc.users().drafts().create(userId="me", body={"message": _mime("stranger@real.com")}).execute()
    assert g.drafted == []
    assert svc.users().messages().list() == "listed"        # everything else passes through


def test_staging_with_no_allowlist_sends_nothing(monkeypatch):
    monkeypatch.setenv("INDI_STAGING", "true")
    monkeypatch.delenv("INDI_STAGING_ALLOWED", raising=False)
    monkeypatch.setattr(staging, "allowed", lambda: set())
    g = Gmail()
    staging.guard(g).users().messages().send(userId="me", body=_mime("ann@test.com")).execute()
    assert g.sent == []
