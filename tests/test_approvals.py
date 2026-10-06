"""
tests/test_approvals.py

Nobody new is sent the terms until the operator approves them
(approvals.py): not someone writing directly, not someone copied in, not
someone who writes "I AGREE" out of the blue. The operator answers APPROVE
or DENY in the request's thread, from their own authenticated address.

This directory is protected. Indi can read it but not edit it.
"""

import base64
import sys
from email import message_from_bytes
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import approvals
import gmail_io
import reply_all
import terms_gate
from test_terms_gate import STRANGER, FakeClaude, _cfg, _msg, people, terms_dir  # noqa: F401

OPERATOR = terms_gate.OPERATOR_ADDRESS


class Gmail:
    """Sends are recorded, each in its own thread; the held message can be fetched."""

    def __init__(self, held_raw=None):
        self.sent, self.held_raw = [], held_raw

    def users(self): return self
    def messages(self): return self
    def drafts(self): return self

    def send(self, userId, body):
        self.sent.append(body)
        n = len(self.sent)
        return SimpleNamespace(execute=lambda **_: {"id": f"s{n}", "threadId": f"ta{n}"})

    def get(self, userId, id, format="full"):
        return SimpleNamespace(execute=lambda **_: self.held_raw)

    def modify(self, **k):
        return SimpleNamespace(execute=lambda **_: {})


def _to(mime):
    m = message_from_bytes(base64.urlsafe_b64decode(mime["raw"]))
    return m["To"], m["Subject"], m


def _raw_of(msg):
    return {"id": msg["gmail_msg_id"], "threadId": msg["thread_id"], "internalDate": "0",
            "payload": {"mimeType": "text/plain", "headers": [
                {"name": "From", "value": msg["from_raw"]}, {"name": "To", "value": "indi@x.org"},
                {"name": "Subject", "value": msg["subject"]},
                {"name": "Message-ID", "value": msg["message_id"]}],
                "body": {"data": base64.urlsafe_b64encode(msg["body"].encode()).decode()}}}


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("INDI_APPROVE_NEW_CORRESPONDENTS", "true")
    monkeypatch.setenv("INDI_STAGING", "false")
    monkeypatch.setattr(gmail_io, "sender_is_authenticated", lambda m: (True, "dmarc=pass"))


def _gate(gmail, people, body="hello there, what are you?"):
    return terms_gate.check_inbound(_cfg(), gmail, FakeClaude(), _msg(body=body), people)


def test_a_newcomer_waits_for_the_operator(terms_dir, people, on):
    g = Gmail()
    d = _gate(g, people)
    assert not d.proceed and "awaiting the operator" in d.outcome
    [ask] = g.sent
    to, subject, m = _to(ask)
    assert to == OPERATOR and subject.startswith("[approve]") and STRANGER in subject
    assert "what are you" not in m.get_payload(decode=True).decode()     # headers only
    assert people.get(STRANGER).terms_sent_count == 0
    _gate(g, people, "anyone there?")                                    # asked once only
    assert len(g.sent) == 1


def test_i_agree_out_of_the_blue_is_not_an_acceptance(terms_dir, people, on):
    g = Gmail()
    d = _gate(g, people, "I AGREE")
    assert not people.get(STRANGER).has_accepted and "awaiting the operator" in d.outcome


def _answer(word, sender=OPERATOR, thread="ta1"):
    m = _msg(body=word, sender=sender, subject="Re: [approve] New correspondent")
    m["thread_id"] = thread
    return m


def test_approve_sends_the_terms_to_the_newcomer(terms_dir, people, on):
    held = _msg()
    g = Gmail(held_raw=_raw_of(held))
    _gate(g, people)
    assert approvals.match(_answer("APPROVE")) == STRANGER
    out = approvals.handle(_cfg(), g, people, _answer("Approve."), STRANGER)
    assert "approved" in out and "terms" in out
    to, _, _ = _to(g.sent[-1])
    assert STRANGER in to and people.get(STRANGER).terms_sent_count == 1
    assert approvals.status(STRANGER) == approvals.APPROVED
    assert not approvals.needs_approval(STRANGER)


def test_deny_means_silence_from_then_on(terms_dir, people, on):
    g = Gmail()
    _gate(g, people)
    out = approvals.handle(_cfg(), g, people, _answer("deny"), STRANGER)
    assert "denied" in out and len(g.sent) == 1
    d = _gate(g, people, "hello again")
    assert not d.proceed and "not approved" in d.outcome and len(g.sent) == 1


def test_only_the_operator_and_only_authenticated(terms_dir, people, on, monkeypatch):
    g = Gmail()
    _gate(g, people)
    assert "not from the operator" in approvals.handle(_cfg(), g, people,
                                                       _answer("APPROVE", sender=STRANGER),
                                                       STRANGER)
    monkeypatch.setattr(gmail_io, "sender_is_authenticated", lambda m: (False, "dmarc=fail"))
    assert "failed authentication" in approvals.handle(_cfg(), g, people, _answer("APPROVE"),
                                                       STRANGER)
    assert approvals.status(STRANGER) == approvals.PENDING


def test_an_unclear_answer_gets_one_question(terms_dir, people, on):
    g = Gmail()
    _gate(g, people)
    out = approvals.handle(_cfg(), g, people, _answer("who is this?"), STRANGER)
    assert "unclear" in out and approvals.status(STRANGER) == approvals.PENDING
    assert "APPROVE" in _to(g.sent[-1])[2].get_payload(decode=True).decode()


def test_someone_copied_in_is_asked_about_too(terms_dir, people, on):
    g = Gmail()
    ann = "ann@example.com"
    out = reply_all.paper(_cfg(), g, people, "carol@example.com",
                          {**_msg(sender=ann), "subject": "Book club"})
    assert "awaiting the operator" in out
    to, subject, m = _to(g.sent[0])
    assert to == OPERATOR and "carol@example.com" in subject
    assert "copied in by ann@example.com" in m.get_payload(decode=True).decode()
    assert people.get("carol@example.com").terms_sent_count == 0


def test_a_denied_newcomer_is_left_off_replies_and_doesnt_hold_them_up(terms_dir, people, on):
    g = Gmail()
    reply_all.paper(_cfg(), g, people, "carol@example.com", _msg(sender="ann@example.com"))
    approvals.handle(_cfg(), g, people, _answer("no"), "carol@example.com")
    assert reply_all.undecided(["carol@example.com"], people) == []


def test_the_operator_never_waits_and_staging_never_asks(people, on, monkeypatch):
    assert not approvals.needs_approval(OPERATOR)
    monkeypatch.setenv("INDI_STAGING", "true")
    assert not approvals.enabled() and not approvals.needs_approval(STRANGER)
