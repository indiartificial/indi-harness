#!/usr/bin/env python3
"""
cutover_prep.py

One-off, before step 5 of MIGRATE-GMAIL.md (INDI_GMAIL_READS=true). Does
the three things the Gmail readers need that `backfill_labels.py` doesn't:

  1. **Past replies get `Indi/Reply`.** Every Responses row names the
     message it answered; Indi's reply is the message in that thread,
     sent or drafted by this account, whose In-Reply-To is that message's
     Message-ID. The per-sender rate limit counts these.
  2. **Past heartbeat mail gets `Indi/Outbound`.** Every Outbound row is
     matched to sent (or drafted) mail to the same recipient, with the
     same subject, within a day of the recorded time. The outbound budgets
     and known_correspondents count these.
  3. **Every v1 memory file becomes v2.** A v1 header's `folded: N` means
     "the first N of this sender's replied messages *in the sheet's
     order*". Resolved now, against the sheet, it becomes the exact id set
     `folded_ids`, which means the same thing whichever store answers.
     Left alone, it would be resolved against Gmail's order after the flip
     — which can differ wherever a message is missing from one side — and
     memory would silently re-fold or skip messages. The previous file is
     kept as a timestamped backup, as consolidation does.

Run it the way backfill_labels.py is run, with the timers stopped:

    cd ~/harness
    /home/indi/venv/bin/python cutover_prep.py            # dry run: report only
    /home/indi/venv/bin/python cutover_prep.py --apply    # do it

Dry run is the default and makes only reads. Safe to re-run: a message
already carrying its label is left alone, and a v2 memory file is never
touched. The sheets are read, never written. It holds the harness lock.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import sys

import gmail_io
import memory_file
import sheets_queue

log = logging.getLogger("indi.cutover_prep")


def _headers(message: dict) -> dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "")
            for h in (message.get("payload") or {}).get("headers", [])}


def _when(value: str) -> datetime.datetime | None:
    try:
        when = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=datetime.timezone.utc)


def label_replies(gmail, queue_rows, response_rows, apply: bool, out=print) -> dict:
    labeler = gmail_io.StatusLabeler(gmail)
    counts = {"labelled": 0, "would_label": 0, "already": 0, "unmatched": 0,
              "missing": 0, "errors": 0}
    answered = sorted({r.get("gmail_msg_id", "").strip() for r in response_rows} - {""})
    threads = {r.get("gmail_msg_id"): r.get("thread_id", "") for r in queue_rows}
    out(f"Replies: {len(answered)} answered message(s) in the Responses tab.")
    for msg_id in answered:
        try:
            src = gmail.users().messages().get(
                userId="me", id=msg_id, format="metadata",
                metadataHeaders=["Message-ID"]).execute()
        except Exception:
            counts["missing"] += 1
            continue
        try:
            rfc = _headers(src).get("message-id", "").strip()
            thread_id = src.get("threadId") or threads.get(msg_id, "")
            thread = gmail.users().threads().get(
                userId="me", id=thread_id, format="metadata",
                metadataHeaders=["In-Reply-To"]).execute().get("messages", [])
            mine = [m for m in thread
                    if {"SENT", "DRAFT"} & set(m.get("labelIds") or [])
                    and rfc and _headers(m).get("in-reply-to", "").strip() == rfc]
            if not mine:
                counts["unmatched"] += 1
                out(f"  {msg_id}: no reply found in its thread")
                continue
            want = labeler.existing_id(sheets_queue.LABEL_REPLY)
            for m in mine:
                if want and want in (m.get("labelIds") or []):
                    counts["already"] += 1
                elif apply:
                    labeler.apply(m["id"], sheets_queue.LABEL_REPLY, ())
                    counts["labelled"] += 1
                else:
                    counts["would_label"] += 1
        except Exception as exc:
            counts["errors"] += 1
            out(f"  {msg_id}: error ({exc.__class__.__name__}: {exc})")
    out("  " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in counts.items()))
    return counts


def label_outbound(gmail, outbound_rows, apply: bool, out=print) -> dict:
    labeler = gmail_io.StatusLabeler(gmail)
    counts = {"labelled": 0, "would_label": 0, "already": 0, "unmatched": 0, "errors": 0}
    out(f"Outbound: {len(outbound_rows)} row(s) in the Outbound tab.")
    for row in outbound_rows:
        to = (row.get("recipient") or "").strip().lower()
        when = _when(row.get("created_at", ""))
        subject = (row.get("subject") or "").strip()
        if not to or when is None:
            counts["unmatched"] += 1
            continue
        try:
            lo, hi = int(when.timestamp()) - 86400, int(when.timestamp()) + 86400
            found = gmail.users().messages().list(
                userId="me", q=f"to:{to} after:{lo} before:{hi}", maxResults=25,
            ).execute().get("messages", []) or []
            match = None
            for ref in found:
                m = gmail.users().messages().get(
                    userId="me", id=ref["id"], format="metadata",
                    metadataHeaders=["Subject", "To"]).execute()
                if not {"SENT", "DRAFT"} & set(m.get("labelIds") or []):
                    continue
                if _headers(m).get("subject", "").strip() == subject:
                    match = m
                    break
            if match is None:
                counts["unmatched"] += 1
                out(f"  {to} {row.get('created_at', '')}: no matching sent mail")
                continue
            want = labeler.existing_id(sheets_queue.LABEL_OUTBOUND)
            if want and want in (match.get("labelIds") or []):
                counts["already"] += 1
            elif apply:
                labeler.apply(match["id"], sheets_queue.LABEL_OUTBOUND, ())
                counts["labelled"] += 1
            else:
                counts["would_label"] += 1
        except Exception as exc:
            counts["errors"] += 1
            out(f"  {to}: error ({exc.__class__.__name__}: {exc})")
    out("  " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in counts.items()))
    return counts


def convert_memory(drive, store, apply: bool, keep: int, out=print,
                   io=memory_file) -> dict:
    """v1 -> v2 for every replied-to sender's memory file, resolved against
    the sheet's ordering. `io` is the module providing the Drive helpers,
    replaceable in tests."""
    counts = {"converted": 0, "would_convert": 0, "already_v2": 0,
              "no_file": 0, "errors": 0}
    senders = sorted({(r.get("sender_addr") or "").strip().lower()
                      for r in store.queue_rows()
                      if r.get("status") == sheets_queue.STATUS_REPLIED} - {""})
    out(f"Memory: {len(senders)} correspondent(s) with replied mail.")
    for addr in senders:
        try:
            name = io.memory_file_name(addr)
            file_id = io.find_file_id(drive, name)
            raw = io.read_file(drive, file_id)
            if not raw.strip():
                counts["no_file"] += 1
                continue
            watermark, body = io.parse_memory(raw)
            if watermark.ids is not None:
                counts["already_v2"] += 1
                continue
            n = watermark.legacy_count
            ids = watermark.resolve(store.messages_for_sender(addr))
            if not apply:
                counts["would_convert"] += 1
                out(f"  {addr}: folded {n} -> {len(ids)} id(s)")
                continue
            io.save_backup(drive, addr, raw, keep)
            io.write_file(drive, name, io.serialise_memory(addr, ids, body), file_id)
            counts["converted"] += 1
            out(f"  {addr}: folded {n} -> {len(ids)} id(s), written as v2")
        except Exception as exc:
            counts["errors"] += 1
            out(f"  {addr}: error ({exc.__class__.__name__}: {exc})")
    out("  " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in counts.items()))
    return counts


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Prepare Gmail for INDI_GMAIL_READS.")
    p.add_argument("--apply", action="store_true",
                   help="make the changes (default: dry run, reads only)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    from googleapiclient.discovery import build

    import check_and_reply
    from config import Config

    cfg = Config.from_env(non_interactive=True)
    check_and_reply.setup_logging(cfg.log_path, verbose=False)
    lock = check_and_reply.acquire_lock(cfg.lock_path)
    try:
        creds = check_and_reply.get_credentials(cfg)
        gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)

        store = sheets_queue.SheetStore(sheets, drive, cfg)
        store.queue_id = store._find_spreadsheet(sheets_queue.QUEUE_TITLE)
        store.responses_id = store._find_spreadsheet(sheets_queue.RESPONSES_TITLE)
        if not store.queue_id or not store.responses_id:
            print("Could not find both the Queue and Responses spreadsheets.")
            return 1
        queue = store.queue_rows()
        responses = store.response_rows()
        outbound = store.outbound_rows()
        if store.schema_report():
            print("The sheet's header rows are not as expected — fix that "
                  "first (backfill_labels.py --dump-rows 5 shows the shape):")
            for key, problem in store.schema_report().items():
                print(f"  {key}: {problem}")
            return 1

        print("DRY RUN — nothing will change.\n" if not args.apply else "APPLYING.\n")
        results = [
            label_replies(gmail, queue, responses, args.apply),
            label_outbound(gmail, outbound, args.apply),
            convert_memory(drive, store, args.apply,
                           getattr(cfg, "memory_backup_versions",
                                   memory_file.DEFAULT_BACKUP_VERSIONS)),
        ]
        if not args.apply:
            print("\nNothing was changed. Re-run with --apply to do it.")
        return 1 if any(r.get("errors") for r in results) else 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
