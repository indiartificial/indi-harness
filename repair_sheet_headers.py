#!/usr/bin/env python3
"""
repair_sheet_headers.py

One-off repair of the Queue, Responses and Outbound tabs, whose header
rows no longer match their data.

WHAT WENT WRONG
---------------
Every row the harness writes is laid out by *position*, in the order
sheets_queue.py declares (QUEUE_HEADERS and friends). Every read is by
*name*, looked up in row 1. On the Queue tab, row 1 stopped being that
declared header row — it now reads `thread_id, "", "", "", gmail_msg_id,
thread_id, sender, ...` — so every read by name lands in the wrong column:
`gmail_msg_id` returns the subject, `status` returns the note, and so on.
That has been silently breaking every sheet reader (status lookups,
memory, history search, known correspondents), and it is why
backfill_labels.py labelled nothing.

The data rows themselves are fine: column A is the message id, B the
thread, C the sender ... H the status, I the note, exactly as declared.
About a hundred older rows additionally carry a copy of the column names
tacked on after their data (from column J) — the remains of the old
`_ensure_headers` behaviour that sheets_queue.py's docstring describes.

WHAT THIS DOES
--------------
For each of the three tabs:

  1. Checks the data rows *positionally*: ids where ids go, a real status
     in the status column, dates in the date column. A tab is repaired
     only if at least REQUIRED_SHARE of its data rows pass. Otherwise it
     is reported and left alone, because rewriting the header over data
     that isn't where we think would make things worse, not better.
  2. Rewrites row 1 as exactly the declared header row.
  3. Clears the stray copies of the column names from the ends of rows
     that have them. Nothing before them is touched.

It never reorders, deletes or edits a data value.

Dry run is the default. With --apply it first writes the raw contents of
every tab it will change to a JSON file in your home directory (outside
the harness, so it's in neither git nor the Drive backup), then makes the
changes, holding the harness lock so no scheduled run appends meanwhile.
Google Sheets' own version history (File > Version history) is a second
way back.

    cd ~/harness
    /home/indi/venv/bin/python repair_sheet_headers.py           # report only
    /home/indi/venv/bin/python repair_sheet_headers.py --apply   # repair

Afterwards, backfill_labels.py should find real message ids.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

import sheets_queue

REQUIRED_SHARE = 0.9

_ID = re.compile(r"^[0-9a-f]{12,24}$", re.IGNORECASE)
_STATUSES = set(sheets_queue.STATUS_LABELS) | {""}


def _col(i: int) -> str:
    return sheets_queue._col_letter(i)


def _cell(row: list, i: int) -> str:
    return str(row[i]).strip() if i < len(row) else ""


def _is_date(value: str) -> bool:
    try:
        datetime.datetime.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


def _queue_ok(row: list) -> bool:
    return (bool(_ID.match(_cell(row, 0))) and bool(_ID.match(_cell(row, 1)))
            and _cell(row, 7) in _STATUSES and _is_date(_cell(row, 6)))


def _responses_ok(row: list) -> bool:
    return (bool(_ID.match(_cell(row, 0))) and "@" in _cell(row, 1)
            and _is_date(_cell(row, 3)))


def _outbound_ok(row: list) -> bool:
    return "@" in _cell(row, 0) and _is_date(_cell(row, 4))


# (spreadsheet attribute on the store, tab, declared columns, row check)
TABS = [
    ("queue_id", sheets_queue.QUEUE_TAB, sheets_queue.QUEUE_HEADERS, _queue_ok),
    ("responses_id", sheets_queue.RESPONSES_TAB, sheets_queue.RESPONSES_HEADERS, _responses_ok),
    ("responses_id", sheets_queue.OUTBOUND_TAB, sheets_queue.OUTBOUND_HEADERS, _outbound_ok),
]


def stray_header_start(row: list, headers: list[str]) -> int | None:
    """Where a tacked-on copy of the column names begins in this row, or
    None. Searched from the last declared column on — on the server the
    copies start in the `attachments` column, which those rows left empty —
    and only if most of the names appear there in order: a single cell
    that happens to say "status" is data, not debris."""
    names = [str(c).strip() for c in row]
    for start in range(max(1, len(headers) - 1), len(names)):
        run = names[start:start + len(headers)]
        matched = sum(1 for got, want in zip(run, headers) if got == want)
        if matched >= max(2, (len(headers) + 1) // 2):
            return start
    return None


def plan_tab(values: list[list], headers: list[str], row_ok) -> dict:
    """What a repair of this tab would change. Pure: reads `values` only."""
    header_row = [str(c) for c in values[0]] if values else []
    data = values[1:] if values else []
    nonblank = [(n, r) for n, r in enumerate(data, start=2) if any(str(c).strip() for c in r)]
    bad = [(n, r) for n, r in nonblank if not row_ok(r)]
    share = 1 - len(bad) / len(nonblank) if nonblank else 1.0
    strays = {}
    for n, r in nonblank:
        start = stray_header_start(r, headers)
        if start is not None:
            strays[n] = start
    return {
        "header_ok": header_row == list(headers),
        "header_row": header_row,
        "rows": len(nonblank),
        "bad_rows": bad,
        "share_ok": share,
        "safe": share >= REQUIRED_SHARE,
        "strays": strays,
    }


def describe(tab: str, plan: dict, out=print) -> None:
    out(f"\nTab {tab!r}: {plan['rows']} data row(s).")
    if plan["header_ok"]:
        out("  Header row: correct.")
    else:
        out(f"  Header row: WRONG — {plan['header_row'][:14]}")
    out(f"  Data rows laid out as declared: {plan['share_ok']:.0%} "
        f"({plan['rows'] - len(plan['bad_rows'])} of {plan['rows']}).")
    for n, r in plan["bad_rows"][:8]:
        out(f"    row {n} doesn't fit: {[str(c)[:30] for c in r[:9]]}")
    if len(plan["bad_rows"]) > 8:
        out(f"    ... and {len(plan['bad_rows']) - 8} more")
    if plan["strays"]:
        out(f"  Rows with a stray copy of the column names on the end: {len(plan['strays'])}")
    if not plan["safe"]:
        out(f"  NOT SAFE TO REPAIR: under {REQUIRED_SHARE:.0%} of rows fit the "
            "declared layout. Left alone — paste this output for help.")


def needs_repair(plan: dict) -> bool:
    return plan["safe"] and (not plan["header_ok"] or bool(plan["strays"]))


def apply_repair(sheets, spreadsheet_id: str, tab: str, headers: list[str],
                 plan: dict) -> None:
    """Row 1 := the declared headers; stray names cleared. Two calls."""
    ranges = [f"{tab}!A1:Z1"] + [f"{tab}!{_col(start)}{n}:Z{n}"
                                 for n, start in sorted(plan["strays"].items())]
    sheets.spreadsheets().values().batchClear(
        spreadsheetId=spreadsheet_id, body={"ranges": ranges}).execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id, range=f"{tab}!A1",
        valueInputOption="RAW", body={"values": [list(headers)]}).execute()


def read_tab(sheets, spreadsheet_id: str, tab: str) -> list[list]:
    return (sheets.spreadsheets().values()
            .get(spreadsheetId=spreadsheet_id, range=f"{tab}!A1:Z")
            .execute().get("values", []))


def run(sheets, ids: dict[str, str], apply: bool, out=print,
        backup_dir: Path | None = None) -> int:
    """Plan (and with `apply`, perform) the repair. Returns an exit code."""
    work, unsafe = [], False
    for attr, tab, headers, ok in TABS:
        sid = ids.get(attr)
        if not sid:
            out(f"\nTab {tab!r}: spreadsheet not found, skipped.")
            continue
        values = read_tab(sheets, sid, tab)
        plan = plan_tab(values, headers, ok)
        describe(tab, plan, out)
        unsafe |= not plan["safe"]
        if needs_repair(plan):
            work.append((sid, tab, headers, plan, values))

    if not work:
        out("\nNothing to repair.")
        return 1 if unsafe else 0
    if not apply:
        out(f"\nDry run: would repair {', '.join(repr(t) for _, t, _, _, _ in work)}. "
            "Nothing was changed; re-run with --apply.")
        return 1 if unsafe else 0

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = (backup_dir or Path.home()) / f"indi-sheet-backup-{stamp}.json"
    backup.write_text(json.dumps({t: v for _, t, _, _, v in work}), encoding="utf-8")
    try:
        os.chmod(backup, 0o600)
    except OSError:
        pass
    out(f"\nRaw contents saved to {backup}")
    for sid, tab, headers, plan, _ in work:
        apply_repair(sheets, sid, tab, headers, plan)
        out(f"Repaired {tab!r}: header row rewritten, "
            f"{len(plan['strays'])} stray name block(s) cleared.")
    return 1 if unsafe else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Repair the sheet header rows.")
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
