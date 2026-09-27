"""
tests/test_drive_tables.py

Steps 6-7 of MIGRATE-GMAIL.md: the non-mail records as CSV files in Drive
(drive_tables.py), the store's records-in-Drive mode, and the one-off copy
(move_records_to_drive.py).

The Drive side is a dictionary of file name -> CSV text. What matters:

  * the Sheets calls the store, reminders and newsletter make behave the
    same over CSV files — reads, appends that report their row, in-place
    updates by row;
  * a file survives a new run (it is re-read from "Drive");
  * with records in Drive, nothing touches a Queue/Responses/Outbound tab;
    mail status goes to labels and the why to the Decisions file;
  * the copy is verbatim, refuses a tab whose header is wrong, and never
    overwrites a file that already holds data.

This directory is protected. Indi can read it but not edit it.
"""

import datetime
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import drive_tables
import gmail_store
import move_records_to_drive as move
import newsletter
import outbound
import sheets_queue
from test_gmail_store import ALICE, INDI, NOW, FakeGmail, _incoming


class DictIO:
    def __init__(self, files=None):
        self.files = dict(files or {})
        self.writes = []

    def list_names(self):
        return set(self.files)

    def find(self, name):
        return name if name in self.files else None

    def read(self, fid):
        return self.files.get(fid, "")

    def write(self, name, text, fid):
        self.files[name] = text
        self.writes.append(name)
        return name


CFG = SimpleNamespace(max_body_chars=45000, gmail_status_labels=True, dry_run=False)


def _records_store(io_=None, gmail=None):
    io_ = io_ or DictIO()
    sheets = drive_tables.DriveSheets(io_)
    store = gmail_store.GmailStore(sheets, None, CFG, gmail or FakeGmail(),
                                   records_in_drive=True)
    store.ensure_spreadsheets()
    return store, io_


def _next_run(store, io_):
    again, _ = _records_store(io_, store.gmail)
    return again


# --------------------------------------------------------------------------
# the Sheets-shaped adapter
# --------------------------------------------------------------------------

def test_ranges_read_and_write_like_sheets():
    io_ = DictIO()
    s = drive_tables.DriveSheets(io_)
    s.update(range="T!A1", body={"values": [["a", "b", "c"]]}).execute()
    res = s.append(range="T!A1", body={"values": [["1", "2", "3"], ["4", "5", "6"]]}).execute()
    assert res["updates"]["updatedRange"].startswith("T!A3")
    s.update(range="T!B2:C2", body={"values": [["x", "y"]]}).execute()
    assert s.get(range="T!A1:Z").execute()["values"] == [["a", "b", "c"], ["1", "x", "y"],
                                                          ["4", "5", "6"]]
    assert s.get(range="T!A1:Z1").execute()["values"] == [["a", "b", "c"]]
    assert "T" in {x["properties"]["title"] for x in s.get(fields="x").execute()["sheets"]}


def test_multiline_cells_survive_the_round_trip():
    io_ = DictIO()
    s = drive_tables.DriveSheets(io_)
    s.update(range="J!A1", body={"values": [["entry"], ['line one\nline "two", three']]}).execute()
    again = drive_tables.DriveSheets(io_)
    assert again.get(range="J!A1:Z").execute()["values"][1] == ['line one\nline "two", three']


# --------------------------------------------------------------------------
# the store over Drive
# --------------------------------------------------------------------------

def test_journal_and_costs_live_in_drive_files():
    store, io_ = _records_store()
    store.append_journal("thinking about tides")
    store.append_costs([{"at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                         "kind": "reply", "model": "m", "usd": "0.25"}])
    again = _next_run(store, io_)
    assert [r["entry"] for r in again.recent_journal()] == ["thinking about tides"]
    assert again.spend_since(24) == (0.25, 1)
    assert set(io_.files) >= {drive_tables.file_name("Journal"),
                              drive_tables.file_name("Costs"),
                              drive_tables.file_name("Decisions")}
    assert again.schema_report() == {}


def test_reminder_status_updates_land_on_the_right_row():
    store, io_ = _records_store()
    for i in range(3):
        outbound._append_reminder(store, {"due_at": f"2026-10-0{i + 1}T09:00:00+00:00",
                                          "recipient": ALICE, "subject": f"r{i}",
                                          "status": outbound.STATUS_SCHEDULED,
                                          "reminder_id": f"id{i}"})
    rows = outbound._reminder_rows(_next_run(store, io_))
    outbound._set_reminder_status(store, rows[1], outbound.STATUS_SENT, "2026-10-02T09:00:01")
    after = outbound._reminder_rows(_next_run(store, io_))
    assert [r["status"] for r in after] == ["scheduled", "sent", "scheduled"]
    assert after[1]["sent_at"] == "2026-10-02T09:00:01" and after[1]["subject"] == "r1"


def test_newsletter_subscribers_work_over_drive():
    store, io_ = _records_store()
    assert newsletter.list_subscribers(store, CFG) == [newsletter.SEED_SUBSCRIBER]
    newsletter.add_subscriber(store, ALICE, CFG, added_by="test")
    newsletter.remove_subscriber(_next_run(store, io_), ALICE, CFG)
    assert newsletter.list_subscribers(_next_run(store, io_), CFG) == [newsletter.SEED_SUBSCRIBER]


def test_no_mail_tab_is_written_and_the_why_goes_to_decisions(monkeypatch):
    monkeypatch.setattr(gmail_store, "_utc_now", lambda: NOW)
    g = FakeGmail()
    store, io_ = _records_store(gmail=g)
    row = store.append_queue_row(_incoming(g, "m1"))
    store.set_status(row, sheets_queue.STATUS_SKIPPED, "per-sender daily cap reached (10)")
    g.add("r1", INDI, ALICE, labels=("SENT",))
    store.append_response("m1", ALICE, "text", reply_id="r1")
    g.add("o1", INDI, ALICE, labels=("SENT",))
    store.append_outbound(ALICE, "hi", "body", "checking in after the move", message_id="o1")
    store.set_attachments(row, ["m1/a.txt"])

    for tab in (sheets_queue.QUEUE_TAB, sheets_queue.RESPONSES_TAB, sheets_queue.OUTBOUND_TAB):
        assert drive_tables.file_name(tab) not in io_.files
    assert "Indi/Skipped" in g.names("m1")
    assert "Indi/Reply" in g.names("r1") and "Indi/Outbound" in g.names("o1")
    decisions = store._load(store.responses_id, gmail_store.DECISIONS_TAB)
    assert [(d["kind"], d["gmail_msg_id"], d["note"]) for d in decisions] == [
        ("status", "m1", "per-sender daily cap reached (10)"),
        ("outbound", "o1", "checking in after the move"),
    ]
    assert _next_run(store, io_).find_queue_row("m1")["status"] == "skipped"
    assert store.outbound_today_to(ALICE) == 1


def test_records_mode_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("INDI_DRIVE_RECORDS", raising=False)
    assert gmail_store.drive_records_enabled(SimpleNamespace()) is False
    monkeypatch.setenv("INDI_DRIVE_RECORDS", "true")
    assert gmail_store.drive_records_enabled(SimpleNamespace()) is True


# --------------------------------------------------------------------------
# the one-off copy
# --------------------------------------------------------------------------

class SourceSheets:
    def __init__(self, tabs):
        self.tabs = tabs

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, spreadsheetId, range=None, fields=None):
        if range is None:
            return SimpleNamespace(execute=lambda: {"sheets": [{"properties": {"title": t}}
                                                               for t in self.tabs]})
        return SimpleNamespace(execute=lambda: {"values": self.tabs[range.split("!")[0]]})


def _source():
    return SourceSheets({
        "Journal": [list(sheets_queue.JOURNAL_HEADERS), ["first entry", "2026-09-01T00:00:00+00:00"]],
        "Costs": [["wrong", "header"], ["x"]],
        "Reminders": [list(outbound.REMINDERS_HEADERS),
                      ["2026-10-01T09:00:00+00:00", ALICE, "a", "b", "sent"],
                      [],
                      ["2026-10-02T09:00:00+00:00", ALICE, "c", "d", "scheduled"]],
    })


def test_copy_is_verbatim_refuses_bad_headers_and_builds_decisions():
    target = drive_tables.DriveSheets(DictIO())
    queue = [{"gmail_msg_id": "m1", "received_at": "2026-09-02", "status": "skipped",
              "sender_addr": ALICE, "note": "no-reply style sender address"},
             {"gmail_msg_id": "m2", "received_at": "2026-09-03", "status": "replied",
              "sender_addr": ALICE, "note": ""}]
    outb = [{"recipient": ALICE, "created_at": "2026-09-04", "rationale": "a check-in"}]
    lines = []
    todo = move.plan(_source(), "R", queue, outb, target, out=lines.append)
    tabs = [t for t, _ in todo]
    assert tabs == ["Journal", "Reminders", "Decisions"]
    assert any("Costs: NOT COPIED" in l for l in lines)
    for tab, values in todo:
        target.update(range=f"{tab}!A1", body={"values": values}).execute()
    again = drive_tables.DriveSheets(target.io)
    rem = again.get(range="Reminders!A1:Z").execute()["values"]
    assert rem[2] == [] and rem[3][2] == "c"           # the blank row keeps its place
    dec = again.get(range="Decisions!A1:Z").execute()["values"]
    assert dec[1][1] == "m1" and dec[2][2] == "outbound" and len(dec) == 3


def test_copy_never_overwrites_a_file_with_data():
    io_ = DictIO({drive_tables.file_name("Journal"):
                  "entry,created_at\nalready here,2026-09-27T00:00:00+00:00\n"})
    lines = []
    todo = move.plan(_source(), "R", [], [], drive_tables.DriveSheets(io_), out=lines.append)
    assert "Journal" not in [t for t, _ in todo]
    assert any("Journal: already in Drive" in l for l in lines)
