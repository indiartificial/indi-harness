#!/usr/bin/env python3
"""
grants.py

List, add and withdraw sharing grants by hand (Phase 2). A grant lets
Indi show one person's mail — one message, or all of it — to someone who
wasn't on it. In later phases consultation creates grants from the
owner's own authenticated reply; until then, and for testing, the
operator records them here, and should only do so on the owner's say-so.

    cd ~/harness
    /home/indi/venv/bin/python grants.py list
    /home/indi/venv/bin/python grants.py add --owner bob@example.com --to ann@example.com
    /home/indi/venv/bin/python grants.py add --owner bob@example.com --to ann@example.com \\
        --item 1a0e3146c44faf7d --note "Bob said yes by phone, 27 Sep"
    /home/indi/venv/bin/python grants.py revoke g-3f9a1c2b7e

Grants last until withdrawn. Withdrawing fills in `revoked_at` on the
grant's row; it never deletes it, so the record of who could see what,
and when, is kept. It takes the harness lock, so it can't collide with a
scheduled run.
"""

from __future__ import annotations

import argparse
import sys

import audience


def run(book: audience.GrantBook, args, out=print) -> int:
    if args.cmd == "list":
        rows = book.all()
        if not rows:
            out("No grants.")
        for g in rows:
            state = f"withdrawn {g.revoked_at}" if g.revoked_at else "active"
            scope = "all their mail" if g.scope == audience.ALL_ITEMS else f"message {g.scope}"
            out(f"{g.grant_id}  {g.owner} -> {g.to}  ({scope})  granted {g.granted_at}  {state}")
        return 0
    if args.cmd == "add":
        g = book.add(args.owner, args.to, args.item or audience.ALL_ITEMS,
                     evidence=args.evidence or "", note=args.note or "")
        out(f"Added {g.grant_id}: {g.owner}'s "
            f"{'mail' if g.scope == audience.ALL_ITEMS else 'message ' + g.scope} "
            f"may be shown to {g.to}.")
        return 0
    if args.cmd == "revoke":
        if book.revoke(args.grant_id):
            out(f"Withdrew {args.grant_id}. Anything already sent is not recalled.")
            return 0
        out(f"No active grant {args.grant_id}.")
        return 1
    return 2


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Manage Indi's sharing grants.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show every grant, active and withdrawn")
    a = sub.add_parser("add", help="record a grant")
    a.add_argument("--owner", required=True, help="whose mail it is")
    a.add_argument("--to", required=True, help="who may now see it")
    a.add_argument("--item", help="one Gmail message id (default: all their mail)")
    a.add_argument("--evidence", help="Gmail id of the message where they agreed")
    a.add_argument("--note", help="a line on how the consent was given")
    r = sub.add_parser("revoke", help="withdraw a grant")
    r.add_argument("grant_id")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    from googleapiclient.discovery import build

    import check_and_reply
    from config import Config

    cfg = Config.from_env(non_interactive=True)
    lock = check_and_reply.acquire_lock(cfg.lock_path)
    try:
        creds = check_and_reply.get_credentials(cfg)
        gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
        store = check_and_reply.build_store(cfg, gmail, drive, sheets)
        store.ensure_spreadsheets()
        return run(audience.GrantBook(store), args)
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
