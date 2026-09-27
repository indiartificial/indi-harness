#!/usr/bin/env python3
"""
move_records_to_drive.py

One-off, just before INDI_DRIVE_RECORDS=true (MIGRATE-GMAIL.md, steps 6-7).
Copies what the spreadsheets hold that isn't mail into CSV files in Drive,
where drive_tables.py will read and write them from then on:

    Journal, Costs, Reminders,
    Newsletter Subscribers, Newsletter Issues   copied verbatim, row for row
    Decisions                                   built from the Queue tab's
                                                notes and the Outbound tab's
                                                rationales

Verbatim matters for Reminders and the newsletter tabs: their code finds a
row by its position, so the copy keeps every row where it was, blank rows
included.

It checks each tab's header row against the columns the code declares
before copying. A tab whose header doesn't match is not copied and is
reported — copying it would carry the same misreading into Drive that
repair_sheet_headers.py just fixed in the sheet.

A Drive file that already holds data is never overwritten, so the script
can be re-run safely; it reports "already in Drive" instead.

Stop the timers first, so nothing is written to the sheet between the
copy and the switch:

    cd ~/harness
    /home/indi/venv/bin/python move_records_to_drive.py           # report only
    /home/indi/venv/bin/python move_records_to_drive.py --apply   # copy

then set INDI_DRIVE_RECORDS=true and start the timers. The spreadsheets
themselves are left exactly as they are.
"""

from __future__ import annotations

import argparse
import sys

import drive_tables
import gmail_store
import newsletter
import outbound
import sheets_queue

COPY = [
    (sheets_queue.JOURNAL_TAB, sheets_queue.JOURNAL_HEADERS),
    (sheets_queue.COSTS_TAB, sheets_queue.COSTS_HEADERS),
    (outbound.REMINDERS_TAB, outbound.REMINDERS_HEADERS),
    (newsletter.SUBSCRIBERS_TAB, newsletter.SUBSCRIBERS_HEADERS),
    (newsletter.ISSUES_TAB, newsletter.ISSUES_HEADERS),
]


def _read(sheets, sid, tab):
    return (sheets.spreadsheets().values()
            .get(spreadsheetId=sid, range=f"{tab}!A1:Z").execute().get("values", []))


def _tabs(sheets, sid) -> set[str]:
    meta = sheets.spreadsheets().get(spreadsheetId=sid,
                                     fields="sheets.properties.title").execute()
    return {s["properties"]["title"] for s in meta.get("sheets", [])}


def decisions_from(queue_rows: list[dict], outbound_rows: list[dict]) -> list[list[str]]:
    rows = [list(gmail_store.DECISIONS_HEADERS)]
    for r in sorted(queue_rows, key=lambda r: r.get("received_at", "")):
        if (r.get("note") or "").strip():
            rows.append([r.get("received_at", ""), r.get("gmail_msg_id", ""), "status",
                         r.get("status", ""), r.get("sender_addr", ""), r["note"][:500]])
    for r in sorted(outbound_rows, key=lambda r: r.get("created_at", "")):
        if (r.get("rationale") or "").strip():
            rows.append([r.get("created_at", ""), "", "outbound", "",
                         r.get("recipient", ""), r["rationale"][:500]])
    return rows


def plan(sheets, responses_id, queue_rows, outbound_rows, target, out=print) -> list:
    """[(tab, values)] to copy, having reported on every tab."""
    todo = []
    present = _tabs(sheets, responses_id)
    for tab, headers in COPY:
        if tab not in present:
            out(f"{tab}: not in the spreadsheet — nothing to copy; it starts empty in Drive.")
            continue
        values = _read(sheets, responses_id, tab)
        data = [r for r in values[1:] if any(str(c).strip() for c in r)]
        if not values or [str(c) for c in values[0]] != list(headers):
            out(f"{tab}: NOT COPIED — header row is {values[0] if values else '(empty)'!r}, "
                f"expected {list(headers)!r}. Paste this for help.")
            continue
        if target._read(f"{tab}!A2:Z"):
            out(f"{tab}: already in Drive, left alone.")
            continue
        out(f"{tab}: {len(data)} row(s) to copy.")
        todo.append((tab, values))
    decisions = decisions_from(queue_rows, outbound_rows)
    if target._read(f"{gmail_store.DECISIONS_TAB}!A2:Z"):
        out(f"{gmail_store.DECISIONS_TAB}: already in Drive, left alone.")
    else:
        out(f"{gmail_store.DECISIONS_TAB}: {len(decisions) - 1} note(s) and rationale(s) to copy.")
        todo.append((gmail_store.DECISIONS_TAB, decisions))
    return todo


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Copy the non-mail tabs into Drive.")
    p.add_argument("--apply", action="store_true", help="copy (default: report only)")
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
        store.queue_id = store._find_spreadsheet(sheets_queue.QUEUE_TITLE)
        store.responses_id = store._find_spreadsheet(sheets_queue.RESPONSES_TITLE)
        if not store.responses_id:
            print("No 'Indi Responses' spreadsheet found.")
            return 1
        queue_rows = store.queue_rows() if store.queue_id else []
        outbound_rows = store.outbound_rows()
        if store.schema_report():
            print("The Queue or Outbound header rows are wrong; run "
                  "repair_sheet_headers.py first.")
            return 1

        target = drive_tables.DriveSheets(drive_tables.DriveIO(drive))
        todo = plan(sheets, store.responses_id, queue_rows, outbound_rows, target)
        if not args.apply:
            print("\nDry run: nothing was copied. Re-run with --apply.")
            return 0
        for tab, values in todo:
            target.update(range=f"{tab}!A1", body={"values": values}).execute()
            print(f"Copied {tab} ({len(values) - 1} row(s)) to "
                  f"{drive_tables.file_name(tab)!r}.")
        print("\nDone. Now set INDI_DRIVE_RECORDS=true.")
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
