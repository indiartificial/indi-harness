"""
tests/test_repair_sheet_headers.py

The one-off repair, against the shapes found on the server on 2026-09-27:
Queue and Responses tabs each holding rows in two layouts (the old
counter-keyed one and the current one) under a header row that matches
neither, with stray copies of the column names on the ends of some rows.

This directory is protected. Indi can read it but not edit it.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import repair_sheet_headers as repair
import sheets_queue

QH = sheets_queue.QUEUE_HEADERS
RH = sheets_queue.RESPONSES_HEADERS
QUEUE_ROW1 = ["thread_id", "", "", ""] + list(QH)
RESP_ROW1 = ["id", "queue_row_id", "response_text", "created_at", "gmail_msg_id", "sender_addr"]
STEVE = '"Steven J. Halasz" <sjhalasz@gmail.com>'


def _gid(i):
    return f"1a09b8ffe5ff{i:04x}"


def _current_q(i, status="replied"):
    return [_gid(i), "1a0d41f2c61f9697", STEVE, "sjhalasz@gmail.com", "Re: test",
            "body", "2026-09-25T21:35:15+00:00", status, "sent"]


def _old_q(n):
    return [str(n), _gid(1000 + n), "1a09b69a5913fb9d", STEVE, "Re: reply failed",
            "This message was received.\r\n", "2026-09-13T16:20:25.623872"]


def _server():
    queue = [QUEUE_ROW1]
    queue += [_current_q(i) for i in range(1, 5)]
    queue += [_current_q(i) + list(QH) for i in range(5, 8)]          # stray names from J
    queue += [_old_q(3) + ["gmail_msg_id", "thread_id", "sender", "sender_addr", "subject"]]
    queue += [_old_q(2), _old_q(1)]
    responses = [RESP_ROW1,
                 ["1", "1", "Dear Steve, first", "2026-08-26T20:44:54.546120"],
                 ["2", "3", "Dear Steve, third", "2026-08-26T21:01:52.742722"],
                 ["3", "99", "orphan reply", "2026-08-26T21:21:20.205240"],
                 [_gid(1), "sjhalasz@gmail.com", "current reply", "2026-09-25T21:40:00+00:00"]]
    outbound = [list(sheets_queue.OUTBOUND_HEADERS),
                ["a@example.com", "hi", "b", "why", "2026-09-20T10:00:00+00:00"]]
    return queue, responses, outbound


class Sheets:
    def __init__(self, tabs):
        self.tabs = tabs
        self.cleared, self.written = [], []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, spreadsheetId, range):
        rows = self.tabs.get((spreadsheetId, range.split("!")[0]), [])
        return SimpleNamespace(execute=lambda: {"values": [list(r) for r in rows]})

    def batchClear(self, spreadsheetId, body):
        self.cleared.extend((spreadsheetId, r) for r in body["ranges"])
        return SimpleNamespace(execute=lambda: {})

    def batchUpdate(self, spreadsheetId, body):
        self.written.extend((spreadsheetId, d["range"], d["values"][0]) for d in body["data"])
        return SimpleNamespace(execute=lambda: {})


def _sheets():
    q, r, o = _server()
    return Sheets({("Q", "Queue"): q, ("R", "Responses"): r, ("R", "Outbound"): o})


IDS = {"queue_id": "Q", "responses_id": "R"}
QUIET = lambda *_: None


def test_both_layouts_are_recognised_and_nothing_is_unknown():
    q, r, o = _server()
    p = repair.plan(q, r, o)
    assert p["unknown"] == []
    assert p["counts"] == {"queue_old": 3, "queue_old_replied": 2, "queue_strays": 3,
                           "responses_old": 3, "responses_unlinked": 1}


def test_old_queue_rows_become_current_rows():
    sheets = _sheets()
    repair.run(sheets, IDS, apply=True, out=QUIET, backup_dir=Path("/tmp"))
    rows = {rng: vals for sid, rng, vals in sheets.written if sid == "Q"}
    old3 = rows["Queue!A9:J9"]
    assert old3[0] == _gid(1003) and old3[3] == "sjhalasz@gmail.com"
    assert old3[6] == "2026-09-13T16:20:25.623872+00:00"
    assert old3[7] == "replied" and "old row id 3" in old3[8]
    assert rows["Queue!A10:J10"][7] == ""                       # id 2: no reply recorded
    assert rows["Queue!A11:J11"][7] == "replied"                 # id 1
    # Debris past the old row's new last column is cleared.
    assert ("Q", "Queue!K9:Z9") in sheets.cleared


def test_old_responses_are_linked_through_the_old_counter():
    sheets = _sheets()
    repair.run(sheets, IDS, apply=True, out=QUIET, backup_dir=Path("/tmp"))
    rows = {rng: vals for sid, rng, vals in sheets.written if sid == "R"}
    assert rows["Responses!A2:D2"] == [_gid(1001), "sjhalasz@gmail.com", "Dear Steve, first",
                                       "2026-08-26T20:44:54.546120+00:00"]
    assert rows["Responses!A3:D3"][0] == _gid(1003)
    assert rows["Responses!A4:D4"][:2] == ["", ""]               # orphan keeps text, no id
    assert "Responses!A5:D5" not in rows                         # current rows untouched


def test_headers_are_rewritten_and_current_rows_only_lose_debris():
    sheets = _sheets()
    repair.run(sheets, IDS, apply=True, out=QUIET, backup_dir=Path("/tmp"))
    written = {(sid, rng): vals for sid, rng, vals in sheets.written}
    assert written[("Q", "Queue!A1")] == list(QH)
    assert written[("R", "Responses!A1")] == list(RH)
    assert ("Q", "Queue!J6:Z6") in sheets.cleared
    assert not any(rng in ("Queue!A2:J2", "Queue!A6:J6") for _, rng in written)


def test_dry_run_writes_nothing(tmp_path):
    sheets = _sheets()
    assert repair.run(sheets, IDS, apply=False, out=QUIET, backup_dir=tmp_path) == 0
    assert sheets.cleared == [] and sheets.written == [] and list(tmp_path.iterdir()) == []


def test_apply_backs_up_first(tmp_path):
    sheets = _sheets()
    repair.run(sheets, IDS, apply=True, out=QUIET, backup_dir=tmp_path)
    backups = list(tmp_path.iterdir())
    assert len(backups) == 1 and "Dear Steve, first" in backups[0].read_text()


def test_any_unrecognisable_row_stops_everything(tmp_path):
    sheets = _sheets()
    sheets.tabs[("Q", "Queue")].append(["subject in column A", "body", "?"])
    assert repair.run(sheets, IDS, apply=True, out=QUIET, backup_dir=tmp_path) == 1
    assert sheets.cleared == [] and sheets.written == []


def test_after_repair_the_store_reads_both_generations():
    q, r, o = _server()
    p = repair.plan(q, r, o)
    fixed_q = [list(QH)] + [list(x) for x in q[1:]]
    for n, new, clear_from in p["edits"]["Queue"]:
        row = new if new is not None else fixed_q[n - 1]
        fixed_q[n - 1] = row[:clear_from] if clear_from is not None else row
    fixed_r = [list(RH)] + [list(x) for x in r[1:]]
    for n, new, _ in p["edits"]["Responses"]:
        fixed_r[n - 1] = new
    store = sheets_queue.SheetStore(Sheets({("Q", "Queue"): fixed_q, ("R", "Responses"): fixed_r,
                                            ("R", "Outbound"): o}), None,
                                    SimpleNamespace(max_body_chars=45000))
    store.queue_id, store.responses_id = "Q", "R"
    ids = [x["gmail_msg_id"] for x in store.messages_for_sender("sjhalasz@gmail.com")]
    assert _gid(1001) in ids and _gid(1003) in ids and _gid(1002) not in ids
    assert store.responses_by_message()[_gid(1001)] == ["Dear Steve, first"]
    assert store.schema_report() == {}


def test_a_data_cell_that_merely_says_status_is_not_debris():
    assert repair._debris_start(_current_q(1) + ["status"], 9) is None
