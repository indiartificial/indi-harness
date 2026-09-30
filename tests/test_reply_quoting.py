"""
tests/test_reply_quoting.py

Replies quote the message they answer, the way Gmail's plain-text
replies do, and Indi reading its own reply back doesn't take the quote
for part of what it said.

This directory is protected. Indi can read it but not edit it.
"""

import base64
import sys
from email import message_from_bytes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_and_reply
import gmail_io
from test_reply_send_path import (SENDER, FakeClaude, FakeGmail, FakeStore, _cfg,  # noqa: F401
                                  people, reply, terms_dir)

INDI = "indi@example.org"
ORIGINAL = ("Can we meet Thursday?\n\nOn Sun, Sep 28, 2026 at 9:00 AM Indi <indi@example.org> wrote:\n"
            "> Happy to meet.\n")


def _raw(text=ORIGINAL, frm=f"=?utf-8?q?Steven_J=2E_Halasz?= <{SENDER}>"):
    return {"id": "m1", "threadId": "t1", "internalDate": "1790707920000",   # 2026-09-29 18:52 UTC
            "payload": {"mimeType": "text/plain", "headers": [
                {"name": "From", "value": frm}, {"name": "To", "value": INDI},
                {"name": "Subject", "value": "meeting"},
                {"name": "Message-ID", "value": "<m1@x>"}],
                "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}}


def test_the_quote_looks_like_gmails():
    msg = gmail_io.extract_message(_raw(), 45000)
    q = gmail_io.gmail_style_quote(msg, "America/New_York")
    first, _, rest = q.partition("\n")
    assert first == f"On Tue, Sep 29, 2026 at 2:52 PM Steven J. Halasz <{SENDER}> wrote:"
    assert "> Can we meet Thursday?" in rest
    assert "\n>\n" in rest                                  # blank lines become ">"
    assert ">> Happy to meet." in rest                      # earlier quotes nest


def test_the_whole_original_is_quoted_not_just_the_new_part():
    msg = gmail_io.extract_message(_raw(), 45000)
    assert "Happy to meet" not in msg["body"] and "Happy to meet" in msg["full_text"]


def test_no_text_no_quote():
    assert gmail_io.gmail_style_quote({"full_text": "  ", "from_raw": SENDER}) == ""


def test_an_unknown_time_zone_falls_back_to_utc():
    msg = gmail_io.extract_message(_raw(), 45000)
    assert "at 6:52 PM" in gmail_io.gmail_style_quote(msg, "Not/AZone")


def _sent_text(gmail):
    m = message_from_bytes(base64.urlsafe_b64decode(gmail.sent[-1]["raw"]))
    part = m.get_payload(0) if m.is_multipart() else m
    return part.get_payload(decode=True).decode()


def _go(tmp_path, people):
    gmail = FakeGmail()
    check_and_reply.process_message(gmail, None, FakeClaude(), _cfg(tmp_path), FakeStore(),
                                    _raw(), {INDI}, "system prompt", people)
    return gmail


def test_replies_carry_the_quote(tmp_path, people, reply, monkeypatch):
    monkeypatch.setenv("INDI_TIMEZONE", "America/New_York")
    text = _sent_text(_go(tmp_path, people))
    reply_part, _, quoted = text.partition("On Tue, Sep 29, 2026 at 2:52 PM")
    assert reply_part.strip() and "wrote:" in quoted and "> Can we meet Thursday?" in quoted


def test_quoting_can_be_switched_off(tmp_path, people, reply, monkeypatch):
    monkeypatch.setenv("INDI_QUOTE_REPLIES", "false")
    assert "wrote:" not in _sent_text(_go(tmp_path, people))


def test_indi_reading_its_reply_back_leaves_the_quote_out(tmp_path, people, reply):
    gmail = _go(tmp_path, people)
    sent = {"id": "r1", "payload": {"mimeType": "text/plain", "headers": [],
                                    "body": {"data": base64.urlsafe_b64encode(
                                        _sent_text(gmail).encode()).decode()}}}
    body = gmail_io.extract_message(sent, 45000)["body"]
    assert "Can we meet Thursday" not in body and body.strip()
