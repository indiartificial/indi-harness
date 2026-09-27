"""
tests/test_reply_all.py

Phase 2, second piece (reply_all.py and its place in process_message):
replies go to everyone on the thread; nobody gets one before accepting
the terms; a newcomer's reply is composed from what they have seen.

Scenario numbers refer to the design doc's test scenarios.

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

import check_and_reply
import claude_api
import drive_tables
import reply_all
import sheets_queue
import terms
from test_drive_tables import DictIO
from test_reply_send_path import (SENDER as ANN, STATS, FakeClaude, FakeGmail,
                                  FakeStore, _cfg, people, reply, terms_dir)  # noqa: F401

INDI = "indi@example.org"
CAROL = "carol@example.com"
DAVE = "dave@example.com"


class Store(FakeStore):
    """The reply-path fake, plus the tables reply_all.py keeps its waiting
    list in, and the flag that says the audience rule is available."""

    supports_audience = True

    def __init__(self):
        super().__init__()
        self.table = sheets_queue.SheetStore(drive_tables.DriveSheets(DictIO()), None,
                                             SimpleNamespace(max_body_chars=45000))
        self.table.responses_id = self.responses_id = drive_tables.RECORDS_ID
        self.sheets = self.table.sheets

    def _ensure_tab(self, *a):
        return self.table._ensure_tab(*a)

    def _load(self, *a, **k):
        return self.table._load(*a, **k)

    def _append(self, *a):
        return self.table._append(*a)


def _raw(to=INDI, cc="", frm=ANN, mid="m1", body="lunch thursday?"):
    headers = [{"name": "From", "value": frm}, {"name": "To", "value": to},
               {"name": "Subject", "value": "lunch"},
               {"name": "Message-ID", "value": f"<{mid}@example.com>"}]
    if cc:
        headers.append({"name": "Cc", "value": cc})
    return {"id": mid, "threadId": "t1", "internalDate": "1700000000000",
            "payload": {"mimeType": "text/plain", "headers": headers,
                        "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}}


def _headers(mime):
    return message_from_bytes(base64.urlsafe_b64decode(mime["raw"]))


@pytest.fixture
def captured(monkeypatch, reply):
    calls = []

    def fake(*a, **k):
        calls.append(k)
        return "Thursday works.\n\n--- Indi", dict(STATS)
    monkeypatch.setattr(claude_api, "generate_reply", fake)
    return calls


def _go(gmail, cfg, store, people, raw, **k):
    return check_and_reply.process_message(gmail, None, FakeClaude(), cfg, store, raw,
                                           {INDI}, "system prompt", people, **k)


def _accept(people, addr):
    people.record_acceptance(addr, terms.load().version)


# --------------------------------------------------------------------------

def test_reply_goes_to_everyone_who_has_accepted(tmp_path, people, captured):
    _accept(people, CAROL)
    gmail, store = FakeGmail(), Store()
    _go(gmail, _cfg(tmp_path, reply_all=True), store, people, _raw(cc=CAROL))
    sent = _headers(gmail.sent[-1])
    assert sent["To"] == ANN and sent["Cc"] == CAROL
    k = captured[-1]
    assert k["recipients"] == {ANN, CAROL}
    # Ann's memory file is hers; with Carol on the reply it stays out.
    assert k["memory_body"] == "" and k["pending_text"] == ""


def test_with_the_setting_off_only_the_sender_is_answered(tmp_path, people, captured):
    gmail, store = FakeGmail(), Store()
    _go(gmail, _cfg(tmp_path, reply_all=False), store, people, _raw(cc=CAROL))
    assert _headers(gmail.sent[-1])["Cc"] is None
    assert captured[-1]["recipients"] == {ANN}


def test_a_store_without_the_audience_rule_never_replies_all(tmp_path, people, captured):
    gmail = FakeGmail()
    _go(gmail, _cfg(tmp_path, reply_all=True), FakeStore(), people, _raw(cc=CAROL))
    assert _headers(gmail.sent[-1])["Cc"] is None


def test_bcc_is_answered_to_the_sender_alone(tmp_path, people, captured):   # scenario 8
    gmail, store = FakeGmail(), Store()
    _go(gmail, _cfg(tmp_path, reply_all=True), store, people, _raw(to=CAROL, cc=DAVE))
    assert len(gmail.sent) == 1 and _headers(gmail.sent[0])["Cc"] is None


def test_no_reply_addresses_and_decliners_are_left_off(tmp_path, people, captured):
    people.record_decline(DAVE)
    gmail, store = FakeGmail(), Store()
    _go(gmail, _cfg(tmp_path, reply_all=True), store, people,
        _raw(cc=f"noreply@example.com, {DAVE}"))
    assert _headers(gmail.sent[-1])["Cc"] is None


def test_someone_who_has_not_accepted_is_papered_and_the_reply_waits(
        tmp_path, people, captured):                                       # scenario 9
    gmail, store = FakeGmail(), Store()
    cfg = _cfg(tmp_path, reply_all=True)
    outcome = _go(gmail, cfg, store, people, _raw(cc=CAROL))
    assert "awaiting terms" in outcome and captured == []
    assert len(gmail.sent) == 1
    terms_mail = _headers(gmail.sent[0])
    assert terms_mail["To"] == CAROL and "Terms of Service" in terms_mail["Subject"]
    assert gmail.read == ["m1"]
    assert store.rows[-1]["status"] == sheets_queue.STATUS_QUEUED
    # Seen again (a retry): no second set of terms, no second hold.
    _go(gmail, cfg, store, people, _raw(cc=CAROL))
    assert len(gmail.sent) == 1
    assert len([r for r in reply_all._rows(store) if r["status"] == "waiting"]) == 1


def test_the_wait_ends_when_everyone_has_answered_or_time_is_up(tmp_path, people, captured):
    gmail, store = FakeGmail(), Store()
    _go(gmail, _cfg(tmp_path, reply_all=True), store, people,
        _raw(cc=f"{CAROL}, {DAVE}"))
    assert reply_all.due(store, people) == []
    _accept(people, CAROL)
    assert reply_all.due(store, people) == []               # Dave still undecided
    people.record_decline(DAVE)
    [(row, kept)] = reply_all.due(store, people)
    assert kept == [CAROL]


def test_silence_past_72_hours_drops_the_silent(tmp_path, people, captured):
    gmail, store = FakeGmail(), Store()
    _go(gmail, _cfg(tmp_path, reply_all=True), store, people,
        _raw(cc=f"{CAROL}, {DAVE}"))
    _accept(people, CAROL)
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=73)
    [(row, kept)] = reply_all.due(store, people, now=later)
    assert kept == [CAROL]


def test_a_released_reply_goes_to_the_sender_and_those_who_accepted(
        tmp_path, people, captured):
    gmail, store = FakeGmail(), Store()
    cfg = _cfg(tmp_path, reply_all=True)
    _go(gmail, cfg, store, people, _raw(cc=f"{CAROL}, {DAVE}"))
    _accept(people, CAROL)
    people.record_decline(DAVE)
    [(row, kept)] = reply_all.due(store, people)
    reply_all.mark_released(store, row, kept, [DAVE])
    _go(gmail, cfg, store, people, _raw(cc=f"{CAROL}, {DAVE}"), released_to=kept)
    sent = _headers(gmail.sent[-1])
    assert sent["To"] == ANN and sent["Cc"] == CAROL
    assert reply_all.due(store, people) == []


def test_a_newcomer_is_not_shown_earlier_messages(tmp_path, people, captured, monkeypatch):
    _accept(people, CAROL)                                                 # scenario 7
    earlier = [_raw(to=INDI, mid="p1", body="just between us: budget is tight"),
               _raw(to=INDI, cc=CAROL, mid="p2", body="carol, meet us thursday")]
    for m, when in zip(earlier, ("1699990000000", "1699995000000")):
        m["internalDate"] = when
    monkeypatch.setattr(check_and_reply, "get_thread_messages",
                        lambda gmail, tid: earlier + [_raw(cc=CAROL)])
    gmail, store = FakeGmail(), Store()
    cfg = _cfg(tmp_path, reply_all=True, max_thread_messages=10, max_thread_chars=12000)
    _go(gmail, cfg, store, people, _raw(cc=CAROL))
    thread_text = captured[-1]["thread_text"]
    assert "meet us thursday" in thread_text
    assert "budget is tight" not in thread_text


def test_paper_sends_the_terms_at_most_twice(tmp_path, people):
    gmail = FakeGmail()
    cfg = _cfg(tmp_path)
    msg = {"subject": "lunch", "from_raw": ANN, "from_addr": ANN}
    for _ in range(3):
        reply_all.paper(cfg, gmail, people, DAVE, msg)
    assert len(gmail.sent) == 2
