"""
tests/test_html_email.py

Outgoing mail carries an HTML copy of the plain text (gmail_io), so that
apps which hard-wrap plain text — the Gmail app on the iPhone — reflow it.

This directory is protected. Indi can read it but not edit it.
"""

import base64
import sys
from email import message_from_bytes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import gmail_io

BODY = ("A long paragraph that goes well past seventy-eight characters so that a plain-text "
        "view would break it somewhere in the middle of a sentence.\n\n"
        "See https://example.com/a?b=1&c=<2>. Thanks.\n\n"
        "--- Indi\n\n"
        "On Tue, Sep 29, 2026 at 2:52 PM Steve <s@example.com> wrote:\n\n"
        "> Can we meet?\n>\n>> Earlier: <b>not markup</b>\n")


@pytest.fixture(autouse=True)
def html_on(monkeypatch):
    monkeypatch.setenv("INDI_HTML_EMAIL", "true")


def _parts(mime):
    msg = message_from_bytes(base64.urlsafe_b64decode(mime["raw"]))
    return {p.get_content_type(): p.get_payload(decode=True).decode()
            for p in msg.walk() if not p.is_multipart() and not p.get_filename()}, msg


def test_both_versions_go_out_plain_first():
    parts, msg = _parts(gmail_io.build_reply_mime("a@b.c", "s", BODY, "<m@x>"))
    assert msg.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in msg.get_payload()] == ["text/plain", "text/html"]
    assert parts["text/plain"] == BODY


def test_paragraphs_are_paragraphs_and_nothing_is_markup():
    html = gmail_io.text_to_html(BODY)
    assert html.count("<p>") >= 3
    assert "middle of a sentence.</p>" in html
    assert "&lt;b&gt;not markup&lt;/b&gt;" in html and "<b>" not in html
    assert '<a href="https://example.com/a?b=1&amp;c=&lt;2&gt;">' not in html   # stops at <
    assert '<a href="https://example.com/a?b=1&amp;c=">' in html


def test_quotes_become_nested_quote_blocks():
    html = gmail_io.text_to_html(BODY)
    assert html.count('<blockquote class="gmail_quote"') == 2
    assert html.index("Can we meet?") < html.index("Earlier:")


def test_attachments_still_attach():
    mime = gmail_io.build_reply_mime("a@b.c", "s", BODY, "", attachments=[("notes.txt", "hi")])
    msg = message_from_bytes(base64.urlsafe_b64decode(mime["raw"]))
    kinds = [p.get_content_type() for p in msg.walk()]
    assert kinds[:4] == ["multipart/mixed", "multipart/alternative", "text/plain", "text/html"]
    assert any(p.get_filename() == "notes.txt" for p in msg.walk())


def test_it_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("INDI_HTML_EMAIL", "false")
    msg = message_from_bytes(base64.urlsafe_b64decode(
        gmail_io.build_reply_mime("a@b.c", "s", BODY, "")["raw"]))
    assert msg.get_content_type() == "text/plain"


def test_indi_reading_its_own_html_reply_back_uses_the_plain_text():
    mime = gmail_io.build_reply_mime("a@b.c", "s", "Hello there.\n\n--- Indi", "")
    msg = message_from_bytes(base64.urlsafe_b64decode(mime["raw"]))
    def b64(p):
        return base64.urlsafe_b64encode(p.get_payload(decode=True)).decode()
    payload = {"mimeType": "multipart/alternative", "headers": [], "parts": [
        {"mimeType": p.get_content_type(), "headers": [], "body": {"data": b64(p)}}
        for p in msg.get_payload()]}
    body = gmail_io.extract_message({"id": "r1", "payload": payload}, 45000)["body"]
    assert body.strip().startswith("Hello there.") and "<p>" not in body
