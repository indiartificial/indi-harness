"""
tests/test_guidelines_attachment.py

Accepting the terms brings the user guidelines back as an attachment,
from data/guidelines/ — and the acceptance still goes out if that file is
missing.

This directory is protected. Indi can read it but not edit it.
"""

import base64
import sys
from email import message_from_bytes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import terms_gate
from test_terms_gate import (STRANGER, FakeClaude, FakeGmail, _cfg, _msg,  # noqa: F401
                             people, terms_dir)


def _parts(mime):
    msg = message_from_bytes(base64.urlsafe_b64decode(mime["raw"]))
    return list(msg.walk())


def _agree(people):
    people.record_terms_sent(STRANGER, "0.2")
    gmail = FakeGmail()
    terms_gate.check_inbound(_cfg(), gmail, FakeClaude(), _msg(body="I AGREE"), people)
    return gmail


def test_the_guidelines_file_ships_with_the_harness():
    path = Path(terms_gate.__file__).parent / "data" / "guidelines" / terms_gate.GUIDELINES_FILE
    text = path.read_text()
    assert "WHO SEES WHAT" in text and "GOOD PRACTICE" in text


def test_accepting_brings_the_guidelines(terms_dir, people):
    parts = _parts(_agree(people).sent[0])
    names = [p.get_filename() for p in parts if p.get_filename()]
    assert names == [terms_gate.GUIDELINES_FILE]
    body = next(p for p in parts if p.get_content_type() == "text/plain"
                and not p.get_filename()).get_payload(decode=True).decode()
    assert "attached Indi's user guidelines" in body and "0.2" in body


def test_without_the_file_the_acceptance_still_goes(terms_dir, people, monkeypatch):
    monkeypatch.setattr(terms_gate, "GUIDELINES_FILE", "missing.txt")
    parts = _parts(_agree(people).sent[0])
    assert not [p for p in parts if p.get_filename()]
    text = "".join(p.get_payload(decode=True).decode() for p in parts
                   if p.get_content_type() == "text/plain")
    assert "Recorded" in text and "attached" not in text
