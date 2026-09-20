"""
tests/test_thread_context.py

A reply carries only the sentences the sender just typed; everything
earlier is quoted below it, and the parser strips that as noise. For a
while nothing put the earlier conversation back, so "Yes, please do."
reached the model as three words with no referent and Indi had to answer
without knowing what it was agreeing to.

These tests cover the two halves of the fix: the quoted chain is split off
rather than destroyed, and the real earlier messages are fetched from the
thread and rendered as context.
"""

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gmail_io


STEVE = '"Steven J. Halasz" <sjhalasz@gmail.com>'
INDI = "Indi Artificial <indiartificial@gmail.com>"
SELVES = {"indiartificial@gmail.com"}

# The attribution line Gmail writes, wrapped exactly the way Gmail wraps
# it: the address runs past the margin and "wrote:" lands on its own line.
WRAPPED_ATTRIBUTION = (
    "On Sun, Sep 20, 2026 at 06:55 Indi Artificial <indiartificial@gmail.com>\n"
    "wrote:"
)


def gmail_message(msg_id, from_header, text, internal_date=1758300000000,
                  thread_id="t1"):
    return {
        "id": msg_id,
        "threadId": thread_id,
        "internalDate": str(internal_date),
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": from_header},
                {"name": "Subject", "value": "Re: your system prompt"},
                {"name": "Message-ID", "value": f"<{msg_id}@mail>"},
            ],
            "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()},
        },
    }


# --------------------------------------------------------------------------
# SPLITTING A REPLY FROM ITS QUOTED CHAIN
# --------------------------------------------------------------------------

def test_wrapped_attribution_line_is_recognised():
    body = f"Yes, please do.\n\n{WRAPPED_ATTRIBUTION}\n\n> Steve,\n>\n> --- Indi\n"
    visible, quoted = gmail_io.split_quoted(body)
    assert visible == "Yes, please do."
    # The marker used to survive the split and the text it introduced did
    # not, leaving a body that ended on a bare "wrote:".
    assert not visible.endswith("wrote:")
    assert "Steve," in quoted


def test_quoted_chain_is_returned_not_discarded():
    body = "Thoughts?\n\n> the vendor quote was 4,200 for the extended term\n"
    visible, quoted = gmail_io.split_quoted(body)
    assert visible == "Thoughts?"
    assert "4,200" in quoted


def test_message_that_is_only_a_quote_still_has_a_body():
    body = "> the vendor quote was 4,200 for the extended term\n"
    msg = gmail_io.extract_message(gmail_message("m1", STEVE, body), 45000)
    assert "4,200" in msg["body"]


def test_quoted_budget_controls_what_is_kept():
    body = f"Yes, please do.\n\n{WRAPPED_ATTRIBUTION}\n\n> earlier conversation\n"
    raw = gmail_message("m1", STEVE, body)

    default = gmail_io.extract_message(raw, 45000)
    assert default["quoted"] == ""

    kept = gmail_io.extract_message(raw, 45000, max_quoted_chars=6000)
    assert "earlier conversation" in kept["quoted"]
    assert kept["body"] == "Yes, please do."

    clipped = gmail_io.extract_message(raw, 45000, max_quoted_chars=20)
    assert clipped["quoted_truncated"]
    assert len(clipped["quoted"]) < len(kept["quoted"])


def test_a_reply_with_only_quoted_text_is_not_treated_as_empty():
    msg = gmail_io.extract_message(
        gmail_message("m1", STEVE, "> what do you make of this?\n"),
        45000, max_quoted_chars=6000,
    )
    should_reply, reason = gmail_io.classify_message(msg, SELVES)
    assert should_reply, reason


# --------------------------------------------------------------------------
# THE REST OF THE THREAD
# --------------------------------------------------------------------------

def _thread():
    base = 1758300000000
    return [
        gmail_message("m1", STEVE, "Have a look at your system prompt.", base),
        gmail_message("m2", INDI,
                      "Steve,\n\nI'd change the reminders section; it says I "
                      "can never follow up.\n\n--- Indi", base + 3_600_000),
        gmail_message("m3", STEVE,
                      f"Yes, please do.\n\n{WRAPPED_ATTRIBUTION}\n\n"
                      "> I'd change the reminders section; it says I\n"
                      "> can never follow up.\n", base + 7_200_000),
    ]


def test_thread_context_is_oldest_first_and_excludes_the_new_message():
    text, count = gmail_io.format_thread_context(
        _thread(), exclude_msg_id="m3", self_addrs=SELVES)
    assert count == 2
    assert "Yes, please do." not in text
    assert text.index("Have a look") < text.index("reminders section")


def test_thread_context_marks_indis_own_messages():
    text, _ = gmail_io.format_thread_context(
        _thread(), exclude_msg_id="m3", self_addrs=SELVES)
    own = [line for line in text.splitlines() if "this was you" in line]
    assert len(own) == 1
    assert "indiartificial@gmail.com" in own[0]


def test_thread_context_keeps_only_new_text_from_each_message():
    text, _ = gmail_io.format_thread_context(
        _thread() + [gmail_message(
            "m4", INDI, f"Done.\n\n{WRAPPED_ATTRIBUTION}\n\n> Yes, please do.\n",
            1758300000000 + 10_800_000)],
        exclude_msg_id="m3", self_addrs=SELVES)
    # Each message contributes itself once, not once per quotation of it.
    assert text.count("reminders section") == 1
    assert "wrote:" not in text


def test_thread_context_respects_its_budgets():
    thread = _thread()
    text, count = gmail_io.format_thread_context(
        thread, exclude_msg_id="m3", self_addrs=SELVES, max_messages=1)
    assert count == 1
    # When messages are dropped, the newest are the ones kept.
    assert "reminders section" in text

    text, _ = gmail_io.format_thread_context(
        thread, exclude_msg_id="m3", self_addrs=SELVES, max_chars=120)
    assert len(text) <= 120 + len("\n\n[... earlier thread truncated ...]")


def test_empty_or_unfetchable_thread_is_not_an_error():
    assert gmail_io.format_thread_context([]) == ("", 0)
    assert gmail_io.format_thread_context([{"id": "x"}]) == ("", 0)


# --------------------------------------------------------------------------
# NOT SENDING THE SAME HISTORY TWICE
# --------------------------------------------------------------------------

def test_a_quote_of_the_thread_is_recognised_as_redundant():
    thread = _thread()
    current = gmail_io.extract_message(thread[-1], 45000, max_quoted_chars=6000)
    text, _ = gmail_io.format_thread_context(
        thread, exclude_msg_id="m3", self_addrs=SELVES)
    # The quotation re-wraps the text it quotes; that must not read as new.
    assert not gmail_io.quoted_adds_context(current["quoted"], text)


def test_a_forward_from_another_mailbox_is_kept():
    forwarded = (
        "Can you make sense of this?\n\n"
        "---------- Forwarded message ----------\n"
        "From: Dana <dana@example.org>\n\n"
        "> The vendor says the renewal price is locked until March and the\n"
        "> earlier quote of 4,200 no longer applies to the extended term.\n"
    )
    msg = gmail_io.extract_message(
        gmail_message("m9", STEVE, forwarded), 45000, max_quoted_chars=6000)
    thread_text, _ = gmail_io.format_thread_context(
        _thread(), exclude_msg_id="m3", self_addrs=SELVES)
    assert msg["body"] == "Can you make sense of this?"
    assert gmail_io.quoted_adds_context(msg["quoted"], thread_text)


def test_quoted_text_is_kept_when_there_is_no_thread_context():
    assert gmail_io.quoted_adds_context(
        "> The vendor says the renewal price is locked until March and the\n"
        "> earlier quote no longer applies.", "")
    assert not gmail_io.quoted_adds_context("", "anything at all")


def test_prose_starting_with_on_does_not_break_the_comparison():
    # "On thinking: ..." is a sentence, not an attribution line. Dropping
    # it from one copy of a message and not the other made a quotation of
    # the thread look like new material and doubled it into the prompt.
    said = ("On thinking: the mechanism you're describing already exists, "
            "but it deliberately isn't email. The heartbeat can write a "
            "journal entry instead, stored in a tab and read back later.")
    thread_text = f"[2026-09-20 10:40 UTC] {INDI} — this was you:\n{said}"
    quoted = f"{WRAPPED_ATTRIBUTION}\n\n" + "\n".join(
        f"> {line}" for line in
        ["On thinking: the mechanism you're describing already",
         "exists, but it deliberately isn't email. The heartbeat",
         "can write a journal entry instead, stored in a tab and",
         "read back later."])
    assert not gmail_io.quoted_adds_context(quoted, thread_text)
