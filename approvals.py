"""
approvals.py

Nobody new hears from Indi until the operator says so. When someone Indi
has never dealt with writes to it — or is copied in on a thread with it —
Indi does not send them the terms. It emails the operator instead:

    [approve] New correspondent: dave@example.com

naming the address, the name on it, the subject, and how they arrived
(wrote directly, or copied by whom). The operator replies in that thread:

    APPROVE  →  the terms go out, as they would have; the person can then
                accept them and correspond as usual.
    DENY     →  nothing is ever sent to them; their mail is set aside, and
                Indi leaves them out of replies to threads they're on.

Anything else gets one short "APPROVE or DENY?" back. The reply must come
from the operator's own address and pass authentication. Until the
operator answers, nothing is sent to the newcomer; a reply waiting on a
copied-in newcomer waits as it would for their acceptance (reply_all.py),
and after 72 hours goes out without them.

The operator email carries headers only — address, name, subject — never
the newcomer's words: the terms promise nothing they write is read or kept
until they accept.

Decisions are kept in data/approvals.json (machine-local, beside the terms
and the correspondents register; Indi can't write there). On unless
INDI_APPROVE_NEW_CORRESPONDENTS=false, and never in a staging copy, whose
test people would otherwise wait on the real operator. The operator is never
held up by this. Protected from self-modification.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import clock
import gmail_io
import gmail_reads
import terms_gate

log = logging.getLogger("indi.approvals")

FILE = Path(__file__).resolve().parent / "data" / "approvals.json"
PENDING, APPROVED, DENIED = "pending", "approved", "denied"
SUBJECT = "[approve] New correspondent: {addr}"
APPROVE_WORDS = ("approve", "approved", "yes", "ok", "okay", "y")
DENY_WORDS = ("deny", "denied", "no", "reject", "n")
ASK_AGAIN = "Please reply APPROVE to send them the terms, or DENY to ignore them.\n\n--- Indi"


def enabled() -> bool:
    if clock.staging():
        return False
    return (gmail_reads._setting("INDI_APPROVE_NEW_CORRESPONDENTS") or "true").lower() \
        not in ("0", "false", "no", "off")


def _load(path: Path | None = None) -> dict:
    try:
        return json.loads((path or FILE).read_text())
    except FileNotFoundError:
        return {}
    except Exception:
        log.error("Unreadable %s; treating everyone as unapproved.", path or FILE)
        return {}


def _save(data: dict, path: Path | None = None) -> None:
    path = path or FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def status(addr: str, path: Path | None = None) -> str:
    return (_load(path).get((addr or "").lower()) or {}).get("status", "")


def needs_approval(addr: str, path: Path | None = None) -> bool:
    """True when sending this person the terms must wait for the operator."""
    if not enabled() or terms_gate.is_operator(addr):
        return False
    return status(addr, path) != APPROVED


def denied(addr: str, path: Path | None = None) -> bool:
    return enabled() and status(addr, path) == DENIED


def request(cfg, gmail, addr: str, kind: str, held: dict, by: str = "",
            path: Path | None = None) -> str:
    """Ask the operator about `addr`, once. `held` is the message to come
    back to on approval: theirs (kind "direct") or the thread's ("copied")."""
    addr = addr.lower()
    data = _load(path)
    entry = data.get(addr) or {}
    if entry.get("status") == DENIED:
        return "denied by the operator"
    if entry.get("status") == PENDING:
        return "awaiting the operator's approval"
    name = (held.get("from_raw") or "") if kind == "direct" else ""
    how = ("wrote to Indi directly" if kind == "direct"
           else f"was copied in by {by or 'someone'} on a thread with Indi")
    body = (f"{name or addr} ({addr}) {how}.\n"
            f"Subject: {held.get('subject', '')}\n\n"
            "They haven't been sent the terms. Reply APPROVE to send them, or DENY to "
            "ignore them; nothing goes to them until you answer. (Their message itself "
            "isn't included: nothing they write is read until they accept.)\n\n--- Indi")
    subject = SUBJECT.format(addr=addr)
    if cfg.dry_run:
        log.info("[dry-run] Would ask the operator to approve %s.", addr)
        return "dry run: would ask the operator"
    mime = gmail_io.build_reply_mime(to_addr=terms_gate.OPERATOR_ADDRESS, subject=subject,
                                     body=body, in_reply_to="")
    mime = terms_gate._with_subject(mime, subject)
    sent = gmail.users().messages().send(userId="me", body=mime).execute() or {}
    data[addr] = {"status": PENDING, "kind": kind, "by": by,
                  "held_msg_id": held.get("gmail_msg_id", ""),
                  "thread_id": sent.get("threadId", ""),
                  "requested_at": clock.now_iso(), "decided_at": ""}
    _save(data, path)
    log.info("Asked the operator to approve %s (%s).", addr, kind)
    return "awaiting the operator's approval"


def match(msg: dict, path: Path | None = None) -> str:
    """The address whose approval request this message answers, if any."""
    thread = msg.get("thread_id", "")
    if not thread:
        return ""
    for addr, entry in _load(path).items():
        if entry.get("thread_id") == thread and entry.get("status") == PENDING:
            return addr
    return ""


def _word(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip().strip(".!").lower()
        if line:
            return line.split()[0].strip(".,!")
    return ""


def handle(cfg, gmail, people, msg: dict, addr: str, path: Path | None = None) -> str:
    """The operator's answer about `addr`."""
    sender = (msg.get("from_addr") or "").lower()
    if not terms_gate.is_operator(sender):
        return f"message in an approval thread not from the operator; ignored"
    authentic, why = gmail_io.sender_is_authenticated(msg)
    if not authentic:
        log.warning("Approval answer failed authentication (%s); ignored.", why)
        return f"approval answer failed authentication ({why}); ignored"
    word = _word(msg.get("body", ""))
    data = _load(path)
    entry = data.get(addr) or {}
    if word not in APPROVE_WORDS + DENY_WORDS:
        if not cfg.dry_run:
            terms_gate._send_fixed(cfg, gmail, msg, ASK_AGAIN)
        return f"approval for {addr}: unclear answer, asked again"
    if cfg.dry_run:
        return f"dry run: {addr} would be {'approved' if word in APPROVE_WORDS else 'denied'}"
    entry.update(status=APPROVED if word in APPROVE_WORDS else DENIED,
                 decided_at=clock.now_iso())
    data[addr] = entry
    _save(data, path)
    if entry["status"] == DENIED:
        log.info("The operator denied %s; nothing will be sent there.", addr)
        return f"{addr} denied by the operator"
    return f"{addr} approved by the operator; " + _send_terms(cfg, gmail, people, addr, entry)


def _send_terms(cfg, gmail, people, addr: str, entry: dict) -> str:
    try:
        raw = gmail.users().messages().get(userId="me", id=entry.get("held_msg_id", ""),
                                           format="full").execute()
    except Exception:
        return "the held message is gone; they'll get the terms if they write again"
    held = gmail_io.extract_message(raw, cfg.max_body_chars)
    if entry.get("kind") == "copied":
        import reply_all
        return reply_all.paper(cfg, gmail, people, addr, held)
    return terms_gate.send_first_terms(cfg, gmail, people, held)
