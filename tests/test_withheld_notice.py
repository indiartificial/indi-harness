"""
tests/test_withheld_notice.py

When a setting withholds Indi's reply from a person — the daily cap, the
allowed-senders list, or the pre-send check — they get one short note
saying Indi is unable to respond. Never to a machine, at most one a day,
never with a store that can't count them.

This directory is protected. Indi can read it but not edit it.
"""

import base64
import sys
from email import message_from_bytes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import check_and_reply
import claude_api
import consult
from test_reply_all import Store as BaseStore
from test_reply_send_path import (SENDER, FakeClaude, FakeGmail, _cfg, people,  # noqa: F401
                                  reply, terms_dir)

INDI = "indi@example.org"


class Store(BaseStore):
    def __init__(self, replies_today=0):
        super().__init__()
        self._replies = replies_today
        self.notices = []

    def replies_in_last_day(self, sender):
        return self._replies

    def notices_in_last_day(self, addr):
        return sum(1 for a in self.notices if a == addr)

    def record_notice(self, message_id):
        self.notices.append(SENDER)


def _raw(frm=SENDER, mid="m1"):
    return {"id": mid, "threadId": "t1", "internalDate": "1700000000000",
            "payload": {"mimeType": "text/plain", "headers": [
                {"name": "From", "value": frm}, {"name": "To", "value": INDI},
                {"name": "Subject", "value": "hello"},
                {"name": "Message-ID", "value": f"<{mid}@x>"}],
                "body": {"data": base64.urlsafe_b64encode(b"hi").decode()}}}


def _go(gmail, cfg, store, people, raw):
    return check_and_reply.process_message(gmail, None, FakeClaude(), cfg, store, raw,
                                           {INDI}, "system prompt", people)


def _body(mime):
    msg = message_from_bytes(base64.urlsafe_b64decode(mime["raw"]))
    part = msg.get_payload(0) if msg.is_multipart() else msg
    return msg, part.get_payload(decode=True).decode()


@pytest.fixture
def no_model(monkeypatch):
    monkeypatch.setattr(claude_api, "generate_reply",
                        lambda *a, **k: pytest.fail("a reply was composed"))


def test_the_daily_cap_sends_one_notice_a_day(tmp_path, people, reply, no_model):
    gmail, store = FakeGmail(), Store(replies_today=25)
    cfg = _cfg(tmp_path, max_replies_per_sender_per_day=25)
    out = _go(gmail, cfg, store, people, _raw())
    assert "notice sent" in out and len(gmail.sent) == 1
    msg, body = _body(gmail.sent[0])
    assert msg["To"] == SENDER and msg["In-Reply-To"] == "<m1@x>"
    assert "unable to respond" in body and "limit resets" in body
    out = _go(gmail, cfg, store, people, _raw(mid="m2"))
    assert "already sent today" in out and len(gmail.sent) == 1


def test_machines_never_get_one(tmp_path, people, reply, no_model):
    gmail, store = FakeGmail(), Store(replies_today=99)
    _go(gmail, _cfg(tmp_path, max_replies_per_sender_per_day=5), store, people,
        _raw(frm="no-reply@example.com"))
    assert gmail.sent == []


def test_senders_outside_the_allowed_list_get_one(tmp_path, people, reply, no_model):
    gmail, store = FakeGmail(), Store()
    cfg = _cfg(tmp_path)
    type(cfg).is_allowed = lambda self, addr: False       # the test config's own class
    out = _go(gmail, cfg, store, people, _raw())
    assert "notice sent" in out
    assert "unable to respond" in _body(gmail.sent[0])[1]


def test_a_blocked_reply_gets_the_same_plain_notice(tmp_path, people, reply, monkeypatch):
    monkeypatch.setattr(consult, "enabled", lambda cfg=None: True)
    monkeypatch.setattr(consult, "match", lambda store, msg: None)
    monkeypatch.setattr(consult, "candidates", lambda store, r, limit=10: [{"x": 1}])
    monkeypatch.setattr(consult, "vet", lambda *a: (None, "leaks something"))
    gmail, store = FakeGmail(), Store()
    out = _go(gmail, _cfg(tmp_path), store, people, _raw())
    assert "blocked by the pre-send check" in out and len(gmail.sent) == 1
    body = _body(gmail.sent[0])[1]
    assert "unable to respond" in body and "limit" not in body


def test_no_notice_without_a_way_to_count_them(tmp_path, people, reply, no_model):
    gmail = FakeGmail()
    store = BaseStore()
    store.replies_in_last_day = lambda s: 99
    out = _go(gmail, _cfg(tmp_path, max_replies_per_sender_per_day=5), store, people, _raw())
    assert "no notice" in out and gmail.sent == []


def test_a_dry_run_sends_nothing(tmp_path, people, reply, no_model):
    gmail, store = FakeGmail(), Store(replies_today=99)
    out = _go(gmail, _cfg(tmp_path, max_replies_per_sender_per_day=5, dry_run=True),
              store, people, _raw())
    assert "would skip" in out and gmail.sent == []
