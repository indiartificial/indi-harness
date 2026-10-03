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

7. **The same history is not sent twice.** The queue's not-yet-folded
   exchanges and the live thread overlap almost completely during an
   active conversation; the ones already present in the thread text are
   left out of the pending block.

8. **The terms gate.** Before the reply model is called at all, the
   sender has to have accepted the Terms of Service — see
   `terms_gate.py`. A stranger's first message is answered with the
   terms and nothing else. This sits between the guards above and every
   other thing this file does, because it decides whether there is
   anything to do.

9. **Unsubscribing is code, not judgement.** Just after the gate, a
   message from a newsletter subscriber that says nothing but
   "unsubscribe" takes them off the list and is acknowledged with a
   fixed reply, without the reply model being asked anything. See
   `newsletter.py`.
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

import attachments
import claude_api
import claude_code
import correspondents
import costs
import gmail_io
import gmail_reads
import drive_tables
import alerts
import audience
import consult
import gmail_store
import followups
import ledger
import reply_all
import local_tools
import locking
import memory_file
import newsletter
import outbound
import self_code
import sheets_queue
import terms
import terms_gate
import config
from config import Config, matches_sender

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# The sign-off that ends every outgoing message. It is also asked for in
# the system prompt, but a rule that only lives in a prompt is a rule the
# model can quietly forget on any given run — and it did. `ensure_signature`
# below makes it a property of the code path instead.
SIGNATURE = "--- Indi"

# How much of a stored message body has to turn up in the rendered thread
# before that exchange counts as already shown. Long enough not to match
# by coincidence, short enough to survive the per-message clipping the
# thread renderer applies.
PENDING_PROBE_CHARS = 200

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
        # Normal, not a failure: two runs overlapped. Exit 0 so systemd
        # doesn't report the unit failed (and alerts.py doesn't page anyone).
        logging.getLogger("indi").info("Another run is already in progress — exiting.")
        raise SystemExit(0)


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


def ensure_signature(body: str) -> str:
    """Guarantee that an outgoing body ends with the signature.

    A no-op if the signature is already the last non-empty line (or close
    to it — a stray blank line or a postscript-free sign-off underneath is
    still fine). Empty bodies are left alone; the caller refuses to send
    those anyway.
    """
    if not body or not body.strip():
        return body
    tail = [line.strip() for line in body.strip().splitlines() if line.strip()]
    if SIGNATURE in tail[-3:]:
        return body
    return body.rstrip() + "\n\n" + SIGNATURE


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

    # What Indi can and cannot do about time. This used to say flatly that
    # there was no scheduler and that no follow-up could ever be promised,
    # which was true when it was written and has been false since outbound
    # initiative and reminders landed. A system prompt that understates the
    # tools is not a safe default: it made Indi tell someone asking for a
    # medication reminder that the capability didn't exist, while the tool
    # to schedule exactly that was sitting in its own request.
    if cfg.enable_outbound:
        initiative = (
            "You have two ways to act outside a reply. A periodic heartbeat run "
            "lets you start a message on your own initiative, within a small "
            "daily budget. And the schedule_reminder tool, when it is offered, "
            "books an email to the person you are replying to for a future time "
            "you specify; it is delivered by ordinary code, not by any later "
            "decision of yours, so a reminder you schedule really will arrive. "
            "It can repeat on an interval, within bounds the tool describes, "
            "and list_reminders and cancel_reminder let that person see and "
            "stop their own — nobody else's.\n"
            "You can also plan to come back to a thread yourself with "
            "schedule_follow_up, when it is offered.\n"
            "That is the whole of it. Promise a reminder or a follow-up only "
            "when you have just scheduled it with the tool and the tool said it "
            "worked. Never promise to check back on something, watch for "
            "something, or send something separately otherwise — you have no "
            "way to do it."
        )
    else:
        initiative = (
            "You cannot send mail on your own initiative and you have no "
            "scheduler. You can only reply to a message that arrives. Never "
            "promise to follow up later, check back, or send something "
            "separately — you have no way to do any of that."
        )

    runtime = (
        "\n\n--- RUNTIME CONTEXT (added automatically each run) ---\n"
        f"Today's date and time: {today.strftime('%A, %B %d, %Y at %H:%M %Z')}.\n"
        f"Your email address: {self_address}\n"
        f"{mode}\n"
        f"{initiative}\n"
        "Keep replies to a few short paragraphs unless the sender clearly wants "
        "more. Don't sign off with your name: your signature is added "
        "automatically."
    )
    return base + runtime + "\n" + audience.PROMPT


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


def get_thread_messages(gmail, thread_id: str) -> list[dict]:
    """Every message Gmail holds in this thread, including ones we sent.

    One extra API call per reply, and the thing it buys is that Indi can
    see the conversation it is part of.
    """
    if not thread_id:
        return []
    thread = (
        gmail.users().threads()
        .get(userId="me", id=thread_id, format="full")
        .execute()
    )
    return thread.get("messages", []) or []


def mark_as_read(gmail, msg_id: str) -> None:
    gmail.users().messages().modify(
        userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
    ).execute()


MAX_CHECK_ATTEMPTS = 3
ATTEMPTS_FILE = "check_attempts.json"


def _check_unavailable(cfg, gmail, store, msg: dict, row, why: str) -> str:
    """The pre-send check couldn't run. Leave the message for the next run,
    or, after MAX_CHECK_ATTEMPTS, give up: mark it read, say so in the log
    at ERROR, and send the sender the plain notice."""
    import json
    log.warning("Pre-send check unavailable for %s: %s", msg["gmail_msg_id"], why)
    if cfg.dry_run:
        return f"pre-send check unavailable ({why[:120]})"
    path = cfg.base_dir / "data" / ATTEMPTS_FILE
    try:
        attempts = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        attempts = {}
    count = attempts.get(msg["gmail_msg_id"], 0) + 1
    attempts[msg["gmail_msg_id"]] = count
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Only the most recent ids matter; keep the file small.
        path.write_text(json.dumps(dict(list(attempts.items())[-200:])))
    except Exception:
        log.warning("Could not record check attempts.", exc_info=True)
    if count < MAX_CHECK_ATTEMPTS:
        if row:
            store.set_status(row, sheets_queue.STATUS_FAILED,
                             f"pre-send check unavailable (attempt {count})")
        return (f"pre-send check unavailable, attempt {count} of "
                f"{MAX_CHECK_ATTEMPTS}; left unread to retry")
    log.error("Giving up on %s after %d attempts: the pre-send check could not run (%s)",
              msg["gmail_msg_id"], count, why)
    outcome = f"not sent: the pre-send check could not run after {count} attempts"
    if row:
        store.set_status(row, sheets_queue.STATUS_SKIPPED, outcome)
    mark_as_read(gmail, msg["gmail_msg_id"])
    return outcome + "; " + send_withheld_notice(cfg, gmail, store, msg, NOTICE_UNABLE)


NOTICE_UNABLE = ("Thank you for your message. I'm unable to respond to it.")
NOTICE_DAILY_CAP = ("Thank you for your message. I've reached the number of replies "
                    "I can send you in a day, so I'm unable to respond to this one. "
                    "Messages you send once that limit resets will be answered as usual.")


def send_withheld_notice(cfg, gmail, store, msg: dict, text: str) -> str:
    """Tell the sender, in their thread and to them alone, that Indi is
    unable to respond. At most one a day per person, so a busy sender gets
    one note rather than one per message; and only with a store that can
    count them, since an uncounted note is how a loop would begin.
    Never raises: the message is already dealt with when this runs."""
    to = (msg.get("reply_to_addr") or msg.get("from_addr") or "").lower()
    counter = getattr(store, "notices_in_last_day", None)
    if not to or counter is None:
        return "no notice (can't count notices with this store)"
    try:
        if counter(to) >= 1:
            return "notice already sent today"
        mime = gmail_io.build_reply_mime(
            to_addr=msg.get("reply_to_raw") or to, subject=msg.get("subject", ""),
            body=ensure_signature(text), in_reply_to=msg.get("message_id", ""),
            references=msg.get("references", ""))
        mime["threadId"] = msg.get("thread_id", "")
        if cfg.send_automatically:
            result = gmail.users().messages().send(userId="me", body=mime).execute() or {}
            sent_id, how = result.get("id", ""), "sent"
        else:
            result = gmail.users().drafts().create(
                userId="me", body={"message": mime}).execute() or {}
            sent_id, how = (result.get("message") or {}).get("id", ""), "drafted"
        store.record_notice(sent_id)
        log.info("Unable-to-respond notice %s to %s.", how, to)
        return f"notice {how}"
    except Exception:
        log.warning("Could not send the unable-to-respond notice to %s.", to,
                    exc_info=True)
        return "notice failed"


def quoting_enabled() -> bool:
    """INDI_QUOTE_REPLIES, on unless set to false."""
    return (gmail_reads._setting("INDI_QUOTE_REPLIES") or "true").lower() not in (
        "0", "false", "no", "off")


SHARED_LOOKBACK = 40       # recent handled messages considered
SHARED_MAX = 5             # shown
SHARED_MAX_CHARS = 6000


def shared_context(store, recipients, thread_id: str) -> str:
    """The latest few messages, on other threads, that every recipient of
    this reply was party to (audience.py), with Indi's replies they saw."""
    ids = store._handled_ids(limit=SHARED_LOOKBACK)
    meta = store._meta(ids)
    cleared = [i for i in store._cleared(ids, recipients)
               if meta.get(i, {}).get("thread") != thread_id][:SHARED_MAX]
    if not cleared:
        return ""
    rows = store._rows(cleared)
    replies = store.responses_for(rows, cleared_for=recipients)
    text = sheets_queue.format_exchanges(rows, replies, max_chars_each=900)
    return text[-SHARED_MAX_CHARS:]


def _addressed(raw: dict, selves) -> str:
    """The incoming message's From, To and Cc, for the model — and, when
    Indi was on neither To nor Cc, what that means for the reply."""
    hdrs = gmail_reads._headers(raw)
    lines = [f"From: {hdrs.get('from', '')}", f"To: {hdrs.get('to', '') or '(none)'}"]
    if hdrs.get("cc"):
        lines.append(f"Cc: {hdrs['cc']}")
    visible = audience.addrs(hdrs.get("to", "")) | audience.addrs(hdrs.get("cc", ""))
    if selves and not visible & {s.lower() for s in selves}:
        lines.append("You were not on To or Cc: this reached you by Bcc, a list "
                     "or a forward. The people named above don't know you have "
                     "it; your reply goes to the sender alone, and must not "
                     "reveal it to anyone else.")
    return "\n".join(lines)


def _meta_of(raw: dict) -> dict:
    """The From/To/Cc of a full Gmail message, as audience.py reads them."""
    hdrs = gmail_reads._headers(raw)
    return {"from": hdrs.get("from", ""), "to": hdrs.get("to", ""),
            "cc": hdrs.get("cc", "")}


def build_store(cfg: Config, gmail, drive, sheets):
    """The store for this configuration (see MIGRATE-GMAIL.md): Gmail and
    Drive records, Gmail with the sheet, or the sheet alone."""
    if gmail_store.drive_records_enabled(cfg):
        # Steps 6-7: no spreadsheet at all. Mail and its status are in
        # Gmail; journal, costs, reminders, newsletter and decisions are
        # CSV files in Drive.
        if not gmail_store.gmail_reads_enabled(cfg):
            raise SystemExit("INDI_DRIVE_RECORDS=true needs INDI_GMAIL_READS=true "
                             "as well — the mail would have no store.")
        store = gmail_store.GmailStore(
            drive_tables.DriveSheets(drive_tables.DriveIO(drive)),
            drive, cfg, gmail, records_in_drive=True)
        log.debug("Mail reads come from Gmail (INDI_GMAIL_READS); records "
                 "are in Drive (INDI_DRIVE_RECORDS).")
    elif gmail_store.gmail_reads_enabled(cfg):
        # Step 5 of MIGRATE-GMAIL.md: Gmail answers every read about
        # mail; the sheet is still written, and still answers for the
        # journal, costs, reminders and newsletter.
        store = gmail_store.GmailStore(sheets, drive, cfg, gmail)
        log.debug("Mail reads come from Gmail (INDI_GMAIL_READS).")
    else:
        store = sheets_queue.SheetStore(
            sheets, drive, cfg,
            gmail_service=gmail if cfg.gmail_status_labels else None,
            # Comparison logging only (MIGRATE-GMAIL.md, step 3); off by
            # default, and the sheet still answers every read either way.
            shadow_gmail_service=(gmail if gmail_reads.shadow_reads_enabled(cfg)
                                  else None),
        )
    return store


def self_addresses(gmail) -> set[str]:
    try:
        profile = gmail.users().getProfile(userId="me").execute()
        addr = (profile.get("emailAddress") or "").lower()
        return {addr} if addr else set()
    except Exception as exc:
        log.warning("Could not read the account's own address: %s", exc)
        return set()


# ---------------------------------------------------------------------------
# NOT SENDING THE SAME HISTORY TWICE
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    return " ".join((text or "").split()).lower()


def pending_shown_in_thread(row_body: str, thread_norm: str,
                            probe_chars: int = PENDING_PROBE_CHARS) -> bool:
    """Is this stored exchange already visible in the rendered thread?

    Checked by content, not just by id: the thread renderer drops older
    messages and clips to a character budget, so an id being in the
    thread is not proof its text survived. Erring towards False keeps a
    duplicate; erring towards True would lose context, which is worse.
    """
    body = _normalise(row_body)
    if len(body) < 40 or not thread_norm:
        return False
    return body[:probe_chars] in thread_norm


def redundant_pending_ids(pending_rows: list[dict], thread_ids: set[str],
                          thread_text: str) -> set[str]:
    thread_norm = _normalise(thread_text)
    if not thread_norm:
        return set()
    return {
        row.get("gmail_msg_id", "")
        for row in pending_rows
        if row.get("gmail_msg_id") in thread_ids
        and pending_shown_in_thread(row.get("body", ""), thread_norm)
    } - {""}


# ---------------------------------------------------------------------------
# PER-MESSAGE PIPELINE
# ---------------------------------------------------------------------------

def process_message(gmail, drive, claude, cfg: Config, store: sheets_queue.SheetStore,
                    raw_message: dict, selves: set[str], system_prompt: str,
                    people: correspondents.CorrespondentStore,
                    released_to: list[str] | None = None) -> str:
    """Handles one message end to end. Returns a short outcome string.
    Raises only on genuinely unexpected errors, which the caller records
    as `failed` so the message can be retried next run."""
    msg = gmail_io.extract_message(raw_message, cfg.max_body_chars,
                                   max_quoted_chars=cfg.max_quoted_chars)
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

    # --- an answer to a consultation (consult.py) -----------------------
    # Mail in a thread Indi started to ask someone's permission is their
    # answer, handled by code and never replied to as ordinary mail.
    consult_on = consult.enabled(cfg) and getattr(store, "supports_audience", False)
    if consult_on:
        pending_consult = consult.match(store, msg)
        if pending_consult is not None:
            ordinary, why_not = gmail_io.classify_message(msg, selves)
            outcome = (consult.handle_answer(claude, cfg, gmail, store, people, msg,
                                             pending_consult, selves)
                       if ordinary else f"automated message in a consultation ({why_not})")
            log.info("Consultation: %s", outcome)
            if cfg.dry_run:
                return outcome
            if existing:
                store.set_status(existing, sheets_queue.STATUS_SKIPPED, outcome)
            else:
                store.append_queue_row(msg, sheets_queue.STATUS_SKIPPED, outcome)
            mark_as_read(gmail, msg["gmail_msg_id"])
            return outcome

    # --- guards ---------------------------------------------------------
    should_reply, reason = gmail_io.classify_message(msg, selves)
    # A person, not a machine, whose reply is being withheld by a setting
    # gets a short note saying so. An autoresponder, a list or a no-reply
    # address never does: that is how mail loops start.
    withheld = None
    if should_reply and not cfg.is_allowed(sender):
        should_reply, reason = False, "sender not in INDI_ALLOWED_SENDERS"
        withheld = NOTICE_UNABLE
    if should_reply and cfg.max_replies_per_sender_per_day:
        recent = store.replies_in_last_day(sender)
        if recent >= cfg.max_replies_per_sender_per_day:
            should_reply = False
            reason = f"per-sender daily cap reached ({recent})"
            withheld = NOTICE_DAILY_CAP

    if not should_reply:
        log.info("Not replying: %s", reason)
        if cfg.dry_run:
            return f"would skip ({reason})" + (" and send a notice" if withheld else "")
        row = existing or store.append_queue_row(msg, sheets_queue.STATUS_SKIPPED, reason)
        if existing:
            store.set_status(row, sheets_queue.STATUS_SKIPPED, reason)
        mark_as_read(gmail, msg["gmail_msg_id"])
        if withheld:
            reason += "; " + send_withheld_notice(cfg, gmail, store, msg, withheld)
        return f"skipped ({reason})"

    # --- terms of service -----------------------------------------------
    # Deliberately after the guards above and before everything below.
    # After, because there is no point papering an autoresponder. Before,
    # because everything below this line either costs money or says
    # something to the sender, and until they have accepted the terms we
    # are not entitled to do either.
    gate = terms_gate.check_inbound(cfg, gmail, claude, msg, people)
    if not gate.proceed:
        log.info("Terms gate: %s", gate.outcome)
        if cfg.dry_run:
            return f"would stop at the terms gate ({gate.outcome})"
        # `skipped` throughout, including where a fixed notice went out:
        # the status column is about whether the reply model produced a
        # reply, and the note says what the gate sent instead.
        if existing:
            store.set_status(existing, sheets_queue.STATUS_SKIPPED, gate.outcome)
        else:
            store.append_queue_row(msg, sheets_queue.STATUS_SKIPPED, gate.outcome)
        mark_as_read(gmail, msg["gmail_msg_id"])
        return f"terms gate: {gate.outcome}"
    if gate.outcome != "terms accepted":
        log.info("Terms gate: %s", gate.outcome)

    # --- unsubscribing from the newsletter -------------------------------
    # After the terms gate, deliberately: the gate is not something a
    # feature gets to step around, and nobody who has not passed it is
    # receiving the newsletter in the first place. Before the model,
    # equally deliberately — "unsubscribe" is an instruction with one
    # correct response, and it is handled by code that cannot decide to
    # do something else. Anyone who is not currently subscribed gets
    # None back and falls through to an ordinary reply.
    unsubscribed = newsletter.check_unsubscribe(cfg, gmail, store, msg)
    if unsubscribed:
        log.info("Newsletter: %s", unsubscribed)
        if cfg.dry_run:
            return f"would unsubscribe ({unsubscribed})"
        if existing:
            store.set_status(existing, sheets_queue.STATUS_SKIPPED, unsubscribed)
        else:
            store.append_queue_row(msg, sheets_queue.STATUS_SKIPPED, unsubscribed)
        mark_as_read(gmail, msg["gmail_msg_id"])
        return f"newsletter: {unsubscribed}"

    # --- who the reply goes to (reply_all.py) --------------------------
    # The sender, and with INDI_REPLY_ALL everyone else on the To and Cc.
    # Nobody is written to before accepting the terms: if anyone hasn't,
    # they are sent the terms and the message waits, uncomposed, until
    # they have answered or 72 hours pass. `released_to` is that later
    # run, with the people who accepted.
    reply_all_on = reply_all.enabled(cfg) and getattr(store, "supports_audience", False)
    if released_to is not None:
        others = [a for a in released_to if a != sender]
    else:
        _, others = reply_all.recipients_for(raw_message, msg, selves, people,
                                             reply_all_on)
        waiting = reply_all.undecided(others, people)
        if waiting:
            log.info("Waiting for %s to accept the terms before replying.",
                     ", ".join(waiting))
            if cfg.dry_run:
                return f"would send the terms to {', '.join(waiting)} and wait"
            if reply_all.waiting_for(store, msg["gmail_msg_id"]) is None:
                for addr in waiting:
                    log.info("Terms to %s: %s", addr,
                             reply_all.paper(cfg, gmail, people, addr, msg))
                reply_all.hold(store, msg, sender, others)
            note = f"awaiting terms from {', '.join(waiting)}"
            if existing:
                store.set_status(existing, sheets_queue.STATUS_QUEUED, note)
            else:
                store.append_queue_row(msg, sheets_queue.STATUS_QUEUED, note)
            mark_as_read(gmail, msg["gmail_msg_id"])
            return note
        others = [a for a in others if reply_all.accepted(a, people)]
    # The sender's own address stands for them here even if they set a
    # Reply-To: audience.py knows them by the address they wrote from.
    recipients = frozenset({sender.lower(), *others})
    if others:
        log.info("Reply goes to %s, copying %s.", msg["reply_to_addr"] or sender,
                 ", ".join(others))

    trusted = cfg.is_trusted(sender)
    scope_all_history = cfg.may_search_all_history(sender)
    # "*" in INDI_HISTORY_ALL_SENDERS used to mean everyone could search
    # everyone's mail. Phase 2 replaces that with the audience rule
    # (audience.py), so the wildcard no longer grants it; only addresses
    # named explicitly keep the operator's unrestricted search.
    if scope_all_history and "*" in cfg.history_all_senders:
        named = tuple(p for p in cfg.history_all_senders if p.strip() != "*")
        scope_all_history = config.matches_sender(named, sender)
        if not scope_all_history:
            log.info("INDI_HISTORY_ALL_SENDERS='*' is ignored: history search is "
                     "scoped by who has seen each message.")
    if scope_all_history and others:
        # Unrestricted search is for a reply that goes to its owner alone;
        # with others on it, it could put their mail in front of people
        # who never saw it.
        scope_all_history = False

    # Privileged tiers are granted by address, and a From header is free
    # text. Verify the message actually came from that domain before
    # honouring any tier above the baseline; on failure, drop to baseline
    # rather than refusing, so ordinary mail from misconfigured domains
    # still gets a reply.
    #
    # Checked once, unconditionally, because two different things need the
    # answer: the tier downgrade just below, which Steve can switch off
    # with INDI_REQUIRE_AUTHENTICATED_SENDER, and the Claude Code gate
    # further down, which he cannot.
    authentic, why = gmail_io.sender_is_authenticated(msg)
    if cfg.require_authenticated_sender and (trusted or scope_all_history):
        if not authentic:
            log.warning(
                "Sender %s claims a privileged tier but failed authentication "
                "(%s) — treating as untrusted.", sender, why,
            )
            trusted = False
            scope_all_history = False

    log.info("Sender is %s; history scope: %s.",
             "trusted (full toolset)" if trusted else "untrusted (restricted toolset)",
             "all mail, unrestricted" if scope_all_history
             else "what everyone on the reply has seen")

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
            cfg, can_modify=cfg.may_modify_own_source(sender),
            # .env names who may do what; readable and changeable only by an
            # authenticated sender who may change the harness, in a reply to
            # them alone (self_code.py).
            can_read_settings=(cfg.may_modify_own_source(sender) and authentic
                               and not others),
        )
        if self_code_runner.can_modify:
            log.warning("SELF-MODIFICATION enabled for %s (auto-apply=%s)",
                        sender, cfg.self_code_auto_apply)

    # The unrestricted one. Nothing in the harness is held back from a
    # session started here — including this check. See claude_code.py.
    claude_code_runner = None
    if cfg.may_run_claude_code(sender, authentic):
        claude_code_runner = claude_code.ClaudeCodeRunner(cfg)
        log.warning(
            "CLAUDE CODE ENABLED for %s — unrestricted access to the harness, "
            "auto-apply=%s, ceiling $%.2f per session, %d per reply.",
            sender, cfg.claude_code_auto_apply, cfg.claude_code_max_usd,
            cfg.max_claude_code_runs_per_reply,
        )
    elif (cfg.enable_claude_code and not authentic
          and matches_sender(cfg.claude_code_senders, sender)):
        log.warning(
            "Sender %s is on INDI_CLAUDE_CODE_SENDERS but the message failed "
            "authentication (%s) — no Claude Code session.", sender, why,
        )

    # The agenda (agenda.py) goes to exactly the senders who already hold
    # one of the two source-and-machine permissions above, and only when
    # the trust check for this message held — `trusted` has already been
    # dropped if authentication failed. Nothing new is granted by
    # address; this reuses those gates and changes neither.
    agenda_owner = ""
    if trusted and (cfg.may_modify_own_source(sender) or cfg.may_use_local_tools(sender)):
        agenda_owner = sender

    # --- context --------------------------------------------------------
    memory = memory_file.SenderMemory(drive, sender)

    # What was actually said earlier in this thread. The queue's "pending"
    # rows and the memory file are summaries of past exchanges with this
    # person in general; neither is the conversation now in progress, and a
    # short reply like "yes, do that" means nothing without it.
    thread_text, thread_count = "", 0
    thread_messages: list[dict] = []
    if cfg.max_thread_chars > 0 and cfg.max_thread_messages > 0:
        try:
            thread_messages = get_thread_messages(gmail, msg["thread_id"])
            # Only what everyone on the reply has already seen: someone
            # copied in partway through a thread never received its
            # earlier messages (audience.py).
            visible = [m for m in thread_messages
                       if audience.cleared(m.get("id", ""), _meta_of(m), recipients,
                                           selves, None)]
            if len(visible) < len(thread_messages):
                log.info("Thread context: %d earlier message(s) left out — not "
                         "seen by everyone on the reply.",
                         len(thread_messages) - len(visible))
            thread_messages = visible
            thread_text, thread_count = gmail_io.format_thread_context(
                thread_messages,
                exclude_msg_id=msg["gmail_msg_id"],
                self_addrs=selves,
                max_messages=cfg.max_thread_messages,
                max_chars=cfg.max_thread_chars,
            )
        except Exception:
            # Losing the thread costs context, not correctness — reply to
            # the message on its own rather than failing it.
            log.warning("Could not read the rest of thread %s.",
                        msg["thread_id"], exc_info=True)
    if thread_count:
        log.info("Thread context: %d earlier message(s), %d chars.",
                 thread_count, len(thread_text))

    # Recent mail on other threads that everyone on this reply has already
    # seen — theirs to discuss — put in front of the writer, so it doesn't
    # have to think to search for it, and doesn't mistake it for private.
    shared_text = ""
    if getattr(store, "supports_audience", False):
        try:
            shared_text = shared_context(store, recipients, msg["thread_id"])
        except Exception:
            log.warning("Could not gather shared recent mail.", exc_info=True)

    # The un-folded exchanges are a second rendering of the same recent
    # history. Where the thread already carries a message verbatim, the
    # summary copy is pure duplication: paid for twice, and two slightly
    # different versions of one past for the model to reconcile.
    thread_ids = {m.get("id", "") for m in thread_messages} - {""}
    already_shown = redundant_pending_ids(
        memory.pending_rows(store), thread_ids, thread_text
    )
    pending = memory.pending_text(store, exclude_msg_ids=already_shown)
    if already_shown:
        log.info("Dropped %d pending exchange(s) already present in the thread.",
                 len(already_shown))

    # The sender's own quotation is usually the same conversation again, so
    # it is only worth its tokens when it holds something the thread didn't
    # — a forward from another mailbox, most often.
    quoted_text = ""
    if gmail_io.quoted_adds_context(msg["quoted"], thread_text):
        quoted_text = msg["quoted"]
        log.info("Quoted text below the message adds %d chars the thread "
                 "doesn't have.", len(quoted_text))

    attachment_blocks, uploaded_ids, stored_paths = claude_api.build_attachment_blocks(
        gmail, cfg, msg, claude, trusted
    )
    if msg["attachment_refs"]:
        log.info("%d attachment(s) on this message.", len(msg["attachment_refs"]))
    # Recorded on the queue row as soon as the files are down, rather than
    # with the outgoing ones at the end: if this run dies before the reply
    # goes out, the sheet should still say where the incoming files went.
    if stored_paths and row:
        store.set_attachments(row, stored_paths)

    # --- generate -------------------------------------------------------
    # What is still open on this thread, for the writer (ledger.py).
    ledger_text = ""
    if ledger.enabled() and getattr(store, "responses_id", None):
        try:
            ledger_text = ledger.render(ledger.open_for(store, msg["thread_id"], recipients))
        except Exception:
            log.warning("Could not read the ledger.", exc_info=True)
    try:
        reply_text, stats = claude_api.generate_reply(
            claude, cfg, system_prompt, msg,
            # A person's memory file and unfolded exchanges are theirs;
            # with anyone else on the reply they stay out of view.
            memory_body="" if others else memory.body,
            pending_text="" if others else pending,
            recipients=recipients,
            addressed=_addressed(raw_message, selves),
            ledger_text=ledger_text,
            shared_text=shared_text,
            store=store,
            trusted=trusted,
            scope_all_history=scope_all_history,
            attachment_blocks=attachment_blocks,
            local_runner=local_runner,
            self_code_runner=self_code_runner,
            claude_code_runner=claude_code_runner,
            agenda_owner=agenda_owner,
            thread_text=thread_text,
            quoted_text=quoted_text,
        )
    finally:
        claude_api.delete_uploaded_files(claude, uploaded_ids)

    if stats["server_tools"]:
        log.info("Claude made %d server tool call(s).", stats["server_tools"])
    if stats["history_searches"]:
        log.info("Claude searched message history %d time(s).", stats["history_searches"])
    if stats.get("attachments"):
        log.info("Claude used the stored-attachment tools %d time(s).",
                 stats["attachments"])
    if stats.get("local_tools"):
        log.warning("Claude made %d local tool call(s) on this machine.",
                    stats["local_tools"])
    if stats.get("reminders"):
        log.info("Scheduled %d reminder(s) for %s.", stats["reminders"], sender)
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

    if stats.get("claude_code"):
        log.warning("Claude Code ran %d session(s) over the harness.",
                    stats["claude_code"])

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

    clean_body, out_attachments, warnings = claude_api.extract_outgoing_attachments(
        reply_text, cfg.max_outgoing_attachment_bytes
    )
    for warning in warnings:
        log.warning("%s", warning)
    if out_attachments:
        log.info("Reply carries %d generated attachment(s): %s",
                 len(out_attachments), ", ".join(n for n, _ in out_attachments))

    # --- the pre-send check (consult.py) --------------------------------
    # Only when there is something the recipients may not see: the
    # composer was never shown it, so this is the backstop. One rewrite,
    # then the message is left unanswered rather than sent with a leak.
    # Not for a reply to the operator alone: they can read the whole
    # mailbox, so nothing in it can leak to them, and a check could only
    # get in the way.
    operator_only = all(terms_gate.is_operator(r) for r in recipients)
    private = (consult.candidates(store, recipients)
               if consult_on and not operator_only else [])
    if private:
        try:
            # What the recipients have already seen, so the check can tell
            # a leak from someone being told back what they themselves said.
            seen = "\n\n".join(p for p in (
                f"THEIR MESSAGE ({msg.get('from_addr', '')}):\n{msg.get('body', '')}",
                quoted_text and f"QUOTED IN IT:\n{quoted_text}",
                thread_text and f"THE THREAD:\n{thread_text}",
                (not others and memory.body) and f"WHAT THEY TOLD INDI BEFORE:\n{memory.body}",
                (not others and pending) and f"RECENT EXCHANGES WITH THEM:\n{pending}",
                ledger_text and f"OPEN ON THE THREAD:\n{ledger_text}",
                shared_text and f"OTHER MAIL THEY HAVE ALL SEEN:\n{shared_text}",
                stats.get("tool_outputs") and (
                    "WHAT INDI LOOKED UP WHILE WRITING (history searches already "
                    "limited to these recipients, its own source code, files, the "
                    "web — none of it private to anyone else):\n"
                    + "\n\n".join(stats["tool_outputs"])),
            ) if p)
            checked, note = consult.vet(claude, cfg, clean_body, recipients, private, seen)
            for name, content in out_attachments:
                if checked is None:
                    break
                verdict, why_att = consult.check(claude, cfg, content, recipients,
                                                 private, seen)
                if verdict != consult.PASS:
                    checked, note = None, f"attachment {name}: {why_att}"
        except consult.CheckUnavailable as exc:
            # Not a verdict: nothing is sent, and the message is left
            # unread to be tried again — up to MAX_CHECK_ATTEMPTS runs,
            # because each try composes the whole reply again.
            return _check_unavailable(cfg, gmail, store, msg, row, str(exc))
        if checked is None:
            outcome = f"blocked by the pre-send check ({note[:200]})"
            log.warning("Not sending to %s: %s", ", ".join(sorted(recipients)), note)
            if cfg.dry_run:
                return outcome
            if row:
                store.set_status(row, sheets_queue.STATUS_SKIPPED, outcome)
            mark_as_read(gmail, msg["gmail_msg_id"])
            # The same words as any other withheld reply: nothing in the
            # note may hint that something private is behind it.
            return outcome + "; " + send_withheld_notice(cfg, gmail, store, msg,
                                                         NOTICE_UNABLE)
        clean_body = checked

    # Re-papering: the sender accepted an older version, so the reply is
    # still theirs but the current terms travel with it. Added before the
    # signature goes on, so the note reads as part of the message rather
    # than as something bolted on after the sign-off.
    if gate.notice:
        clean_body = clean_body.rstrip() + "\n\n" + gate.notice

    signed_body = ensure_signature(clean_body)
    if signed_body != clean_body:
        log.info("Signature was missing from the reply; appended it.")
        clean_body = signed_body

    safe_attachments = [
        (gmail_io.sanitise_filename(name), content)
        for name, content in [*out_attachments, *gate.attachments]
    ]

    # Quote the message being answered below the reply, as Gmail does.
    # Safe to show everyone the reply goes to: they were all on it.
    outgoing_body = clean_body
    if quoting_enabled():
        quote = gmail_io.gmail_style_quote(msg, gmail_reads._setting("INDI_TIMEZONE") or "UTC")
        if quote:
            outgoing_body = clean_body.rstrip() + "\n\n" + quote

    if cfg.dry_run:
        print("\n" + "=" * 70)
        print(f"TO:      {msg['reply_to_raw']}")
        if others:
            print(f"CC:      {', '.join(others)}")
        print(f"SUBJECT: Re: {msg['subject']}")
        print("-" * 70)
        print(outgoing_body)
        for name, content in safe_attachments:
            print(f"\n[attachment {name}: {len(content)} chars]")
        print("=" * 70 + "\n")
        return "dry run (nothing written)"

    mime = gmail_io.build_reply_mime(
        to_addr=msg["reply_to_raw"],
        subject=msg["subject"],
        body=outgoing_body,
        in_reply_to=msg["message_id"],
        references=msg["references"],
        attachments=safe_attachments,
        cc=others,
    )
    mime["threadId"] = msg["thread_id"]

    if cfg.send_automatically:
        result = gmail.users().messages().send(userId="me", body=mime).execute()
        outcome = "sent"
        reply_id = (result or {}).get("id", "")
    else:
        result = gmail.users().drafts().create(userId="me", body={"message": mime}).execute()
        outcome = "drafted"
        # A draft's own id is not a message id; the message inside it is
        # what carries labels and turns up in the thread.
        reply_id = ((result or {}).get("message") or {}).get("id", "")

    # --- after the mail has gone ----------------------------------------
    # Everything below this line runs with a reply already in the world,
    # and that changes what an exception means. Up to here, raising is the
    # right thing: the caller marks the row failed, the message stays
    # unread, and the next run tries again. After the send, a raise means
    # the next run finds an unread message with a non-terminal row and
    # replies to it a second time — once per run, forever. That happened,
    # and it happened because an AttributeError in the attachment
    # bookkeeping below sat between the send and the two lines that stop
    # the message coming back.
    #
    # So those two go first, and everything after them is bookkeeping that
    # is logged and swallowed. A missing record is a blemish; a duplicate
    # reply to a real person is not.
    #
    # The two guards back each other up, which is why neither of them is
    # allowed to raise either: a terminal row means the next run
    # recognises the message and marks it read itself, and a message
    # already marked read is never fetched again even if its row was left
    # saying `queued`. Getting back into the loop now takes two
    # independent failures rather than one.
    try:
        store.set_status(row, sheets_queue.STATUS_REPLIED, outcome)
    except Exception:
        log.warning("Reply %s but the queue row could not be updated.",
                    outcome, exc_info=True)
    try:
        mark_as_read(gmail, msg["gmail_msg_id"])
    except Exception:
        log.warning("Reply %s but the message could not be marked read.",
                    outcome, exc_info=True)
    log.info("Reply %s.", outcome)

    # The response row is written only now, after the mail actually went
    # somewhere, so the sheets never claim a reply that doesn't exist.
    try:
        store.append_response(msg["gmail_msg_id"], sender, clean_body,
                              reply_id=reply_id)
    except Exception:
        log.warning("Could not record the response for %s.", sender,
                    exc_info=True)

    # The outgoing files, kept for the same reason the incoming ones are:
    # "the document you sent me" is a question about a real file, and
    # until now the only copy was in the recipient's mailbox. Stored after
    # the send, so the store never holds something that never went out.
    try:
        for name, content in safe_attachments:
            rel = attachments.store_text(
                cfg, msg["gmail_msg_id"], name, content, attachments.DIRECTION_OUT,
                sender=sender, subject=msg["subject"],
            )
            if rel:
                stored_paths.append(rel)
        if stored_paths:
            store.set_attachments(row, stored_paths)
    except Exception:
        log.warning("Could not store the outgoing attachment copies.",
                    exc_info=True)

    # Only now is it true that the new terms were sent. Recording it any
    # earlier would mean a reply that failed to go out still counted as
    # having delivered them.
    if gate.commit is not None:
        try:
            gate.commit()
        except Exception:
            log.warning("Could not record that the terms went out to %s.",
                        sender, exc_info=True)

    try:
        memory_file.consolidate(claude, cfg, drive, store, sender)
    except Exception:
        log.warning("Could not consolidate memory for %s.", sender,
                    exc_info=True)

    # The ledger's turn: account for what was open, record what's new;
    # settled entries cancel follow-ups about them, dated commitments get
    # one (ledger.py, followups.py). Best effort, like everything here.
    if ledger.enabled() and getattr(store, "responses_id", None):
        try:
            changes = ledger.keep(
                claude, store, msg, clean_body, recipients, reply_id,
                on_closed=lambda e: followups.cancel_about(
                    store, e["entry_id"], f"ledger entry {e['status']}"),
                on_commitment=lambda e: followups.schedule_for_commitment(store, e, msg))
            if changes:
                log.info("Ledger: %s", "; ".join(changes))
        except Exception:
            log.warning("The ledger step failed.", exc_info=True)

    # The broker's turn: now that the reply has gone, is anything the
    # recipients couldn't see worth asking its owner about? (consult.py)
    if consult_on and private:
        try:
            for asked in consult.after_reply(claude, cfg, gmail, store, people,
                                             msg, recipients):
                log.info("Consultation: %s", asked)
        except Exception:
            log.warning("The broker step failed.", exc_info=True)
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
    # Counts this run's ERROR lines for the end-of-run alert (alerts.py).
    monitor = alerts.RunMonitor()
    logging.getLogger().addHandler(monitor)
    run_mode = "heartbeat" if args.heartbeat else "mail"
    run_failures = 0

    lock = acquire_lock(cfg.lock_path)
    store = None
    people = None
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
            # Start-of-run notices like this one go to DEBUG: written only
            # with --verbose, so the log isn't the same lines every 5 minutes.
            log.debug("INDI_SEND_AUTOMATICALLY is on: replies go out without review.")
        problems = cfg.validate_local_tools() + cfg.validate_claude_code()
        if problems:
            for problem in problems:
                log.error("%s", problem)
            raise SystemExit(
                "Refusing to start with an unsafe local-tools configuration."
            )
        if cfg.enable_claude_code:
            log.debug(
                "CLAUDE CODE SESSIONS ARE ON for %s. These senders can have "
                "Indi run an unrestricted coding agent over this harness — "
                "every file, including the credentials, the permission checks "
                "and the test suite. Authentication is mandatory for them and "
                "each session is capped at $%.2f and %ss.",
                ", ".join(cfg.claude_code_senders), cfg.claude_code_max_usd,
                cfg.claude_code_timeout,
            )
        if cfg.enable_local_files or cfg.enable_local_exec:
            log.debug(
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

        store = build_store(cfg, gmail, drive, sheets)
        store.ensure_spreadsheets()
        if hasattr(store, "selves"):
            store.selves = set(selves)

        # Who has accepted the terms. Local, and read-only under
        # --dry-run so a rehearsal cannot record an acceptance that never
        # actually happened.
        people = correspondents.CorrespondentStore(read_only=cfg.dry_run)
        current_terms = terms.load()
        if current_terms is None:
            log.error(
                "No Terms of Service could be loaded from %s. Correspondents "
                "who have not already accepted will get no reply at all until "
                "a version file is put there.", terms.TERMS_DIR,
            )
        else:
            log.debug("Terms of service v%s in force (%s).",
                     current_terms.version, current_terms.path.name)

        if args.heartbeat:
            if not cfg.enable_outbound:
                log.info("Outbound initiative is disabled (INDI_ENABLE_OUTBOUND).")
                return 0
            outcome = outbound.run_heartbeat(
                claude, cfg, gmail, store, system_prompt, selves, people
            )
            log.info("Heartbeat: %s", outcome)
            return 0

        # Due reminders go out on every mail check, not only on the hourly
        # heartbeat. Mail runs every few minutes, so this is the difference
        # between "9am" meaning 9am and it meaning some time before 10 —
        # and it means a reminder still arrives on a day when the heartbeat
        # timer is off or its model call fails. Delivery is idempotent: a
        # row is marked sent the moment it goes.
        if cfg.enable_outbound:
            try:
                for outcome in outbound.deliver_due_reminders(cfg, gmail, store,
                                                              people):
                    log.info("Reminder: %s", outcome)
            except Exception:
                log.error("Reminder delivery failed", exc_info=True)

        # Consultations nobody answered in time count as no.
        if not cfg.dry_run and consult.enabled(cfg) \
                and getattr(store, "supports_audience", False):
            try:
                expired = consult.expire(store)
                if expired:
                    log.info("%d consultation(s) expired unanswered.", expired)
                for line in consult.retry_followups(claude, cfg, gmail, store, selves):
                    log.info("Consultation: %s", line)
            except Exception:
                log.warning("Could not expire consultations.", exc_info=True)

        # Replies that were waiting for someone to accept the terms, now
        # ready: everyone answered, or 72 hours passed (reply_all.py).
        if not args.message_id and not cfg.dry_run:
            try:
                for row, kept in reply_all.due(store, people):
                    dropped = [a for a in (row.get("recipients") or "").split(", ")
                               if a and a not in kept]
                    reply_all.mark_released(store, row, kept, dropped)
                    detail = get_detail(gmail, row["gmail_msg_id"])
                    outcome = process_message(gmail, drive, claude, cfg, store, detail,
                                              selves, system_prompt, people,
                                              released_to=kept)
                    log.info("Released after the terms wait: %s", outcome)
            except Exception:
                log.error("Releasing waiting replies failed", exc_info=True)

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
                    gmail, drive, claude, cfg, store, detail, selves,
                    system_prompt, people,
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
        run_failures = failures
        return 1 if failures else 0
    finally:
        # Tell the phone, and check in with the dead-man's switch. Never in
        # a dry run; never able to raise.
        crashed = sys.exc_info()[1]
        if isinstance(crashed, SystemExit):
            crashed = None if crashed.code in (0, None) else crashed
        if not cfg.dry_run and not getattr(args, "message_id", None):
            try:
                alerts.after_run(run_mode, monitor, run_failures, crashed)
                if crashed is None:
                    alerts.ping("INDI_HEALTHCHECK_URL" if run_mode == "mail"
                                else "INDI_HEALTHCHECK_HEARTBEAT_URL")
            except Exception:
                log.warning("Alerting failed.", exc_info=True)
        # In `finally` so a run that raised still records what it spent
        # before dying — those calls were billed either way. Covers the
        # heartbeat and the no-mail path too, which return early.
        record_spend(store, cfg)
        if people is not None:
            people.close()
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
