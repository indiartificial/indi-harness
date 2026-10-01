"""
tests/test_followups.py

Follow-ups Indi plans for itself (followups.py): scheduled while
replying, acted on when due — sent to the same people or skipped with a
reason — drawing only on what those people have seen.

This directory is protected. Indi can read it but not edit it.
"""

import base64
import datetime
import sys
from email import message_from_bytes
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import claude_api
import followups as fu
from test_audience import ANN, BOB, CAROL, INDI, world  # noqa: F401
from test_consult import Client, Mail as _Mail, _text, _tool


class Mail(_Mail):
    def threads(self):
        return self.g.threads()

CFG = SimpleNamespace(max_tokens=2000, dry_run=False, send_automatically=True,
                      max_body_chars=45000)
NOW = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture
def setup(world, monkeypatch):
    g, store = world
    store.sheets.tabs[("R", fu.TAB)] = [list(fu.HEADERS)]
    monkeypatch.setattr(fu, "_now", lambda: NOW)
    return g, store, Mail(g)


def _msg(thread="t-shared", mid="b-shared", frm=BOB):
    return {"thread_id": thread, "gmail_msg_id": mid, "from_addr": frm}


def _plan(store, hours=24, note="check the venue quote came", thread="t-shared",
          recipients=(BOB, ANN)):
    return fu.schedule(store, _msg(thread), recipients,
                       {"due_at": (NOW + datetime.timedelta(hours=hours)).isoformat(),
                        "note": note}, now=NOW)


def _sent(mail):
    m = message_from_bytes(base64.urlsafe_b64decode(mail.sent[-1]["raw"]))
    return m, m.get_payload(decode=True).decode()


# --------------------------------------------------------------------------
# scheduling
# --------------------------------------------------------------------------

def test_scheduling_checks_the_time_and_note(setup):
    _, store, _ = setup
    assert _plan(store, hours=0.25)[1] and _plan(store, hours=24 * 61)[1]
    assert fu.schedule(store, _msg(), {BOB}, {"due_at": "next week", "note": "x"}, now=NOW)[1]
    assert fu.schedule(store, _msg(), {BOB}, {"due_at": (NOW + datetime.timedelta(days=2)).isoformat(),
                                               "note": " "}, now=NOW)[1]
    assert fu.pending(store) == []
    text, err = _plan(store)
    assert not err and "planned" in text
    [row] = fu.pending(store)
    assert row["recipients"] == f"{ANN}, {BOB}" and row["owner"] == BOB


def test_one_per_thread(setup):
    _, store, _ = setup
    _plan(store, note="first")
    _plan(store, note="second")
    assert [r["note"] for r in fu.pending(store)] == ["second"]


def test_listing_and_cancelling_only_what_concerns_the_sender(setup):
    _, store, _ = setup
    _plan(store)
    _plan(store, thread="t-other", recipients=(CAROL,))
    text, _ = fu.handle_reply_tool(store, _msg(frm=ANN), {ANN}, fu.LIST_TOOL, {}, False)
    assert "venue quote" in text and CAROL not in text
    assert fu.handle_reply_tool(store, _msg(frm=ANN), {ANN, BOB}, fu.LIST_TOOL, {}, True)[1]
    fu.handle_reply_tool(store, _msg(frm=ANN), {ANN}, fu.CANCEL_TOOL, {"which": "all"}, False)
    assert [r["recipients"] for r in fu.pending(store)] == [CAROL]


# --------------------------------------------------------------------------
# acting on them
# --------------------------------------------------------------------------

def _run(store, mail, client, people=None, hours=25):
    return fu.run_due(client, CFG, mail, store, "sys", {INDI}, people, "m",
                      now=NOW + datetime.timedelta(hours=hours))


def test_a_due_follow_up_is_sent_in_the_thread_to_the_same_people(setup):
    g, store, mail = setup
    _plan(store)
    out = _run(store, mail, Client([_tool(fu.SEND_TOOL, body="Did the quote arrive?")]))
    assert len(out) == 1 and out[0].endswith(" sent")
    m, body = _sent(mail)
    assert m["To"] == BOB and m["Cc"] == ANN and m["In-Reply-To"]
    assert "Did the quote arrive?" in body and "--- Indi" in body
    assert fu.pending(store) == []


def test_not_yet_due_is_left_alone(setup):
    _, store, mail = setup
    _plan(store, hours=48)
    assert _run(store, mail, Client(), hours=25) == [] and mail.sent == []


def test_skipping_is_recorded_with_the_reason(setup):
    _, store, mail = setup
    _plan(store)
    out = _run(store, mail, Client([_tool(fu.SKIP_TOOL, reason="Ann already sent it")]))
    assert "skipped: Ann already sent it" in out[0] and mail.sent == []
    row = fu._rows(store)[-1]
    assert row["status"] == fu.SKIPPED and row["outcome"] == "Ann already sent it"


def test_the_thread_is_shown_as_its_recipients_saw_it(setup):
    g, store, mail = setup
    # Carol joined the thread late (b-later); a follow-up for Ann and Carol
    # must not show the earlier message Carol never received.
    _plan(store, recipients=(BOB, ANN, CAROL))
    client = Client([_tool(fu.SKIP_TOOL, reason="fine")])
    _run(store, mail, client)
    shown = client.seen[0]["messages"][0]["content"]
    assert "carol joins" in shown and "budget tight" not in shown


def test_the_daily_budget(setup, monkeypatch):
    _, store, mail = setup
    monkeypatch.setattr(fu, "per_day", lambda: 1)
    _plan(store)
    _plan(store, thread="t-b-private", recipients=(BOB,))
    out = _run(store, mail, Client([_tool(fu.SEND_TOOL, body="one")],
                                   [_tool(fu.SEND_TOOL, body="two")]))
    assert len(mail.sent) == 1 and "daily budget" in out[-1]


def test_errors_are_retried_then_given_up(setup):
    _, store, mail = setup
    _plan(store)

    class Broken:
        messages = SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(RuntimeError()))
    for _ in range(3):
        _run(store, mail, Broken())
    assert fu._rows(store)[-1]["status"] == fu.FAILED and mail.sent == []


def test_someone_who_declined_is_not_written_to(setup):
    _, store, mail = setup
    _plan(store)
    people = SimpleNamespace(get=lambda a: SimpleNamespace(
        has_accepted=(a == ANN), status="declined" if a == BOB else "accepted"))
    out = _run(store, mail, Client(), people=people)
    assert "skipped (terms)" in out[0] and mail.sent == []


def test_offered_on_any_reply_but_list_and_cancel_only_solo():
    cfg = SimpleNamespace(enable_web_search=False, enable_web_fetch=False,
                          enable_code_execution=False, enable_history_search=False,
                          enable_outbound=False, betas=lambda **kw: [])
    names = lambda shared: {t["name"] for t in claude_api.build_tools(
        cfg, False, True, reminder_sender=ANN, shared_reply=shared)[0]}
    assert fu.REPLY_TOOL_NAMES <= names(False)
    assert fu.SCHEDULE_TOOL in names(True) and fu.LIST_TOOL not in names(True)
