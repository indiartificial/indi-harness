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
"""

from __future__ import annotations

import argparse
import logging
import sys

import gmail_io
import sheets_queue

log = logging.getLogger("indi.backfill")


def _is_not_found(exc: Exception) -> bool:
    # googleapiclient's HttpError carries the response on `.resp`; checked
    # by attribute so this module doesn't need the client stack to import.
    return getattr(getattr(exc, "resp", None), "status", None) == 404


def backfill(gmail, rows: list[dict], apply: bool = False, out=print) -> dict:
    """Label each row's message to match its status. Returns counts.

    Rows are taken in sheet order and only the first row for a given
    message id is used, which is the one `find_queue_row` — and so every
    status write — acts on.
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


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="One-off: label existing Queue messages with their Indi/* status.")
    p.add_argument("--apply", action="store_true",
                   help="actually change labels (default is a dry run)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    # Imported here so `backfill` can be tested without the Google client
    # stack, as drive_backup.py does. If this raises ModuleNotFoundError,
    # the script is being run outside the venv — see the usage note above.
    from googleapiclient.discovery import build

    import check_and_reply
    from config import Config

    cfg = Config.from_env(non_interactive=True)
    check_and_reply.setup_logging(cfg.log_path, verbose=False)
    if not cfg.gmail_status_labels:
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
        counts = backfill(gmail, store.queue_rows(), apply=args.apply)
        return 1 if counts["errors"] else 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
