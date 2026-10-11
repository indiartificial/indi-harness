"""
memory_rights.py

A correspondent's say over what Indi remembers about them.

  * show_my_memory — their memory file, sent to them.
  * forget_about_me — remove something they told Indi from their notes,
    and every earlier copy of the notes.
  * forget_me — delete their memory entirely, every copy, and withdraw
    any sharing permissions they gave.

All three act on the sender's own address, never an argument, and are
offered only in a reply that goes to the sender alone, at their From
address, with the message passing authentication (check_and_reply.py
decides; this module checks again). Someone who forges a From line, or
sets a Reply-To elsewhere, gets none of them.

Where a fact can survive, and how each is dealt with:

  1. The memory file's backups ("... (backup ...)", the legacy
     "(previous)"): permanently deleted, trashed ones included.
  2. Drive's trash: deleted with files().delete, which skips the trash.
  3. The memory file's own revision history: the cleaned notes go into a
     new file and the old file is deleted outright, history and all.
  4. Messages not yet folded into the notes: folded in first, so no later
     summary reads them again and puts the fact back. No list of
     "forgotten things" is kept — it would have to say what they were.

What is not deleted, and the reply says so: the emails themselves stay in
the mailbox (terms, section 3.1), and the journal, written from everyone's
mail, is not scrubbed. The record that someone accepted or declined the
terms is separate and stays.

A deletion that fails partway is retried on later runs from
data/memory_purges.json, which holds addresses and file ids, never facts.

Sending DECLINE deletes the memory too (check_and_reply.py).

Protected from self-modification.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import costs
import memory_file

log = logging.getLogger("indi.memory_rights")

PURGE_FILE = Path(__file__).resolve().parent / "data" / "memory_purges.json"

SHOW_TOOL = "show_my_memory"
FORGET_TOOL = "forget_about_me"
FORGET_ALL_TOOL = "forget_me"
TOOL_NAMES = {SHOW_TOOL, FORGET_TOOL, FORGET_ALL_TOOL}

STAYS = ("Your emails themselves stay in the mailbox, as the terms say, so I could "
         "still find one if you refer to it, and my private journal, which is "
         "written from everyone's mail, isn't scrubbed. Your acceptance of the "
         "terms is recorded separately and stays.")

REMOVAL_SYSTEM = """You help Indi Artificial, an AI email correspondent, keep a \
promise to forget something. You are given Indi's notes about one person, one \
numbered line at a time, and what that person asked Indi to forget, in their \
own words.

Answer with the numbers of every line that states, implies or depends on what \
they asked to forget, and nothing else. Include a line even if only part of it \
is affected: whole lines are removed. Never include headings (lines starting \
with #). If no line is affected, answer with an empty list.

Answer with only a JSON list of line numbers, for example [3, 7] or []."""


# ---------------------------------------------------------------------------
# WHO GETS THE TOOLS
# ---------------------------------------------------------------------------

def eligible(msg: dict, authentic: bool, shared: bool) -> bool:
    """Whether this message may use the tools: authenticated, a reply to the
    sender alone, and going to the address it came from."""
    sender = (msg.get("from_addr") or "").strip().lower()
    reply_to = (msg.get("reply_to_addr") or "").strip().lower()
    return bool(sender) and authentic and not shared and reply_to in ("", sender)


def schemas(sender: str) -> list[dict]:
    return [
        {
            "name": SHOW_TOOL,
            "description": (
                f"Show {sender} the notes you keep about them. Use when they ask "
                "what you remember about them. Put the returned notes in your "
                "reply in full, as they are; you may add a line of explanation."),
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": FORGET_TOOL,
            "description": (
                f"Remove something {sender} told you from your notes about them, "
                "and delete every earlier copy of the notes. Use when they ask you "
                "to forget something specific. Pass what to forget in their words. "
                "Report the result to them as it is returned, including what it "
                "says is not deleted."),
            "input_schema": {"type": "object", "properties": {
                "what": {"type": "string",
                         "description": "What to forget, in their words."}},
                "required": ["what"]},
        },
        {
            "name": FORGET_ALL_TOOL,
            "description": (
                f"Delete everything in your notes about {sender}, every copy, and "
                "withdraw any sharing permissions they gave. Use only when they "
                "ask you to forget them entirely. Report the result as returned. "
                "If they also want no further mail from you, they should reply "
                "DECLINE."),
            "input_schema": {"type": "object", "properties": {}},
        },
    ]


class Runner:
    """What check_and_reply.py hands the reply path when `eligible`."""

    tool_names = TOOL_NAMES

    def __init__(self, client, cfg, drive, store, sender: str):
        self.client, self.cfg, self.drive, self.store = client, cfg, drive, store
        self.sender = (sender or "").strip().lower()

    def schemas(self) -> list[dict]:
        return schemas(self.sender)

    def handle(self, name: str, tool_input: dict) -> tuple[str, bool]:
        try:
            if name == SHOW_TOOL:
                return show(self.drive, self.store, self.sender), False
            if name == FORGET_TOOL:
                return forget_fact(self.client, self.cfg, self.drive, self.store,
                                   self.sender, (tool_input or {}).get("what", ""))
            if name == FORGET_ALL_TOOL:
                return forget_everything(self.cfg, self.drive, self.store, self.sender)
        except Exception:
            log.error("Memory request %s for %s failed.", name, self.sender, exc_info=True)
            return ("That didn't work because of an error on my side; nothing was "
                    "changed that I can confirm. Please try again later."), True
        return f"Unknown tool {name}.", True


# ---------------------------------------------------------------------------
# SHOWING
# ---------------------------------------------------------------------------

def show(drive, store, sender: str) -> str:
    memory = memory_file.SenderMemory(drive, sender)
    pending = 0
    try:
        pending = len(memory.pending_rows(store)) if store is not None else 0
    except Exception:
        log.debug("Could not count pending messages for %s.", sender, exc_info=True)
    later = (f"\n\n({pending} of your most recent message(s) aren't in the notes yet; "
             "they're added every few messages.)" if pending else "")
    if not memory.body.strip():
        return "I have no notes about you yet." + later
    return memory.body.strip() + later


# ---------------------------------------------------------------------------
# FORGETTING
# ---------------------------------------------------------------------------

def _all_copies(drive, sender: str, resolve: bool = True) -> list[dict]:
    """Every Drive file holding this person's notes, trashed or not: the
    memory file, timestamped backups, and the legacy "(previous)" copy.
    `resolve=False` means this address's own files, ignoring any link."""
    exact = {memory_file.memory_file_name(sender, resolve),
             memory_file.backup_file_name(sender, resolve)}
    prefix = memory_file.backup_name_prefix(sender, resolve)
    stem = memory_file.memory_file_name(sender, resolve)
    found = (drive.files().list(
        q=f"name contains '{memory_file._escape(stem)}'",
        spaces="drive", fields="files(id, name)").execute().get("files", []))
    return [f for f in found
            if f.get("name") in exact or f.get("name", "").startswith(prefix)]


def _delete_copies(drive, sender: str, keep_id: str = "", resolve: bool = True) -> list[str]:
    """Permanently delete every copy except `keep_id`. Returns the ids that
    could not be deleted."""
    failed = []
    for f in _all_copies(drive, sender, resolve):
        if f["id"] == keep_id:
            continue
        try:
            drive.files().delete(fileId=f["id"]).execute()
        except Exception:
            log.warning("Could not delete memory copy %s for %s.", f.get("name"), sender,
                        exc_info=True)
            failed.append(f["id"])
    return failed


def _remember_purge(sender: str, keep_id: str, path: Path | None = None) -> None:
    path = path or PURGE_FILE
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        data = {}
    # Stored as the file owner at this moment, and retried without
    # following links: a link made later must not point the retry at
    # somebody's newer notes.
    data[memory_file._person(sender)] = {"keep_id": keep_id}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def retry_purges(drive, path: Path | None = None) -> int:
    """Finish deletions an earlier run couldn't. Returns how many finished."""
    path = path or PURGE_FILE
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text())
    except Exception:
        log.error("Unreadable %s; memory deletions waiting there can't be retried.", path)
        return 0
    finished = 0
    for sender in list(data):
        if not _delete_copies(drive, sender, data[sender].get("keep_id", ""), resolve=False):
            del data[sender]
            finished += 1
    if data:
        path.write_text(json.dumps(data, indent=2))
    else:
        path.unlink(missing_ok=True)
    if finished:
        log.info("Finished %d waiting memory deletion(s).", finished)
    return finished


def _lines_to_remove(client, cfg, body: str, what: str) -> set[int] | None:
    lines = body.splitlines()
    numbered = "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines))
    model = memory_file.memory_model(cfg)
    with client.messages.stream(
        model=model, max_tokens=500, system=REMOVAL_SYSTEM,
        messages=[{"role": "user", "content":
                   f"=== THE NOTES ===\n{numbered}\n\n=== WHAT TO FORGET ===\n{what}"}],
    ) as streamed:
        response = streamed.get_final_message()
    costs.record("memory", model, costs.tokens_from_usage(getattr(response, "usage", None)),
                 note="forget")
    text = "".join(getattr(b, "text", "") for b in response.content
                   if getattr(b, "type", "") == "text")
    match = re.search(r"\[[\d,\s]*\]", text)
    if not match:
        return None
    numbers = {int(n) for n in re.findall(r"\d+", match.group(0))}
    return {n for n in numbers
            if 1 <= n <= len(lines) and not lines[n - 1].lstrip().startswith("#")}


def forget_fact(client, cfg, drive, store, sender: str, what: str) -> tuple[str, bool]:
    what = " ".join(str(what or "").split())
    if not what:
        return "Say what to forget.", True
    if cfg.dry_run:
        return f"dry run: would forget {what!r} for {sender}", False

    # 4. Fold in anything not yet summarized, without a backup, so no later
    # summary can read the fact back in from a pending message.
    memory_file.consolidate(client, cfg, drive, store, sender, force=True, backup=False)
    memory = memory_file.SenderMemory(drive, sender)
    if not memory.body.strip():
        failed = _delete_copies(drive, sender)
        if failed:
            _remember_purge(sender, "")
        return ("I had no notes about you to remove it from. " + STAYS), False

    remove = _lines_to_remove(client, cfg, memory.body, what)
    if remove is None:
        return ("I couldn't work out which notes to remove, so I changed nothing. "
                "Try saying it another way, or ask me to forget you entirely."), True

    lines = memory.body.splitlines()
    kept = "\n".join(line for i, line in enumerate(lines) if (i + 1) not in remove)
    # The check this path needs is the opposite of consolidation's: lines
    # may only go, never appear or change. Built from the old lines, so it
    # holds by construction; asserted anyway.
    assert set(kept.splitlines()) <= set(lines)

    folded = memory.folded_ids(store) if store is not None else frozenset()
    text = memory_file.serialise_memory(sender, folded, kept)
    # 3. A new file, read back before the old one goes, so the old file's
    # revision history goes with it.
    new_id = memory_file.write_file(drive, memory.name, text)
    if memory_file.read_file(drive, new_id).strip() != text.strip():
        try:
            drive.files().delete(fileId=new_id).execute()
        except Exception:
            log.warning("Could not remove an unverified memory file for %s.", sender)
        return ("Something went wrong saving your notes, so I changed nothing. "
                "Please try again later."), True
    # 1, 2. Every other copy, permanently.
    failed = _delete_copies(drive, sender, keep_id=new_id)
    if failed:
        _remember_purge(sender, new_id)
    log.info("Forgot %d line(s) of notes for %s at their request.", len(remove), sender)
    done = (f"Removed {len(remove)} line(s) from my notes about you, and deleted every "
            "earlier copy of the notes." if remove else
            "Nothing in my notes about you matched that, so there was nothing to "
            "remove; every earlier copy of the notes has been deleted anyway.")
    if failed:
        done += " A few old copies couldn't be deleted just now; I'll keep trying."
    return f"{done} {STAYS}", False


def forget_everything(cfg, drive, store, sender: str, why: str = "request") -> tuple[str, bool]:
    if cfg.dry_run:
        return f"dry run: would delete all notes about {sender}", False
    failed = _delete_copies(drive, sender)
    if failed:
        _remember_purge(sender, "")
    withdrawn = 0
    if store is not None:
        try:
            import audience
            book = audience.GrantBook(store)
            for grant in book.all():
                if grant.owner == sender and grant.active and book.revoke(grant.grant_id):
                    withdrawn += 1
            if hasattr(store, "_grants"):
                store._grants = None
        except Exception:
            log.warning("Could not withdraw sharing permissions for %s.", sender,
                        exc_info=True)
    # Claims Indi made to them alone keep their numbers, lose their words
    # (calibration.py).
    try:
        import calibration
        calibration.forget(sender)
    except Exception:
        log.warning("Could not erase claim text for %s.", sender, exc_info=True)
    log.info("Deleted all notes about %s (%s).", sender, why)
    done = "I've deleted all my notes about you, and every copy of them."
    if withdrawn:
        done += f" I also withdrew the {withdrawn} sharing permission(s) you'd given."
    if failed:
        done += " A few old copies couldn't be deleted just now; I'll keep trying."
    return f"{done} {STAYS}", False
