"""
reply_all.py

Phase 2, second piece: replies go to everyone on the thread, and nobody
gets one before they have accepted the terms. See the design doc "Indi
Phase 2 — Revised Design: Shared Memory with Consent".

WHO A REPLY GOES TO
-------------------
With INDI_REPLY_ALL on (and INDI_GMAIL_READS, which the audience rule
needs), a reply goes to the sender (their Reply-To if they set one) on
To, and to everyone else on the incoming message's To and Cc on Cc —
less Indi, anyone who has declined the terms, and anything that looks
like a no-reply address. Two exceptions send it to the sender alone:

  * Indi was not on the message's To or Cc — a Bcc, a list, a forward.
    The visible recipients don't know Indi has it, so answering them all
    would tell them (audience.py treats the message as private too);
  * more than MAX_RECIPIENTS people would get it, which is a mailing
    list in all but name.

With the setting off, every reply goes to the sender alone, as before.

NOBODY IS WRITTEN TO BEFORE THEY'VE ACCEPTED
--------------------------------------------
When someone the reply would go to hasn't accepted the terms, Indi:

  1. sends each of them the terms, once (at most twice over time, the
     same limit the gate uses), in a message of their own;
  2. records the message in the `Awaiting Terms` table and does not
     compose anything yet;
  3. on each later run, releases it when nobody is still undecided —
     everyone has accepted or declined — or when WAIT_HOURS have passed,
     and then replies to the sender and those who accepted.

Composing only at release, for the people who will actually get it, means
the reply never has to be rewritten for a smaller group and is never
written with more in view than its final recipients may see. Declining
drops a person; silence past the deadline drops them too.

The waiting message is left with a non-terminal status, so the normal
pipeline handles it when it is released, with every guard run again.

This module decides who receives mail, so it is protected from
self-modification.
"""

from __future__ import annotations

import datetime
import clock
import logging
from email.utils import getaddresses

import audience
import correspondents
import gmail_io
import gmail_reads
import terms
import terms_gate

log = logging.getLogger("indi.reply_all")

WAIT_HOURS = 72
MAX_RECIPIENTS = 10
MAX_TERMS_SENDS = 2

WAITING_TAB = "Awaiting Terms"
WAITING_HEADERS = ["gmail_msg_id", "thread_id", "sender", "recipients",
                   "created_at", "status", "released_at", "note"]
WAITING, RELEASED = "waiting", "released"

PAPER_SUBJECT = "Terms of Service for Indi: you were copied on \"{subject}\""
PAPER_BODY = (
    "Hello,\n\n"
    "{sender} has included Indi, an AI correspondent, in an email "
    "conversation you are part of (\"{subject}\").\n\n"
    "Before Indi sends anything to you, it needs you to accept its Terms of "
    "Service, attached. To accept, reply to this message with the words "
    "I AGREE. To decline, reply DECLINE and Indi will leave you out of its "
    "replies.\n\n"
    "If you don't answer within {hours} hours, Indi will reply to the others "
    "without you. Nothing you have written is read or kept until you "
    "accept.\n\n"
    "--- Indi\n"
)


def enabled(cfg=None) -> bool:
    configured = getattr(cfg, "reply_all", None)
    if isinstance(configured, bool):
        return configured
    return gmail_reads._setting("INDI_REPLY_ALL").lower() in ("1", "true", "yes", "on")


def _now() -> datetime.datetime:
    return clock.now()


def _looks_automated(addr: str) -> bool:
    return bool(gmail_io._NOREPLY_PATTERN.search(addr.split("@", 1)[0]))


def recipients_for(raw_message: dict, msg: dict, selves, people,
                   reply_all: bool) -> tuple[str, list[str]]:
    """(the sender, everyone else) a reply to this message goes to, before
    anyone is dropped for not having accepted the terms."""
    sender = (msg.get("reply_to_addr") or msg.get("from_addr") or "").lower()
    if not reply_all or not sender:
        return sender, []
    selves = {s.lower() for s in selves or ()}
    hdrs = gmail_reads._headers(raw_message)
    visible = audience.addrs(hdrs.get("to", "")) | audience.addrs(hdrs.get("cc", ""))
    if not selves or not (visible & selves):
        return sender, []                      # Bcc, list or forward
    others = []
    for addr in sorted(visible - selves - {sender, msg.get("from_addr", "").lower()}):
        if _looks_automated(addr):
            continue
        if people is not None and people.get(addr).status == correspondents.STATUS_DECLINED:
            continue
        others.append(addr)
    if len(others) + 1 > MAX_RECIPIENTS:
        log.info("Reply would go to %d people; answering the sender only.",
                 len(others) + 1)
        return sender, []
    return sender, others


def accepted(addr: str, people) -> bool:
    return terms_gate.is_operator(addr) or people.get(addr).has_accepted


def undecided(addrs, people) -> list[str]:
    """Those who have neither accepted nor declined."""
    return [a for a in addrs
            if not accepted(a, people)
            and people.get(a).status != correspondents.STATUS_DECLINED]


# -- papering -------------------------------------------------------------

def paper(cfg, gmail, people, addr: str, msg: dict) -> str:
    """Send one person the terms, as a message of their own."""
    record = people.get(addr)
    if record.terms_sent_count >= MAX_TERMS_SENDS:
        return "terms already sent twice"
    current = terms.load()
    if current is None:
        log.error("No terms loaded; cannot paper %s.", addr)
        return "no terms to send"
    subject = PAPER_SUBJECT.format(subject=(msg.get("subject") or "")[:80])
    body = PAPER_BODY.format(sender=msg.get("from_raw") or msg.get("from_addr", ""),
                             subject=(msg.get("subject") or "")[:80], hours=WAIT_HOURS)
    if cfg.dry_run:
        log.info("[dry-run] Would send the terms to %s.", addr)
        return "dry run"
    mime = gmail_io.build_reply_mime(
        to_addr=addr, subject=subject, body=body, in_reply_to="", references="",
        attachments=[(gmail_io.sanitise_filename(current.filename), current.text)])
    mime = terms_gate._with_subject(mime, subject)
    if cfg.send_automatically:
        gmail.users().messages().send(userId="me", body=mime).execute()
    else:
        gmail.users().drafts().create(userId="me", body={"message": mime}).execute()
    people.record_terms_sent(addr, current.version)
    return "terms sent"


# -- the waiting table ----------------------------------------------------

def _rows(store) -> list[dict]:
    store._ensure_tab(store.responses_id, WAITING_TAB, WAITING_HEADERS)
    return store._load(store.responses_id, WAITING_TAB, declared=WAITING_HEADERS)


def waiting_for(store, gmail_msg_id: str) -> dict | None:
    for row in _rows(store):
        if row.get("gmail_msg_id") == gmail_msg_id and row.get("status") == WAITING:
            return row
    return None


def hold(store, msg: dict, sender: str, others: list[str]) -> None:
    store._append(store.responses_id, WAITING_TAB, WAITING_HEADERS, {
        "gmail_msg_id": msg["gmail_msg_id"], "thread_id": msg.get("thread_id", ""),
        "sender": sender, "recipients": ", ".join(others),
        "created_at": _now().isoformat(), "status": WAITING,
        "released_at": "", "note": "",
    })


def _set(store, row: dict, status: str, note: str) -> None:
    from sheets_queue import _col_letter
    row.update(status=status, released_at=_now().isoformat(), note=note[:300])
    n = row.get("_row", -1)
    if n <= 0:
        return
    first = _col_letter(WAITING_HEADERS.index("status"))
    last = _col_letter(WAITING_HEADERS.index("note"))
    store.sheets.spreadsheets().values().update(
        spreadsheetId=store.responses_id, range=f"{WAITING_TAB}!{first}{n}:{last}{n}",
        valueInputOption="RAW",
        body={"values": [[status, row["released_at"], row["note"]]]},
    ).execute()


def due(store, people, now: datetime.datetime | None = None) -> list[tuple[dict, list[str]]]:
    """Waiting messages ready to answer, each with who now gets the reply
    besides the sender: those who accepted."""
    now = now or _now()
    out = []
    for row in _rows(store):
        if row.get("status") != WAITING:
            continue
        others = [a.strip() for a in (row.get("recipients") or "").split(",") if a.strip()]
        try:
            since = datetime.datetime.fromisoformat(row.get("created_at", ""))
        except ValueError:
            since = now
        expired = now - since >= datetime.timedelta(hours=WAIT_HOURS)
        if undecided(others, people) and not expired:
            continue
        out.append((row, [a for a in others if accepted(a, people)]))
    return out


def mark_released(store, row: dict, kept: list[str], dropped: list[str]) -> None:
    note = f"to {len(kept) + 1}" + (f"; dropped {', '.join(dropped)}" if dropped else "")
    _set(store, row, RELEASED, note)
