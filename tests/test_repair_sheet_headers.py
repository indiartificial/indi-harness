"""
tests/test_repair_sheet_headers.py

The one-off header repair, against the exact shape found on the server on
2026-09-27: a Queue row 1 reading `thread_id, "", "", "", gmail_msg_id,
thread_id, sender, ...`, data rows laid out as declared, and older rows
carrying a copy of the column names from column J.

This directory is protected. Indi can read it but not edit it.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import repair_sheet_headers as repair
import sheets_queue

H = sheets_queue.QUEUE_HEADERS
BROKEN_ROW1 = ["thread_id", "", "", ""] + list(H)


def _data(i, status="replied"):
    return [f"1a0da7ead45a{i:04x}", "1a0d41f2c61f9697", '"S" <s@example.com>',
            "s@example.com", "Re: test", "body", "2026-09-25T21:35:15+00:00", status, "sent"]


class Sheets:
    def __init__(self, tabs):
        self.tabs = tabs
        self.cleared, self.updated = [], []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, spreadsheetId, range):
        tab = range.split("!")[0]
        return SimpleNamespace(execute=lambda: {"values": [list(r) for r in self.tabs.get(tab, [])]})

    def batchClear(self, spreadsheetId, body):
        self.cleared.extend(body["ranges"])
        return SimpleNamespace(execute=lambda: {})

    def update(self, spreadsheetId, range, valueInputOption, body):
        self.updated.append((range, body["values"]))
        return SimpleNamespace(execute=lambda: {})


def _server_shape():
    rows = [BROKEN_ROW1] + [_data(i) for i in range(1, 8)]
    rows += [_data(i) + list(H) for i in range(8, 30)]          # stray names from J
    rows += [_data(99, "skipped")]
    return rows


def test_the_server_shape_is_recognised_and_safe():
    plan = repair.plan_tab(_server_shape(), H, repair._queue_ok)
    assert not plan["header_ok"] and plan["safe"] and plan["bad_rows"] == []
    assert set(plan["strays"].values()) == {9}                    # column J
    assert len(plan["strays"]) == 22


def test_dry_run_changes_nothing(tmp_path):
    sheets = Sheets({"Queue": _server_shape()})
    assert repair.run(sheets, {"queue_id": "Q"}, apply=False, out=lambda *_: None,
                      backup_dir=tmp_path) == 0
    assert sheets.cleared == [] and sheets.updated == []
    assert list(tmp_path.iterdir()) == []


def test_apply_rewrites_row1_clears_strays_and_backs_up_first(tmp_path):
    sheets = Sheets({"Queue": _server_shape()})
    repair.run(sheets, {"queue_id": "Q"}, apply=True, out=lambda *_: None,
               backup_dir=tmp_path)
    assert sheets.updated == [("Queue!A1", [list(H)])]
    assert sheets.cleared[0] == "Queue!A1:Z1"
    assert "Queue!J10:Z10" in sheets.cleared and len(sheets.cleared) == 23
    # Data columns are never in a cleared range.
    assert all(r.split("!")[1][0] in "AJ" for r in sheets.cleared)
    backups = list(tmp_path.iterdir())
    assert len(backups) == 1 and "Re: test" in backups[0].read_text()


def test_a_tab_whose_data_does_not_fit_is_left_alone(tmp_path):
    shifted = [BROKEN_ROW1] + [["Re: test", "body"] + _data(i) for i in range(10)]
    sheets = Sheets({"Queue": shifted})
    code = repair.run(sheets, {"queue_id": "Q"}, apply=True, out=lambda *_: None,
                      backup_dir=tmp_path)
    assert code == 1 and sheets.cleared == [] and sheets.updated == []


def test_a_correct_tab_is_not_touched(tmp_path):
    sheets = Sheets({"Queue": [list(H)] + [_data(i) for i in range(5)]})
    assert repair.run(sheets, {"queue_id": "Q"}, apply=True, out=lambda *_: None,
                      backup_dir=tmp_path) == 0
    assert sheets.updated == [] and sheets.cleared == []


def test_a_data_cell_that_merely_says_status_is_not_debris():
    row = _data(1) + ["status"]
    assert repair.stray_header_start(row, H) is None


def test_after_repair_the_store_reads_real_ids():
    rows = [list(H)] + [_data(i)[:9] for i in range(3)]
    store = sheets_queue.SheetStore(None, None, SimpleNamespace(max_body_chars=45000))
    store.sheets = Sheets({"Queue": rows})
    store.queue_id = "Q"
    got = store.queue_rows()
    assert got[0]["gmail_msg_id"].startswith("1a0da7") and got[0]["status"] == "replied"
