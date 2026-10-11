"""
address_links.py

One person, two addresses. Indi knows people by email address, so
someone who writes from work and from home has two partial memories and
can't see their own mail from the other address. Linking makes the second
address an alias of the first:

  1. From their main address — authenticated, alone on the reply, replied
     to at that address (memory_rights.eligible) — the person asks Indi to
     link another address. Indi emails that address a six-digit code.
  2. They reply to that email from the other address, with the code. The
     reply must pass authentication and arrive within 72 hours; three
     wrong codes cancel the request.
  3. Then: the other address's memory notes are merged into the main
     address's and its own copies deleted; from then on both addresses
     share one memory file. Each address is granted sight of the other's
     mail (audience.py grants, recorded so unlinking can withdraw them).
     The other address is recorded as having accepted the terms, at the
     version the main address accepted — it is the same person.

What is not merged, and the person is told: reminders, follow-ups and open
commitments stay with the address they were made from, newsletter
subscriptions are per address, and permissions other people gave to one
address are not extended to the other (their consent named an address).

The person can list their linked addresses and unlink one. Unlinking
withdraws the mutual grants; the merged notes stay with the main address.

A confirmation email is never sent to Indi's own address, the operator,
an address that has declined the terms or been turned away by the
operator, or an address already linked to someone. One code per address
per day, so this can't be used to pester a stranger.

State: data/address_links.json (aliases and pending requests; machine-
local, Indi can't write there). Protected from self-modification.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import secrets
from pathlib import Path

import clock
import correspondents
import gmail_io
import terms_gate

log = logging.getLogger("indi.address_links")

FILE = Path(__file__).resolve().parent / "data" / "address_links.json"
EXPIRY_HOURS = 72
MAX_TRIES = 3
MAX_PENDING_PER_PERSON = 3
SUBJECT = "Confirm this address for Indi Artificial"

LINK_TOOL = "link_my_other_address"
LIST_TOOL = "list_my_addresses"
UNLINK_TOOL = "unlink_my_address"
TOOL_NAMES = {LINK_TOOL, LIST_TOOL, UNLINK_TOOL}

_cache: dict | None = None


def _norm(addr: str) -> str:
    return (addr or "").strip().lower()


def _load(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or FILE).read_text())
    except FileNotFoundError:
        data = {}
    except Exception:
        log.error("Unreadable %s; treating every address as its own person.", path or FILE)
        data = {}
    data.setdefault("aliases", {})
    data.setdefault("pending", [])
    return data


def _save(data: dict, path: Path | None = None) -> None:
    global _cache
    path = path or FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)
    _cache = None


def reset_cache() -> None:
    global _cache
    _cache = None


def canonical(addr: str, path: Path | None = None) -> str:
    """The main address for `addr`: itself unless it is a linked alias."""
    global _cache
    addr = _norm(addr)
    if not addr:
        return addr
    if path is not None:
        return _load(path)["aliases"].get(addr, {}).get("primary", addr)
    if _cache is None:
        _cache = _load()["aliases"]
    return _cache.get(addr, {}).get("primary", addr)


def aliases_of(primary: str, path: Path | None = None) -> list[str]:
    primary = _norm(primary)
    return sorted(a for a, v in _load(path)["aliases"].items()
                  if v.get("primary") == primary)


# ---------------------------------------------------------------------------
# TOOLS
# ---------------------------------------------------------------------------

def schemas(sender: str) -> list[dict]:
    return [
        {
            "name": LINK_TOOL,
            "description": (
                f"Start linking another email address of {sender}'s, so you treat "
                "both as the same person: one memory, and each sees the other's "
                "mail. Only when they ask, giving an address they say is theirs. "
                "A code is emailed to that address; nothing is linked until they "
                "send it back from there. Tell them so."),
            "input_schema": {"type": "object", "properties": {
                "address": {"type": "string",
                            "description": "Their other email address."}},
                "required": ["address"]},
        },
        {
            "name": LIST_TOOL,
            "description": f"List the other addresses linked to {sender}, and any waiting.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": UNLINK_TOOL,
            "description": (
                f"Unlink one of {sender}'s linked addresses, when they ask. The "
                "notes already merged stay with this address; the two stop "
                "seeing each other's mail."),
            "input_schema": {"type": "object", "properties": {
                "address": {"type": "string", "description": "The address to unlink."}},
                "required": ["address"]},
        },
    ]


class Runner:
    tool_names = TOOL_NAMES

    def __init__(self, cfg, gmail, store, people, sender: str, selves=(),
                 path: Path | None = None):
        self.cfg, self.gmail, self.store, self.people = cfg, gmail, store, people
        self.sender = _norm(sender)
        self.selves = {_norm(s) for s in selves or ()}
        self.path = path

    def schemas(self) -> list[dict]:
        return schemas(self.sender)

    def handle(self, name: str, tool_input: dict) -> tuple[str, bool]:
        tool_input = tool_input or {}
        try:
            if name == LINK_TOOL:
                return request_link(self.cfg, self.gmail, self.people, self.sender,
                                    tool_input.get("address", ""), self.selves, self.path)
            if name == LIST_TOOL:
                return list_links(self.sender, self.path), False
            if name == UNLINK_TOOL:
                return unlink(self.cfg, self.store, self.sender,
                              tool_input.get("address", ""), self.path)
        except Exception:
            log.error("Address-link request %s for %s failed.", name, self.sender,
                      exc_info=True)
            return "That didn't work because of an error on my side. Please try again later.", True
        return f"Unknown tool {name}.", True


def _expired(entry: dict, now=None) -> bool:
    try:
        when = datetime.datetime.fromisoformat(entry.get("requested_at", ""))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return (now or clock.now()) - when > datetime.timedelta(hours=EXPIRY_HOURS)


def request_link(cfg, gmail, people, primary: str, other: str, selves=(),
                 path: Path | None = None) -> tuple[str, bool]:
    primary, other = canonical(primary, path), _norm(other)
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", other or ""):
        return "That doesn't look like an email address.", True
    if other == primary or other in {_norm(s) for s in selves or ()}:
        return "That address can't be linked.", True
    if terms_gate.is_operator(other):
        return "That address can't be linked.", True
    data = _load(path)
    if other in data["aliases"]:
        return ("That address is already linked to someone. If it's yours, unlink it "
                "there first."), True
    if other in {v.get("primary") for v in data["aliases"].values()} or \
            any(p["primary"] == other for p in data["pending"] if not _expired(p)):
        return "That address already has addresses linked to it; link the other way round.", True
    import approvals
    record = people.get(other) if people is not None else None
    if (record is not None and record.status == correspondents.STATUS_DECLINED) or approvals.denied(other):
        return ("That address has asked not to hear from me, so I won't send it a "
                "code."), True
    data["pending"] = [p for p in data["pending"] if not _expired(p)]
    if any(p["other"] == other for p in data["pending"]):
        return ("A code was already sent to that address in the last few days; reply "
                "to that email from there."), True
    if sum(1 for p in data["pending"] if p["primary"] == primary) >= MAX_PENDING_PER_PERSON:
        return "Several links are already waiting; finish or let those expire first.", True

    code = f"{secrets.randbelow(1_000_000):06d}"
    body = (f"Someone writing to Indi Artificial from {primary} says this address is "
            "also theirs, and asked to link the two, so Indi treats them as one "
            "person: one memory, and each address can see mail from the other.\n\n"
            f"If that's you, reply to this message from this address with the code:\n\n"
            f"    {code}\n\n"
            f"The code works for {EXPIRY_HOURS} hours. If it isn't you, ignore this "
            "message; nothing happens without the code, and you won't hear from Indi "
            "again because of it.\n\n--- Indi")
    if cfg.dry_run:
        return f"dry run: would email a code to {other}", False
    mime = gmail_io.build_reply_mime(to_addr=other, subject=SUBJECT, body=body,
                                     in_reply_to="")
    mime = terms_gate._with_subject(mime, SUBJECT)
    sent = gmail.users().messages().send(userId="me", body=mime).execute() or {}
    data["pending"].append({"primary": primary, "other": other, "code": code,
                            "thread_id": sent.get("threadId", ""), "tries": 0,
                            "requested_at": clock.now().isoformat()})
    _save(data, path)
    log.info("Sent an address-link code to %s for %s.", other, primary)
    return (f"I've emailed a code to {other}. Reply to that email from {other} with "
            f"the code within {EXPIRY_HOURS} hours, and the two addresses will be "
            "linked."), False


def list_links(primary: str, path: Path | None = None) -> str:
    primary = canonical(primary, path)
    data = _load(path)
    linked = sorted(a for a, v in data["aliases"].items() if v.get("primary") == primary)
    waiting = sorted(p["other"] for p in data["pending"]
                     if p["primary"] == primary and not _expired(p))
    if not linked and not waiting:
        return f"No other addresses are linked to {primary}."
    out = []
    if linked:
        out.append("Linked: " + ", ".join(linked))
    if waiting:
        out.append("Waiting for the code: " + ", ".join(waiting))
    return "\n".join(out)


def unlink(cfg, store, primary: str, other: str, path: Path | None = None) -> tuple[str, bool]:
    primary = canonical(primary, path)
    other = _norm(other)
    data = _load(path)
    entry = data["aliases"].get(other)
    if not entry or entry.get("primary") != primary:
        return f"{other} isn't linked to {primary}.", True
    if cfg.dry_run:
        return f"dry run: would unlink {other}", False
    withdrawn = _revoke(store, entry.get("grants", []))
    del data["aliases"][other]
    _save(data, path)
    log.info("Unlinked %s from %s.", other, primary)
    return (f"Unlinked {other}. My notes stay with {primary}; the two addresses no "
            f"longer see each other's mail ({withdrawn} permission(s) withdrawn)."), False


def _revoke(store, grant_ids) -> int:
    if store is None or not grant_ids:
        return 0
    try:
        import audience
        book = audience.GrantBook(store)
        done = sum(1 for g in grant_ids if book.revoke(g))
        if hasattr(store, "_grants"):
            store._grants = None
        return done
    except Exception:
        log.warning("Could not withdraw link grants %s.", grant_ids, exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# THE CODE COMING BACK
# ---------------------------------------------------------------------------

def match(msg: dict, path: Path | None = None) -> dict | None:
    """The pending link this message answers, if it is in a link thread."""
    thread = msg.get("thread_id", "")
    if not thread:
        return None
    for entry in _load(path)["pending"]:
        if entry.get("thread_id") == thread:
            return entry
    return None


def confirm(cfg, gmail, drive, store, people, msg: dict, entry: dict,
            path: Path | None = None) -> str:
    """Handle a reply in a link thread. Only the invited address, passing
    authentication, with the right code, links anything."""
    sender = _norm(msg.get("from_addr"))
    if sender != entry["other"]:
        return "message in an address-link thread from another address; ignored"
    authentic, why = gmail_io.sender_is_authenticated(msg)
    if not authentic:
        log.warning("Address-link reply from %s failed authentication (%s).", sender, why)
        return f"address-link reply failed authentication ({why}); ignored"
    data = _load(path)
    current = next((p for p in data["pending"] if p.get("thread_id") == entry["thread_id"]),
                   None)
    if current is None:
        return "address-link request no longer pending"
    if _expired(current):
        data["pending"].remove(current)
        _save(data, path)
        if not cfg.dry_run:
            terms_gate._send_fixed(cfg, gmail, msg, "That code has expired. Ask again from "
                                   "your main address if you still want to link them."
                                   "\n\n--- Indi")
        return "address-link code expired"
    if current["code"] not in re.findall(r"\b\d{6}\b", msg.get("body", "")):
        current["tries"] = int(current.get("tries", 0)) + 1
        left = MAX_TRIES - current["tries"]
        if left <= 0:
            data["pending"].remove(current)
        _save(data, path)
        if not cfg.dry_run:
            terms_gate._send_fixed(cfg, gmail, msg, (
                f"That isn't the code. {left} tr{'y' if left == 1 else 'ies'} left."
                if left > 0 else "That isn't the code, and that was the last try; the "
                "request is cancelled.") + "\n\n--- Indi")
        return f"address-link code wrong ({max(left, 0)} tries left)"
    if cfg.dry_run:
        return f"dry run: would link {sender} to {current['primary']}"

    primary = current["primary"]
    data["pending"].remove(current)
    grants = []
    if store is not None:
        try:
            import audience
            book = audience.GrantBook(store)
            note = f"linked addresses ({primary}, {sender})"
            grants = [book.add(sender, primary, evidence="address link code", note=note).grant_id,
                      book.add(primary, sender, evidence="address link code", note=note).grant_id]
            if hasattr(store, "_grants"):
                store._grants = None
        except Exception:
            log.warning("Could not grant linked addresses sight of each other's mail.",
                        exc_info=True)
    data["aliases"][sender] = {"primary": primary, "linked_at": clock.now().isoformat(),
                               "grants": grants}
    _save(data, path)

    if people is not None:
        try:
            main = people.get(primary)
            if main.has_accepted and not people.get(sender).has_accepted:
                people.record_acceptance(sender, main.terms_version_accepted)
        except Exception:
            log.warning("Could not carry the terms acceptance over to %s.", sender,
                        exc_info=True)

    merged = merge_memory(drive, primary, sender)
    log.info("Linked %s to %s (%s).", sender, primary, merged)
    terms_gate._send_fixed(cfg, gmail, msg, (
        f"Done: {sender} is now linked to {primary}. I'll treat them as one person, "
        "with one set of notes, and each address can see mail from the other.\n\n"
        "Not merged: reminders, follow-ups and open commitments stay with the address "
        "they were made from, newsletter subscriptions are per address, and anything "
        "other people agreed to share with one address isn't extended to the other. "
        "To undo this, ask me to unlink the address.\n\n--- Indi"))
    return f"linked {sender} to {primary}; {merged}"


# ---------------------------------------------------------------------------
# MERGING NOTES
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^\s*#{1,3}\s*(.+?)\s*$")


def _sections(body: str) -> list[tuple[str, list[str]]]:
    """(heading, lines) in order. Text before any heading is "Long-term":
    an old unsectioned file is all background."""
    out: list[tuple[str, list[str]]] = []
    current = ("Long-term", [])
    for line in (body or "").splitlines():
        heading = _HEADING.match(line)
        if heading:
            if current[1] or out:
                out.append(current)
            current = (heading.group(1).strip(), [])
        elif line.strip():
            current[1].append(line.rstrip())
    out.append(current)
    return [s for s in out if s[1] or s[0]]


def merge_bodies(main: str, other: str) -> str:
    """Deterministic: every line of both, grouped by section, duplicates
    dropped. No model, so nothing can be lost or invented here; the next
    consolidation tidies the result."""
    order = ["Shareable", "Long-term", "Current"]
    merged: dict[str, list[str]] = {}
    for body in (main, other):
        for heading, lines in _sections(body):
            key = next((o for o in order if o.lower() == heading.lower()), heading)
            bucket = merged.setdefault(key, [])
            for line in lines:
                if line not in bucket:
                    bucket.append(line)
    keys = [k for k in order if k in merged] + [k for k in merged if k not in order]
    return "\n\n".join(f"## {k}\n" + "\n".join(merged[k]) for k in keys).strip() + "\n"


def merge_memory(drive, primary: str, other: str) -> str:
    import memory_file
    import memory_rights
    if drive is None:
        return "no Drive: notes not merged"
    other_name = memory_file.memory_file_name(other, resolve=False)
    other_id = memory_file.find_file_id(drive, other_name)
    if not other_id:
        return "no notes to merge"
    other_mark, other_body = memory_file.parse_memory(memory_file.read_file(drive, other_id))
    main = memory_file.SenderMemory(drive, primary)
    body = merge_bodies(main.body, other_body) if main.body.strip() else other_body
    folded = set(main.watermark.ids or ()) | set(other_mark.ids or ())
    text = memory_file.serialise_memory(primary, folded, body)
    new_id = memory_file.write_file(drive, main.name, text, main.file_id)
    if memory_file.read_file(drive, new_id).strip() != text.strip():
        return "merged notes didn't save; the other address's notes were kept"
    failed = memory_rights._delete_copies(drive, other, resolve=False)
    return "notes merged" + (" (some old copies still to delete)" if failed else "")
