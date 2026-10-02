"""
tests/test_ledger.py

The open ledger (ledger.py): open questions and commitments per thread,
shown to the writer, kept by a separate step that must justify every
change, and tied to follow-ups.

This directory is protected. Indi can read it but not edit it.
"""

import datetime
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import followups
import ledger
from test_drive_tables import _records_store

ANN, BOB, CAROL = "ann@example.com", "bob@example.com", "carol@example.com"
MSG = {"thread_id": "t1", "gmail_msg_id": "m9", "from_addr": ANN, "body": "The venue said yes."}


def _tool(**inp):
    return SimpleNamespace(type="tool_use", name="update_ledger", input=inp)


class Client:
    def __init__(self, answer):
        self.answer, self.seen = answer, []
        self.messages = self

    def create(self, **kw):
        self.seen.append(kw)
        return SimpleNamespace(content=[self.answer], usage=None)


def _store():
    store, _ = _records_store()
    return store


def test_entries_are_shown_only_to_those_who_saw_them_formed():
    store = _store()
    ledger.add(store, "t1", {ANN, BOB}, ledger.QUESTION, "Has the venue confirmed?")
    assert [e["text"] for e in ledger.open_for(store, "t1", {ANN})] == ["Has the venue confirmed?"]
    assert ledger.open_for(store, "t1", {ANN, CAROL}) == []          # Carol joined later
    assert ledger.open_for(store, "t2", {ANN}) == []
    assert "OPEN ON THIS THREAD" in ledger.render(ledger.open_for(store, "t1", {ANN}))


def test_the_keeper_resolves_with_a_citation_and_records_new_entries():
    store = _store()
    q = ledger.add(store, "t1", {ANN}, ledger.QUESTION, "Has the venue confirmed?")
    closed, dated = [], []
    due = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3)).isoformat()
    client = Client(_tool(
        updates=[{"entry_id": q["entry_id"], "action": "resolved", "settled_by": "message",
                  "reason": "Ann says it's confirmed"}],
        new=[{"kind": "commitment", "text": "Send Ann the agenda", "due_at": due},
             {"kind": "question", "text": "Who brings the projector?"}]))
    changes = ledger.keep(client, store, MSG, "Great — I'll send the agenda by Friday.", {ANN},
                          reply_id="r1", on_closed=closed.append, on_commitment=dated.append)
    assert len(changes) == 3
    entry = next(e for e in ledger.current(store, "t1") if e["entry_id"] == q["entry_id"])
    assert entry["status"] == "resolved" and entry["evidence"] == "m9" and entry["revision"] == "2"
    assert [e["text"] for e in ledger.open_for(store, "t1", {ANN})] == [
        "Send Ann the agenda", "Who brings the projector?"]
    assert closed[0]["entry_id"] == q["entry_id"] and dated[0]["text"] == "Send Ann the agenda"
    # History is kept: two rows for the resolved entry.
    assert len([r for r in ledger._rows(store) if r["entry_id"] == q["entry_id"]]) == 2


def test_unjustified_changes_are_refused():
    store = _store()
    q = ledger.add(store, "t1", {ANN}, ledger.QUESTION, "Q1")
    c = ledger.add(store, "t1", {ANN}, ledger.COMMITMENT, "C1")
    client = Client(_tool(updates=[
        {"entry_id": q["entry_id"], "action": "resolved", "settled_by": "reply"},   # no reply id
        {"entry_id": c["entry_id"], "action": "dropped"},                           # no reason
        {"entry_id": "L-made-up", "action": "dropped", "reason": "x"}], new=[]))
    assert ledger.keep(client, store, MSG, "reply", {ANN}, reply_id="") == []
    assert len(ledger.open_for(store, "t1", {ANN})) == 2


def test_the_keeper_only_sees_what_the_recipients_may():
    store = _store()
    ledger.add(store, "t1", {ANN, BOB}, ledger.QUESTION, "Bob's budget question")
    client = Client(_tool(updates=[], new=[]))
    ledger.keep(client, store, MSG, "reply", {ANN, CAROL})
    assert "Bob's budget question" not in str(client.seen[0]["messages"])


def test_closing_an_entry_cancels_follow_ups_about_it():
    store = _store()
    e = ledger.add(store, "t1", {ANN}, ledger.QUESTION, "Has the venue confirmed?")
    due = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2)).isoformat()
    followups.schedule(store, MSG, {ANN}, {"due_at": due, "note": "check venue",
                                            "about": e["entry_id"]})
    assert followups.cancel_about(store, e["entry_id"], "ledger entry resolved") == 1
    assert followups.pending(store) == []


def test_a_dated_commitment_gets_a_follow_up_unless_one_is_planned():
    store = _store()
    due = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3)).isoformat()
    c = ledger.add(store, "t1", {ANN}, ledger.COMMITMENT, "Send the agenda", due_at=due)
    followups.schedule_for_commitment(store, c, MSG)
    [f] = followups.pending(store)
    assert f["about"] == c["entry_id"] and "Send the agenda" in f["note"]
    c2 = ledger.add(store, "t1", {ANN}, ledger.COMMITMENT, "Book the room", due_at=due)
    assert followups.schedule_for_commitment(store, c2, MSG) == "thread already has a follow-up"


def test_it_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("INDI_LEDGER", "false")
    assert ledger.enabled() is False



from test_reply_all import (Store as ReplyStore, _go, _raw, captured,  # noqa: E402,F401
                            people, reply, terms_dir)
from test_reply_send_path import SENDER, _cfg  # noqa: E402


def test_the_writer_is_shown_whats_open(tmp_path, people, captured):
    store = ReplyStore()
    ledger.add(store, "t1", {SENDER}, ledger.COMMITMENT, "Send the agenda by Friday")
    _go(__import__("test_reply_send_path").FakeGmail(), _cfg(tmp_path), store, people, _raw())
    assert "Send the agenda by Friday" in captured[-1]["ledger_text"]
