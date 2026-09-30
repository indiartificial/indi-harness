"""
tests/test_unwrap.py

Newsletter issues go out one line per paragraph (gmail_io.unwrap_paragraphs
via newsletter.compose_body), without disturbing lists, short lines or
the signature.

This directory is protected. Indi can read it but not edit it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import newsletter
from gmail_io import unwrap_paragraphs

WRAPPED = """Today I spent most of the morning untangling a question about who had
seen which message on a thread whose Cc list kept changing, and it turned
out to be simpler than it looked.

Things I changed my mind about:
- Consultation should come after the reply, not before it, because nobody
  should wait three days for an answer.
- Short notes are better than long ones.

Tuesday
Punta Gorda

--- Indi"""


def test_wrapped_paragraphs_become_single_lines():
    out = unwrap_paragraphs(WRAPPED).split("\n")
    assert out[0].startswith("Today I spent") and out[0].endswith("than it looked.")
    assert out[1] == ""


def test_lists_short_lines_and_signature_stay_put():
    out = unwrap_paragraphs(WRAPPED).split("\n")
    assert "Things I changed my mind about:" in out
    item = next(l for l in out if l.startswith("- Consultation"))
    assert item.endswith("three days for an answer.")          # hanging indent joined
    assert "- Short notes are better than long ones." in out
    assert "Tuesday" in out and "Punta Gorda" in out
    assert "--- Indi" in out


def test_the_words_are_unchanged():
    assert unwrap_paragraphs(WRAPPED).split() == WRAPPED.split()


def test_an_issue_goes_out_unwrapped_with_its_unsubscribe_line():
    body = newsletter.compose_body(WRAPPED)
    assert "Today I spent most of the morning untangling a question about who had seen" in body
    assert body.endswith(newsletter.UNSUBSCRIBE_LINE)


def test_the_newsletter_is_asked_for_one_line_paragraphs():
    from types import SimpleNamespace
    assert "one unbroken line" in newsletter.mandate(SimpleNamespace(), ["a@b.c"])
