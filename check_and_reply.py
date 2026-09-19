#!/usr/bin/env python3
"""
check_and_reply.py

Reads unread mail for the Indi Artificial account, asks Claude for a
reply, and either drafts it in Gmail or sends it.

Run it:
    python check_and_reply.py --dry-run        # show what it would do
    python check_and_reply.py                  # draft or send for real
    python check_and_reply.py --message-id ID  # reprocess one message
    python check_and_reply.py --limit 3

Behaviour differences from the previous version, in order of how badly
they mattered:

1. **Reply guards.** Before anything else, each message is checked against
   `gmail_io.classify_message`: our own address, `Auto-Submitted`,
   `Precedence: bulk`, `List-Id`, no-reply senders, autoresponder subjects.
   Plus an optional sender allowlist and a per-sender daily reply cap.
   Without these, turning on INDI_SEND_AUTOMATICALLY means one vacation
   responder on the other end produces an unbounded mail loop.

2. **Per-message error isolation.** Each message runs inside try/except,
   and the queue row's `status` column records where it got to. One bad
   message no longer aborts the batch, and a crash mid-message no longer
   strands that message as permanently unread-and-already-queued.

3. **A lock file.** Overlapping cron runs used to double-process the same
   unread messages.

4. **Oldest first.** Gmail returns newest-first; processing in that order
   scrambled the chronology recorded in the queue and memory.

5. **Real logging**, to stdout and to log.txt — which the old code's
   warning messages already told the user to check, though nothing wrote it.

6. **Non-interactive auth.** Under cron, a missing/expired token used to
   drop into `run_local_server()`, which waits forever for a browser.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import anthropic

import claude_api
import costs
import gmail_io
import local_tools
import locking
import memory_file
import outbound
import self_code
import sheets_queue
from config import Config

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

log = logging.getLogger("indi")


# ---------------------------------------------------------------------------
# SETUP
# ---------------------------------------------------------------------------

def setup_logging(log_path: Path, verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    # Under pythonw.exe on Windows there is no console, and sys.stdout is
    # None — attaching a StreamHandler to it raises on the first log line.
    # The file handler below is what matters for a scheduled run anyway.
    if sys.stdout is not None:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        root.addHandler(stream)

    try:
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:
        print(f"Could not open log file {log_path}: {exc}", file=sys.stderr)

    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)


def acquire_lock(lock_path: Path) -> locking.FileLock:
    """Prevents two overlapping runs from processing the same unread mail.

    Backed by fcntl on POSIX and msvcrt on Windows — see locking.py. The
    OS releases it if this process dies, so a crash never leaves a stale
    lock behind.
    """
    try:
        return locking.FileLock(lock_path).acquire()
    except locking.LockBusy:
        raise SystemExit("Another run is already in progress — exiting.")


def get_credentials(cfg: Config):
    creds = None
    if cfg.token_path.exists():
        creds = Credentials.from_authorized_user_file(str(cfg.token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if cfg.non_interactive or not sys.stdin.isatty():
            raise SystemExit(
                f"No valid credentials at {cfg.token_path} and this is a "
                "non-interactive run. Run the script once from a terminal to "
                "complete the OAuth flow before scheduling it."
            )
        if not cfg.credentials_path.exists():
            raise SystemExit(f"Missing OAuth client file: {cfg.credentials_path}")
        flow = InstalledAppFlow.from_client_secrets_file(str(cfg.credentials_path), SCOPES)
        creds = flow.run_local_server(port=0)

    cfg.token_path.write_text(creds.to_json())
    try:
        os.chmod(cfg.token_path, 0o600)
    except OSError:
        pass
    if sys.platform.startswith("win"):
        # chmod on Windows only toggles the read-only flag; it does not
        # restrict other users. If the machine has more than one account,
        # tighten token.json's ACL by hand (icacls) or keep it on an
        # encrypted per-user volume.
        log.debug("On Windows, file permissions on %s are not enforced by chmod.",
                  cfg.token_path)
    return creds


def load_system_prompt(cfg: Config, self_address: str) -> str:
    """Reads system_prompt.txt and appends the runtime facts Claude can't
    know on its own — most importantly today's date, which the old version
    never supplied, leaving Claude to guess from its training cutoff any
    time a sender asked about scheduling or deadlines."""
    import datetime

    if cfg.system_prompt_path.exists():
        base = cfg.system_prompt_path.read_text(encoding="utf-8")
    else:
        log.warning("No system prompt at %s — using the built-in fallback.",
                    cfg.system_prompt_path)
        base = (
            "You are Indi Artificial, an AI email assistant. You are not human "
            "and have no body. Reply to the email below helpfully, honestly and "
            "concisely, in plain text with no markdown."
        )

    today = datetime.datetime.now().astimezone()
    mode = (
        "This reply will be SENT IMMEDIATELY to the recipient without any human "
        "review. Write accordingly: be careful, and say plainly when you are "
        "unsure rather than guessing."
        if cfg.send_automatically else
        "This reply will be saved as a Gmail DRAFT for Steve to review before "
        "it goes anywhere."
    )

    runtime = (
        "\n\n--- RUNTIME CONTEXT (added automatically each run) ---\n"
        f"Today's date and time: {today.strftime('%A, %B %d, %Y at %H:%M %Z')}.\n"
        f"Your email address: {self_address}\n"
        f"{mode}\n"
        "You cannot send mail on your own initiative and you have no scheduler. "
        "You can only reply to a message that arrives. Never promise to follow "
        "up later, check back, or send something separately — you have no way "
        "to do any of that.\n"
        "Keep replies to a few short paragraphs unless the sender clearly wants "
        "more."
    )
    return base + runtime


# ---------------------------------------------------------------------------
# GMAIL HELPERS
# ---------------------------------------------------------------------------

def get_unread(gmail, limit: int) -> list[dict]:
    result = (
        gmail.users().messages()
        .list(userId="me", labelIds=["INBOX", "UNREAD"], maxResults=limit)
        .execute()
    )
    return result.get("messages", [])


def get_detail(gmail, msg_id: str) -> dict:
    return gmail.users().messages().get(userId="me", id=msg_id, format="full").execute()


def mark_as_read(gmail, msg_id: str) -> None:
    gmail.users().messages().modify(
        userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
    ).execute()


def self_addresses(gmail) -> set[str]:
    try:
        profile = gmail.users().getProfile(userId="me").execute()
        addr = (profile.get("emailAddress") or "").lower()
        return {addr} if addr else set()
    except Exception as exc:
        log.warning("Could not read the account's own address: %s", exc)
        return set()


# ---------------------------------------------------------------------------
# PER-MESSAGE PIPELINE
# ---------------------------------------------------------------------------

def process_message(gmail, drive, claude, cfg: Config, store: sheets_queue.SheetStore,
                    raw_message: dict, selves: set[str], system_prompt: str) -> str:
    """Handles one message end to end. Returns a short outcome string.
    Raises only on genuinely unexpected errors, which the caller records
    as `failed` so the message can be retried next run."""
    msg = gmail_io.extract_message(raw_message, cfg.max_body_chars)
    sender = msg["from_addr"]
    log.info("Message from %s — %s", msg["from_raw"], msg["subject"])

    existing = store.find_queue_row(msg["gmail_msg_id"])
    if existing and existing.get("status") in sheets_queue.TERMINAL_STATUSES:
        # Already handled on a previous run. Make sure it's marked read so
        # it stops coming back, then move on. The old code hit `continue`
        # here without marking it read, so such a message was re-fetched
        # and re-skipped on every single run, forever.
        log.info("Already handled (%s) — marking read.", existing.get("status"))
        if not cfg.dry_run:
            mark_as_read(gmail, msg["gmail_msg_id"])
        return "already handled"

    # --- guards ---------------------------------------------------------
    should_reply, reason = gmail_io.classify_message(msg, selves)
    if should_reply and not cfg.is_allowed(sender):
        should_reply, reason = False, "sender not in INDI_ALLOWED_SENDERS"
    if should_reply and cfg.max_replies_per_sender_per_day:
        recent = store.replies_in_last_day(sender)
        if recent >= cfg.max_replies_per_sender_per_day:
            should_reply = False
            reason = f"per-sender daily cap reached ({recent})"

    if not should_reply:
        log.info("Not replying: %s", reason)
        if cfg.dry_run:
            return f"would skip ({reason})"
        row = existing or store.append_queue_row(msg, sheets_queue.STATUS_SKIPPED, reason)
        if existing:
            store.set_status(row, sheets_queue.STATUS_SKIPPED, reason)
        mark_as_read(gmail, msg["gmail_msg_id"])
        return f"skipped ({reason})"

    trusted = cfg.is_trusted(sender)
    scope_all_history = cfg.may_search_all_history(sender)

    # Privileged tiers are granted by address, and a From header is free
    # text. Verify the message actually came from that domain before
    # honouring any tier above the baseline; on failure, drop to baseline
    # rather than refusing, so ordinary mail from misconfigured domains
    # still gets a reply.
    if cfg.require_authenticated_sender and (trusted or scope_all_history):
        authentic, why = gmail_io.sender_is_authenticated(msg)
        if not authentic:
            log.warning(
                "Sender %s claims a privileged tier but failed authentication "
                "(%s) — treating as untrusted.", sender, why,
            )
            trusted = False
            scope_all_history = False

    log.info("Sender is %s; history scope: %s.",
             "trusted (full toolset)" if trusted else "untrusted (restricted toolset)",
             "all correspondents" if scope_all_history else "own thread only")

    # Local machine access is a second, narrower permission. A sender must
    # be on INDI_LOCAL_TOOLS_SENDERS *and* be trusted; see
    # Config.may_use_local_tools. Everyone else gets no runner at all, so
    # the tools are never even described to the model.
    local_runner = None
    if cfg.may_use_local_tools(sender):
        can_read = cfg.enable_local_files and cfg.local_files_root is not None
        can_write = can_read and cfg.local_files_writable_root is not None
        can_exec = cfg.enable_local_exec
        if can_read or can_exec:
            local_runner = local_tools.LocalToolRunner(
                cfg, sender, can_read=can_read, can_write=can_write, can_exec=can_exec
            )
            log.warning(
                "LOCAL TOOLS ENABLED for %s — read=%s write=%s exec=%s (%s)",
                sender, can_read, can_write, can_exec,
                cfg.local_exec_backend if can_exec else "no exec",
            )

    # --- queue row ------------------------------------------------------
    if cfg.dry_run:
        row = None
    elif existing:
        row = existing
        store.set_status(row, sheets_queue.STATUS_QUEUED, "retry")
    else:
        row = store.append_queue_row(msg, sheets_queue.STATUS_QUEUED)

    self_code_runner = None
    if cfg.enable_self_code:
        self_code_runner = self_code.SelfCodeRunner(
            cfg, can_modify=cfg.may_modify_own_source(sender)
        )
        if self_code_runner.can_modify:
            log.warning("SELF-MODIFICATION enabled for %s (auto-apply=%s)",
                        sender, cfg.self_code_auto_apply)

    # --- context --------------------------------------------------------
    memory = memory_file.SenderMemory(drive, sender)
    pending = memory.pending_text(store)

    attachment_blocks, uploaded_ids = claude_api.build_attachment_blocks(
        gmail, cfg, msg, claude, trusted
    )
    if msg["attachment_refs"]:
        log.info("%d attachment(s) on this message.", len(msg["attachment_refs"]))

    # --- generate -------------------------------------------------------
    try:
        reply_text, stats = claude_api.generate_reply(
            claude, cfg, system_prompt, msg,
            memory_body=memory.body,
            pending_text=pending,
            store=store,
            trusted=trusted,
            scope_all_history=scope_all_history,
            attachment_blocks=attachment_blocks,
            local_runner=local_runner,
            self_code_runner=self_code_runner,
        )
    finally:
        claude_api.delete_uploaded_files(claude, uploaded_ids)

    if stats["server_tools"]:
        log.info("Claude made %d server tool call(s).", stats["server_tools"])
    if stats["history_searches"]:
        log.info("Claude searched message history %d time(s).", stats["history_searches"])
    if stats.get("local_tools"):
        log.warning("Claude made %d local tool call(s) on this machine.",
                    stats["local_tools"])
    if stats["cache_read"] or stats["cache_created"]:
        log.info("Prompt cache: %d read, %d written.",
                 stats["cache_read"], stats["cache_created"])

    # Server tool calls are web searches under the current config; code
    # execution is the only other server tool and it's off. If that
    # changes, this over-bills, because code execution isn't priced per
    # call the way search is.
    usd = costs.record("reply", cfg.model, stats["tokens"],
                       web_searches=stats["server_tools"] if cfg.enable_web_search else 0,
                       note=sender)
    if usd is not None:
        log.info("This message cost about $%.4f.", usd)

    if stats.get("self_code"):
        log.warning("Claude used the self-modification tools %d time(s).",
                    stats["self_code"])

    if stats.get("set_aside"):
        reason = stats["set_aside"]
        log.info("Indi chose not to reply: %s", reason)
        if cfg.dry_run:
            return f"would set aside ({reason})"
        if row:
            store.set_status(row, sheets_queue.STATUS_IGNORED, reason)
        mark_as_read(gmail, msg["gmail_msg_id"])
        return f"set aside without replying ({reason})"

    if not reply_text.strip():
        # Never draft or send a blank email.
        if row:
            store.set_status(row, sheets_queue.STATUS_FAILED, "empty reply from model")
        return "no reply generated (left unread for retry)"

    clean_body, attachments, warnings = claude_api.extract_outgoing_attachments(
        reply_text, cfg.max_outgoing_attachment_bytes
    )
    for warning in warnings:
        log.warning("%s", warning)
    if attachments:
        log.info("Reply carries %d generated attachment(s): %s",
                 len(attachments), ", ".join(n for n, _ in attachments))

    safe_attachments = [
        (gmail_io.sanitise_filename(name), content) for name, content in attachments
    ]

    if cfg.dry_run:
        print("\n" + "=" * 70)
        print(f"TO:      {msg['reply_to_raw']}")
        print(f"SUBJECT: Re: {msg['subject']}")
        print("-" * 70)
        print(clean_body)
        for name, content in safe_attachments:
            print(f"\n[attachment {name}: {len(content)} chars]")
        print("=" * 70 + "\n")
        return "dry run (nothing written)"

    mime = gmail_io.build_reply_mime(
        to_addr=msg["reply_to_raw"],
        subject=msg["subject"],
        body=clean_body,
        in_reply_to=msg["message_id"],
        references=msg["references"],
        attachments=safe_attachments,
    )
    mime["threadId"] = msg["thread_id"]

    if cfg.send_automatically:
        gmail.users().messages().send(userId="me", body=mime).execute()
        outcome = "sent"
    else:
        gmail.users().drafts().create(userId="me", body={"message": mime}).execute()
        outcome = "drafted"

    # Record the response only after the mail actually went somewhere, so
    # the sheets never claim a reply that doesn't exist.
    store.append_response(msg["gmail_msg_id"], sender, clean_body)
    store.set_status(row, sheets_queue.STATUS_REPLIED, outcome)
    mark_as_read(gmail, msg["gmail_msg_id"])
    log.info("Reply %s.", outcome)

    memory_file.consolidate(claude, cfg, drive, store, sender)
    return outcome


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Indi Artificial email agent")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be sent; write nothing anywhere")
    p.add_argument("--limit", type=int, default=None,
                   help="override INDI_MAX_UNREAD_PER_RUN")
    p.add_argument("--message-id", default=None,
                   help="process one specific Gmail message id")
    p.add_argument("--non-interactive", action="store_true",
                   help="fail instead of starting an OAuth browser flow")
    p.add_argument("--heartbeat", action="store_true",
                   help="run an initiative cycle instead of checking mail: Indi "
                        "decides whether to write to anyone, journal, or do nothing")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def record_spend(store, cfg: Config) -> None:
    """Log what this run cost, then append it to the ledger."""
    rows = costs.run_rows()
    if not rows:
        return
    log.info("Claude spend this run: %s", costs.run_summary())
    if store is None or cfg.dry_run:
        return
    try:
        store.append_costs(rows)
        spent, calls = store.spend_since(24)
        log.info("Spend in the last 24h: $%.4f over %d call(s).", spent, calls)
    except Exception:
        # Bookkeeping must never turn a run that replied fine into a
        # failure, so this swallows rather than propagates.
        log.warning("Could not write the cost ledger.", exc_info=True)


def main() -> int:
    args = parse_args()
    cfg = Config.from_env(dry_run=args.dry_run, non_interactive=args.non_interactive)
    setup_logging(cfg.log_path, args.verbose)

    lock = acquire_lock(cfg.lock_path)
    store = None
    try:
        creds = get_credentials(cfg)
        gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
        claude = anthropic.Anthropic(api_key=cfg.api_key)

        selves = self_addresses(gmail)
        system_prompt = load_system_prompt(cfg, ", ".join(selves) or "unknown")

        if cfg.dry_run:
            log.info("DRY RUN — no mail sent, no sheets or memory written.")
        if cfg.send_automatically and not cfg.dry_run:
            log.warning("INDI_SEND_AUTOMATICALLY is on: replies go out without review.")
        problems = cfg.validate_local_tools()
        if problems:
            for problem in problems:
                log.error("%s", problem)
            raise SystemExit(
                "Refusing to start with an unsafe local-tools configuration."
            )
        if cfg.enable_local_files or cfg.enable_local_exec:
            log.warning(
                "Local machine access is ON for %s. Files: %s%s. Exec: %s.",
                ", ".join(cfg.local_tools_senders),
                cfg.local_files_root if cfg.enable_local_files else "off",
                f" (writable: {cfg.local_files_writable_root})"
                if cfg.local_files_writable_root else "",
                cfg.local_exec_backend if cfg.enable_local_exec else "off",
            )
            if cfg.enable_local_exec and cfg.local_exec_backend == "subprocess":
                log.warning(
                    "Exec backend is 'subprocess': code runs as your user on "
                    "this kernel. Resource-limited, but not isolated. Prefer "
                    "docker."
                )
        if not cfg.trusted_senders:
            log.info("No trusted senders configured — the powerful tools "
                     "(web fetch, code execution, cross-sender history) are off "
                     "for everyone.")

        store = sheets_queue.SheetStore(sheets, drive, cfg)
        store.ensure_spreadsheets()

        if args.heartbeat:
            if not cfg.enable_outbound:
                log.info("Outbound initiative is disabled (INDI_ENABLE_OUTBOUND).")
                return 0
            outcome = outbound.run_heartbeat(
                claude, cfg, gmail, store, system_prompt, selves
            )
            log.info("Heartbeat: %s", outcome)
            return 0

        if args.message_id:
            ids = [{"id": args.message_id}]
        else:
            ids = get_unread(gmail, args.limit or cfg.max_unread_per_run)
        if not ids:
            log.info("No unread messages.")
            return 0

        details = []
        for ref in ids:
            try:
                details.append(get_detail(gmail, ref["id"]))
            except Exception as exc:
                log.error("Could not fetch message %s: %s", ref["id"], exc)
        # Oldest first, so a drained backlog is recorded in the order it
        # actually arrived.
        details.sort(key=lambda d: int(d.get("internalDate", 0) or 0))

        failures = 0
        for detail in details:
            try:
                outcome = process_message(
                    gmail, drive, claude, cfg, store, detail, selves, system_prompt
                )
                log.info("Outcome: %s", outcome)
            except Exception as exc:
                failures += 1
                log.exception("Failed on message %s: %s", detail.get("id"), exc)
                # Mark the row failed so the next run retries it instead of
                # treating a half-finished message as already handled.
                try:
                    row = store.find_queue_row(detail.get("id", ""))
                    if row and not cfg.dry_run:
                        store.set_status(row, sheets_queue.STATUS_FAILED, str(exc)[:200])
                except Exception:
                    log.debug("Could not record failure status", exc_info=True)

        log.info("Run complete: %d message(s), %d failure(s).", len(details), failures)
        return 1 if failures else 0
    finally:
        # In `finally` so a run that raised still records what it spent
        # before dying — those calls were billed either way. Covers the
        # heartbeat and the no-mail path too, which return early.
        record_spend(store, cfg)
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
