#!/usr/bin/env python3
"""
backfill_labels.py

One-off: give every message already in the Queue sheet the Indi/* label
matching its status (MIGRATE-GMAIL.md, step 2). New status writes label
themselves; this catches up the history written before they did.

Run it deliberately, from a terminal, with the venv interpreter — the
Google client stack is not installed system-wide, so a bare `python3`
dies at `import googleapiclient` before anything happens:

    cd ~/harness
    /home/indi/venv/bin/python backfill_labels.py            # dry run: report, change nothing
    /home/indi/venv/bin/python backfill_labels.py --apply    # actually label
    /home/indi/venv/bin/python backfill_labels.py --dump-rows 5   # read-only: show the sheet's shape
    /home/indi/venv/bin/python backfill_labels.py --dump-tail 5   # read-only: show the last rows

Nothing in the poll or the heartbeat imports this. It is a migration
step, not a routine, and it should happen once, when someone is watching.

What it takes care over:

  * **Dry run is the default.** Without `--apply` it makes only reads —
    the label list and each message's current labels — and reports what
    it would do. It does not even create a missing label.

  * **Safe to re-run.** Each message's labels are read first. One that
    already carries exactly the right Indi/* label and none of the other
    four is left alone, so a second pass after an interrupted first one
    only touches what the first didn't reach.

  * **Messages that are gone.** A row whose message has since been
    deleted from the mailbox is counted and skipped, not an error. Any
    other failure on one message is logged and the pass moves on.

  * **The sheet is read, never written.** The spreadsheet is looked up,
    not `ensure_spreadsheets()`-ed, so a run can't create or re-header
    anything. It holds the harness lock throughout, so a scheduled run
    can't change a status between this reading the row and labelling
    the message — which would leave the label saying something the
    sheet doesn't.

  * **It says what it read.** A pass that finds no messages, or finds
    rows whose `gmail_msg_id` doesn't look like a Gmail message id, is
    almost always reading a different spreadsheet or a Queue tab whose
    columns aren't where `QUEUE_HEADERS` says they are — not a mailbox
    with nothing in it. So the run prints the spreadsheet id, the header
    row as the sheet spells it, the row count, and a warning if more
    than one Drive file carries the Queue title. Those are all reads.

  * **It can show the shape when the shape is wrong.** `--dump-rows N`
    prints the first N rows cell by cell, each cell truncated, with its
    column letter — the comma-joined header line is unreadable once a
    cell holds an entire email body. `--dump-tail N` does the same for
    the last N rows, with their real sheet row numbers, which is how a
    sheet that is accumulating rows at the *top* rather than the bottom
    shows itself: compare `received_at` at the two ends. Alongside
    both, a scan reports every row where a declared header name turns
    up as a *value*, and in which column, which is how a shifted or
    displaced header row shows itself. All of these are reads and none
    depends on `--apply`.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys

import gmail_io
import sheets_queue

log = logging.getLogger("indi.backfill")

# Gmail message ids are hex strings, comfortably longer than this. The test
# is deliberately loose: it exists to catch a row whose first column holds
# something like "gmail_msg_id" or an email address, not to validate ids.
_LOOKS_LIKE_MSG_ID = re.compile(r"^[0-9a-f]{8,}$", re.IGNORECASE)

# How much of a cell to show in a dump. Bodies run to tens of thousands of
# characters; the first few are enough to recognise what a cell is.
_CELL_PREVIEW = 48


def _stderr(message: str) -> None:
    print(message, file=sys.stderr)


def _is_not_found(exc: Exception) -> bool:
    # googleapiclient's HttpError carries the response on `.resp`; checked
    # by attribute so this module doesn't need the client stack to import.
    return getattr(getattr(exc, "resp", None), "status", None) == 404


def _preview(value: str, width: int = _CELL_PREVIEW) -> str:
    """One cell, on one line, short enough to read in a terminal."""
    text = str(value)
    collapsed = " ".join(text.split())
    if len(collapsed) > width:
        collapsed = collapsed[: width - 1] + "…"
    return repr(collapsed)


def _dump_block(rows: list[list[str]], first_number: int, out=print) -> None:
    """Print `rows` cell by cell, numbering them from `first_number`."""
    for offset, row in enumerate(rows):
        n = first_number + offset
        label = "row 1 (read as the header row)" if n == 1 else f"row {n}"
        out(f"{label}: {len(row)} cell(s)")
        for i, cell in enumerate(row):
            out(f"    {sheets_queue._col_letter(i)}: {_preview(cell)}")


def dump_rows(values: list[list[str]], count: int, out=print) -> None:
    """Print the first `count` rows cell by cell, with column letters.

    `values` is the raw A1:Z block as the API returns it — rows of
    strings, short rows not padded. Printing it this way is the only
    reliable way to see where a row's first cell actually is, because
    every other report in this script reads by column *name* and so
    inherits whatever the header row claims.
    """
    if not values:
        out("(no rows to dump)")
        return
    shown = values[: max(1, count)]
    out(f"--- first {len(shown)} row(s), cell by cell ---")
    _dump_block(shown, 1, out=out)
    out("--- end of dump ---")


def dump_tail(values: list[list[str]], count: int, out=print) -> None:
    """Print the last `count` rows, numbered as the sheet numbers them.

    The head of the sheet cannot tell you which way rows are being
    added; the two ends together can. Read `received_at` in the first
    rows and in these: if the oldest message is down here, the sheet is
    oldest-first and appends are landing where they should, and if the
    oldest is at the top, something is inserting new rows above the
    old ones and every row number the status writer holds is a moving
    target.
    """
    if not values:
        out("(no rows to dump)")
        return
    n = max(1, count)
    tail = values[-n:]
    first_number = len(values) - len(tail) + 1
    out(f"--- last {len(tail)} row(s) of {len(values)}, cell by cell ---")
    _dump_block(tail, first_number, out=out)
    out("--- end of tail ---")


def scan_for_header_text(values: list[list[str]], headers: list[str],
                         out=print, limit: int = 20) -> int:
    """Report rows that hold declared header *names* as cell values.

    A healthy sheet has exactly one such row — row 1. More than one, or
    one that doesn't start at column A, means the header row has been
    displaced or rows have been shifted sideways, and says by how much.
    Returns the number of rows found.
    """
    wanted = set(headers)
    found = 0
    out("--- rows containing header names as values ---")
    for n, row in enumerate(values, start=1):
        hits = [(i, c) for i, c in enumerate(row) if c in wanted]
        if len(hits) < 2:
            continue
        found += 1
        if found <= limit:
            first_col, first_name = hits[0]
            out(f"row {n}: {len(hits)} header name(s), starting at column "
                f"{sheets_queue._col_letter(first_col)} with {first_name!r} "
                f"(row has {len(row)} cell(s))")
    if found > limit:
        out(f"... and {found - limit} more such row(s).")
    if not found:
        out("(none — not even row 1, which is itself a problem)")
    out("--- end of scan ---")
    return found


def read_raw_rows(store, tab: str = sheets_queue.QUEUE_TAB) -> list[list[str]]:
    """The tab exactly as the API hands it over: no header mapping, no
    padding. Separate from `SheetStore._load` on purpose — the whole
    point is to bypass the name-based reading that is in doubt."""
    return (
        store.sheets.spreadsheets()
        .values()
        .get(spreadsheetId=store.queue_id, range=f"{tab}!A1:Z")
        .execute()
        .get("values", [])
    )


def backfill(gmail, rows: list[dict], apply: bool = False, out=print,
             warn=_stderr) -> dict:
    """Label each row's message to match its status. Returns counts.

    Rows are taken in sheet order and only the first row for a given
    message id is used, which is the one `find_queue_row` — and so every
    status write — acts on.

    `warn` receives shape complaints about the rows themselves; they go
    to stderr rather than through `out` so they can't be mistaken for
    per-message results.
    """
    labeler = gmail_io.StatusLabeler(gmail)
    all_names = list(sheets_queue.STATUS_LABELS.values())
    counts = {"labelled": 0, "would_label": 0, "already": 0, "missing": 0,
              "no_status": 0, "errors": 0}
    seen: set[str] = set()
    todo = []
    for row in rows:
        msg_id = (row.get("gmail_msg_id") or "").strip()
        if not msg_id or msg_id in seen:
            continue
        seen.add(msg_id)
        todo.append((msg_id, (row.get("status") or "").strip()))

    verb = "Labelling" if apply else "Dry run — checking"
    out(f"{verb} {len(todo)} message(s) from the Queue sheet.")

    odd = [m for m, _ in todo if not _LOOKS_LIKE_MSG_ID.match(m)]
    if todo and len(odd) == len(todo):
        warn("WARNING: none of these look like Gmail message ids "
             f"(first: {todo[0][0]!r}). The Queue tab's columns are probably "
             "not where QUEUE_HEADERS expects them, or this is not the Queue "
             "spreadsheet the harness writes to. Nothing here will label.")
    elif odd:
        warn(f"WARNING: {len(odd)} of {len(todo)} row(s) have a gmail_msg_id "
             f"that doesn't look like one (e.g. {odd[0]!r}).")

    for n, (msg_id, status) in enumerate(todo, start=1):
        prefix = f"[{n}/{len(todo)}] {msg_id}"
        name = sheets_queue.STATUS_LABELS.get(status)
        if name is None:
            counts["no_status"] += 1
            out(f"{prefix}: status {status!r} has no label, skipped")
            continue
        try:
            current = set(
                gmail.users().messages()
                .get(userId="me", id=msg_id, format="minimal", fields="labelIds")
                .execute().get("labelIds", [])
            )
            want = labeler.existing_id(name)
            others = {i for i in (labeler.existing_id(o) for o in all_names
                                  if o != name) if i}
            if want and want in current and not (current & others):
                counts["already"] += 1
                out(f"{prefix}: already {name}")
                continue
            if not apply:
                counts["would_label"] += 1
                out(f"{prefix}: would set {name}")
                continue
            labeler.apply(msg_id, name, all_names)
            counts["labelled"] += 1
            out(f"{prefix}: set {name}")
        except Exception as exc:
            if _is_not_found(exc):
                counts["missing"] += 1
                out(f"{prefix}: no longer in the mailbox, skipped")
                continue
            counts["errors"] += 1
            log.warning("Could not label %s", msg_id, exc_info=True)
            out(f"{prefix}: error ({exc.__class__.__name__}: {exc}), skipped")

    out("Done: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in counts.items()))
    if not apply and counts["would_label"]:
        out("Nothing was changed. Re-run with --apply to label them.")
    return counts


def _report_source(drive, store) -> None:
    """Say which spreadsheet is about to be read, and how it is shaped.

    Read-only. `_find_spreadsheet` picks the first of any duplicates and
    only logs about it; a terminal run deserves to see that on screen,
    because a forked history is the one failure here that looks like
    success.
    """
    title = sheets_queue.QUEUE_TITLE
    try:
        files = (
            drive.files()
            .list(
                q=(f"name = '{sheets_queue._escape_drive_query(title)}' and "
                   "mimeType = 'application/vnd.google-apps.spreadsheet' and "
                   "trashed = false"),
                spaces="drive",
                fields="files(id, name, modifiedTime)",
            )
            .execute()
            .get("files", [])
        )
    except Exception:
        log.warning("Could not list spreadsheets named %r", title, exc_info=True)
        files = []
    if len(files) > 1:
        _stderr(f"WARNING: {len(files)} spreadsheets are named {title!r}:")
        for f in files:
            _stderr(f"  {f.get('id')}  modified {f.get('modifiedTime', '?')}")
        _stderr("  Using the first. Duplicates fork the history — remove them "
                "before trusting this run.")

    try:
        headers = store.tab_headers(store.queue_id, sheets_queue.QUEUE_TAB)
        rows = store.queue_rows()
    except Exception:
        log.warning("Could not read the %s tab", sheets_queue.QUEUE_TAB,
                    exc_info=True)
        return
    print(f"Queue spreadsheet {store.queue_id}, tab {sheets_queue.QUEUE_TAB}: "
          f"{len(rows)} data row(s).")
    # Deliberate wording: this is the column naming the module will *use*,
    # which is the sheet's own row 1 only when row 1 is a header row. If a
    # SHEET SCHEMA error appeared above, these are the declared names being
    # applied positionally and row 1 holds something else entirely — the
    # dump below is the one that shows what is actually in the cells.
    print("Columns as this run will read them (the declared names if a "
          "SHEET SCHEMA error appeared above), one per line:")
    for i, cell in enumerate(headers):
        print(f"  {sheets_queue._col_letter(i)}: {_preview(cell)}")
    if not headers:
        print("  (empty)")
    if headers and headers[: len(sheets_queue.QUEUE_HEADERS)] != sheets_queue.QUEUE_HEADERS:
        _stderr("WARNING: that header row is not the one QUEUE_HEADERS "
                "declares. Every column is read by name, so a row's values "
                "will be attributed to the wrong fields.")


def _report_shape(store, dump_count: int, tail_count: int = 0) -> None:
    """The read-only diagnostics: raw dump, tail dump, header-text scan."""
    try:
        values = read_raw_rows(store)
    except Exception:
        log.warning("Could not read raw rows from the %s tab",
                    sheets_queue.QUEUE_TAB, exc_info=True)
        _stderr("Could not read the raw rows; see the log.")
        return
    if dump_count > 0:
        dump_rows(values, dump_count)
    if tail_count > 0:
        dump_tail(values, tail_count)
    scan_for_header_text(values, sheets_queue.QUEUE_HEADERS)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="One-off: label existing Queue messages with their Indi/* status.")
    p.add_argument("--apply", action="store_true",
                   help="actually change labels (default is a dry run)")
    p.add_argument("--dump-rows", type=int, default=0, metavar="N",
                   help="read-only: print the first N rows cell by cell, then "
                        "scan every row for header names appearing as values, "
                        "and stop without touching labels")
    p.add_argument("--dump-tail", type=int, default=0, metavar="N",
                   help="read-only: print the last N rows cell by cell, with "
                        "their sheet row numbers — compare received_at with "
                        "the head to see which way rows are being added")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    diagnostic = bool(args.dump_rows or args.dump_tail)
    # Imported here so `backfill` can be tested without the Google client
    # stack, as drive_backup.py does. If this raises ModuleNotFoundError,
    # the script is being run outside the venv — see the usage note above.
    from googleapiclient.discovery import build

    import check_and_reply
    from config import Config

    cfg = Config.from_env(non_interactive=True)
    check_and_reply.setup_logging(cfg.log_path, verbose=False)
    if not cfg.gmail_status_labels and not diagnostic:
        print("INDI_GMAIL_STATUS_LABELS is off; not backfilling labels "
              "that nothing would keep up to date.")
        return 1

    lock = check_and_reply.acquire_lock(cfg.lock_path)
    try:
        creds = check_and_reply.get_credentials(cfg)
        gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)

        store = sheets_queue.SheetStore(sheets, drive, cfg)
        store.queue_id = store._find_spreadsheet(sheets_queue.QUEUE_TITLE)
        if not store.queue_id:
            print(f"No spreadsheet named {sheets_queue.QUEUE_TITLE!r}; nothing to backfill.")
            return 1
        _report_source(drive, store)
        if diagnostic:
            # Diagnostic mode: read the shape, change nothing, don't
            # pretend a labelling pass over a misread sheet means anything.
            _report_shape(store, args.dump_rows, args.dump_tail)
            print("Diagnostic run — no labels were touched.")
            return 0
        counts = backfill(gmail, store.queue_rows(), apply=args.apply)
        return 1 if counts["errors"] else 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
