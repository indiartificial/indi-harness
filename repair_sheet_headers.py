#!/usr/bin/env python3
"""
repair_sheet_headers.py

One-off repair of the Queue and Responses tabs, which hold rows written
in two different layouts under one header row that matches neither.

WHAT WAS FOUND (2026-09-27)
---------------------------
Every row the harness writes is laid out by *position*; every read is by
the *name* in row 1. Two generations of the harness have written these
tabs:

  Queue, old rows (ids 1-72, to about 2026-09-13):
      id, gmail_msg_id, thread_id, sender, subject, body, received_at
      — a counter first, no sender_addr, no status column at all.
  Queue, current rows:
      gmail_msg_id, thread_id, sender, sender_addr, subject, body,
      received_at, status, note, attachments           (QUEUE_HEADERS)
  Responses, old rows:
      id, queue_row_id, response_text, created_at
      — linked to the Queue by that old counter, not by Gmail id.
  Responses, current rows:
      gmail_msg_id, sender_addr, response_text, created_at   (RESPONSES_HEADERS)

Row 1 of Queue reads `thread_id, "", "", "", gmail_msg_id, ...` and row 1
of Responses is the old header with the new names appended. So every read
by name has been landing in the wrong column for one generation or both:
status lookups, memory, history search and known correspondents on the
Queue side, and — on the Responses side — every reply's `gmail_msg_id`
and `sender_addr` read as blank, which has quietly switched off the
per-sender daily reply limit.

WHAT THIS DOES
--------------
It rewrites the old rows into the current layout, so each tab holds one
layout under the right header:

  * An old Queue row keeps its Gmail id, thread, sender, subject, body and
    time. `sender_addr` is parsed from the sender. `status` is `replied`
    if an old Responses row answers it, and left blank otherwise — the
    old harness recorded nothing else, and a guess would be worse than a
    blank (blank means "no label, not counted" everywhere downstream).
    `note` records the old row id. The old time had no timezone; the old
    harness wrote UTC, so +00:00 is added.
  * An old Responses row gets the Gmail id and sender of the Queue row its
    `queue_row_id` named. One whose Queue row can't be found keeps its
    text and time with a blank id.
  * Row 1 of each tab becomes exactly the declared header row.
  * Stray copies of the column names on the ends of rows are cleared.

Every row must be recognisable as one layout or the other. If any isn't,
nothing is changed and those rows are listed. Current-layout rows are
never edited except to clear stray names after their data.

Dry run is the default. With --apply it first writes the raw contents of
both tabs to a JSON file in your home directory (outside the harness, so
it's in neither git nor the Drive backup), then makes the changes while
holding the harness lock. Google Sheets' version history is a second way
back.

    cd ~/harness
    /home/indi/venv/bin/python repair_sheet_headers.py           # report only
    /home/indi/venv/bin/python repair_sheet_headers.py --apply   # repair
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from email.utils import parseaddr
from pathlib import Path

import sheets_queue

_ID = re.compile(r"^[0-9a-f]{12,24}$", re.IGNORECASE)
_INT = re.compile(r"^\d+$")
_STATUSES = set(sheets_queue.STATUS_LABELS) | {""}

Q_HEADERS = list(sheets_queue.QUEUE_HEADERS)
R_HEADERS = list(sheets_queue.RESPONSES_HEADERS)
O_HEADERS = list(sheets_queue.OUTBOUND_HEADERS)


def _cell(row: list, i: int) -> str:
    return str(row[i]).strip() if i < len(row) else ""


def _raw(row: list, i: int) -> str:
    return str(row[i]) if i < len(row) else ""


def _is_date(value: str) -> bool:
    try:
        datetime.datetime.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


def _utc(value: str) -> str:
    try:
        when = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when.isoformat()


# -- recognising rows -------------------------------------------------------

def queue_kind(row: list) -> str | None:
    if (_ID.match(_cell(row, 0)) and _ID.match(_cell(row, 1))
            and _is_date(_cell(row, 6)) and _cell(row, 7) in _STATUSES):
        return "current"
    if (_INT.match(_cell(row, 0)) and _ID.match(_cell(row, 1))
            and _ID.match(_cell(row, 2)) and _is_date(_cell(row, 6))):
        return "old"
    return None


def responses_kind(row: list) -> str | None:
    if _ID.match(_cell(row, 0)) and "@" in _cell(row, 1) and _is_date(_cell(row, 3)):
        return "current"
    if _INT.match(_cell(row, 0)) and _INT.match(_cell(row, 1)) and _is_date(_cell(row, 3)):
        return "old"
    return None


def outbound_kind(row: list) -> str | None:
    return "current" if "@" in _cell(row, 0) and _is_date(_cell(row, 4)) else None


def stray_header_start(row: list, headers: list[str], first: int) -> int | None:
    """Where a tacked-on copy of the column names begins, searching from
    column index `first` on; None if there isn't one. Most of the names
    must appear in order — one cell that happens to say "status" is data."""
    names = [str(c).strip() for c in row]
    for start in range(first, len(names)):
        run = names[start:start + len(headers)]
        if sum(1 for got, want in zip(run, headers) if got == want) >= max(2, (len(headers) + 1) // 2):
            return start
    return None


def _debris_start(row: list, first: int) -> int | None:
    """Stray names from either generation's header."""
    old_q = ["id", "gmail_msg_id", "thread_id", "sender", "subject", "body", "received_at"]
    starts = [s for s in (stray_header_start(row, Q_HEADERS, first),
                          stray_header_start(row, old_q, first),
                          stray_header_start(row, R_HEADERS, first)) if s is not None]
    return min(starts) if starts else None


def _data(values):
    return [(n, r) for n, r in enumerate(values[1:], start=2)
            if any(str(c).strip() for c in r)]


# -- the plan ---------------------------------------------------------------

def plan(queue: list[list], responses: list[list], outbound: list[list] | None = None) -> dict:
    """Everything a repair would change, computed from the raw values only."""
    q_rows, r_rows = _data(queue), _data(responses)
    unknown = [("Queue", n, r) for n, r in q_rows if queue_kind(r) is None]
    unknown += [("Responses", n, r) for n, r in r_rows if responses_kind(r) is None]
    if outbound:
        unknown += [("Outbound", n, r) for n, r in _data(outbound) if outbound_kind(r) is None]

    old_queue = {_cell(r, 0): r for _, r in q_rows if queue_kind(r) == "old"}
    answered_old = {_cell(r, 1) for _, r in r_rows if responses_kind(r) == "old"}

    edits = {"Queue": [], "Responses": []}          # (row, new values or None, clear-from index or None)
    counts = {"queue_old": 0, "queue_old_replied": 0, "queue_strays": 0,
              "responses_old": 0, "responses_unlinked": 0}

    for n, r in q_rows:
        kind = queue_kind(r)
        if kind == "old":
            counts["queue_old"] += 1
            replied = _cell(r, 0) in answered_old
            counts["queue_old_replied"] += replied
            new = [_cell(r, 1), _cell(r, 2), _raw(r, 3),
                   (parseaddr(_raw(r, 3))[1] or "").strip().lower(),
                   _raw(r, 4), _raw(r, 5), _utc(_cell(r, 6)),
                   sheets_queue.STATUS_REPLIED if replied else "",
                   f"migrated from old row id {_cell(r, 0)}", ""]
            edits["Queue"].append((n, new, len(Q_HEADERS)))
        elif kind == "current":
            start = _debris_start(r, len(Q_HEADERS) - 1)
            if start is not None:
                counts["queue_strays"] += 1
                edits["Queue"].append((n, None, start))

    for n, r in r_rows:
        kind = responses_kind(r)
        if kind == "old":
            counts["responses_old"] += 1
            q = old_queue.get(_cell(r, 1))
            if q is None:
                counts["responses_unlinked"] += 1
            new = [_cell(q, 1) if q else "",
                   (parseaddr(_raw(q, 3))[1] or "").strip().lower() if q else "",
                   _raw(r, 2), _utc(_cell(r, 3))]
            edits["Responses"].append((n, new, len(R_HEADERS)))
        elif kind == "current":
            start = _debris_start(r, len(R_HEADERS))
            if start is not None:
                edits["Responses"].append((n, None, start))

    headers_ok = {
        "Queue": bool(queue) and [str(c) for c in queue[0]] == Q_HEADERS,
        "Responses": bool(responses) and [str(c) for c in responses[0]] == R_HEADERS,
        "Outbound": not outbound or [str(c) for c in outbound[0]] == O_HEADERS,
    }
    return {"unknown": unknown, "edits": edits, "counts": counts,
            "headers_ok": headers_ok, "rows": {"Queue": len(q_rows), "Responses": len(r_rows)}}


def describe(p: dict, out=print) -> None:
    c = p["counts"]
    out(f"Queue: {p['rows']['Queue']} data row(s); header {'correct' if p['headers_ok']['Queue'] else 'WRONG'}.")
    out(f"  {c['queue_old']} old-layout row(s) to convert "
        f"({c['queue_old_replied']} answered → 'replied', the rest left without a status).")
    out(f"  {c['queue_strays']} current row(s) with stray column names to clear.")
    out(f"Responses: {p['rows']['Responses']} data row(s); header "
        f"{'correct' if p['headers_ok']['Responses'] else 'WRONG'}.")
    out(f"  {c['responses_old']} old-layout row(s) to convert"
        + (f" ({c['responses_unlinked']} whose Queue row can't be found keep a blank id)."
           if c["responses_unlinked"] else "."))
    if not p["headers_ok"]["Outbound"]:
        out("Outbound: header WRONG (not repaired by this script).")
    if p["unknown"]:
        out(f"\nNOT SAFE TO REPAIR: {len(p['unknown'])} row(s) match neither layout:")
        for tab, n, r in p["unknown"][:10]:
            out(f"  {tab} row {n}: {[str(x)[:30] for x in r[:9]]}")
        if len(p["unknown"]) > 10:
            out(f"  ... and {len(p['unknown']) - 10} more")
        out("Nothing will be changed. Paste this output for help.")


def _col(i: int) -> str:
    return sheets_queue._col_letter(i)


def requests_for(tab: str, headers: list[str], edits, header_ok: bool):
    """(ranges to clear, value ranges to write) for one tab."""
    clears, writes = [], []
    if not header_ok:
        clears.append(f"{tab}!A1:Z1")
        writes.append({"range": f"{tab}!A1", "values": [list(headers)]})
    for n, new, clear_from in edits:
        if new is not None:
            writes.append({"range": f"{tab}!A{n}:{_col(len(new) - 1)}{n}", "values": [new]})
        if clear_from is not None:
            clears.append(f"{tab}!{_col(clear_from)}{n}:Z{n}")
    return clears, writes


def run(sheets, ids: dict[str, str], apply: bool, out=print,
        backup_dir: Path | None = None) -> int:
    def read(sid, tab):
        return (sheets.spreadsheets().values()
                .get(spreadsheetId=sid, range=f"{tab}!A1:Z").execute().get("values", []))

    qid, rid = ids.get("queue_id"), ids.get("responses_id")
    if not qid or not rid:
        out("Could not find both the Queue and Responses spreadsheets.")
        return 1
    queue = read(qid, sheets_queue.QUEUE_TAB)
    responses = read(rid, sheets_queue.RESPONSES_TAB)
    outbound = read(rid, sheets_queue.OUTBOUND_TAB)
    p = plan(queue, responses, outbound)
    describe(p, out)
    if p["unknown"]:
        return 1

    jobs = []
    for sid, tab, headers in ((qid, sheets_queue.QUEUE_TAB, Q_HEADERS),
                              (rid, sheets_queue.RESPONSES_TAB, R_HEADERS)):
        name = "Queue" if tab == sheets_queue.QUEUE_TAB else "Responses"
        clears, writes = requests_for(tab, headers, p["edits"][name], p["headers_ok"][name])
        if clears or writes:
            jobs.append((sid, tab, clears, writes))
    if not jobs:
        out("\nNothing to repair.")
        return 0
    if not apply:
        out("\nDry run: nothing was changed. Re-run with --apply to repair.")
        return 0

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = (backup_dir or Path.home()) / f"indi-sheet-backup-{stamp}.json"
    backup.write_text(json.dumps({"Queue": queue, "Responses": responses}), encoding="utf-8")
    try:
        os.chmod(backup, 0o600)
    except OSError:
        pass
    out(f"\nRaw contents saved to {backup}")
    for sid, tab, clears, writes in jobs:
        if clears:
            sheets.spreadsheets().values().batchClear(
                spreadsheetId=sid, body={"ranges": clears}).execute()
        if writes:
            sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=sid,
                body={"valueInputOption": "RAW", "data": writes}).execute()
        out(f"Repaired {tab!r}: {len(writes)} range(s) written, {len(clears)} cleared.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Repair the Queue and Responses tabs.")
    p.add_argument("--apply", action="store_true", help="make the repair (default: report)")
    args = p.parse_args(argv)

    from googleapiclient.discovery import build

    import check_and_reply
    from config import Config

    cfg = Config.from_env(non_interactive=True)
    lock = check_and_reply.acquire_lock(cfg.lock_path)
    try:
        creds = check_and_reply.get_credentials(cfg)
        sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        store = sheets_queue.SheetStore(sheets, drive, cfg)
        ids = {"queue_id": store._find_spreadsheet(sheets_queue.QUEUE_TITLE),
               "responses_id": store._find_spreadsheet(sheets_queue.RESPONSES_TITLE)}
        return run(sheets, ids, args.apply)
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
