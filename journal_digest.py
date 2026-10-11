"""
journal_digest.py

The journal is Indi's notes to itself, and the heartbeat reads only the
last few entries (INDI_HEARTBEAT_JOURNAL_ENTRIES). Everything older was,
in practice, forgotten. Once a week this folds the entries written since
the last digest, together with that digest, into a new one: a journal
entry beginning "WEEKLY DIGEST". The heartbeat shows the latest digest
alongside the recent entries, so what mattered keeps being carried
forward without the context growing.

The digest is written from the journal alone, which is itself written
from everyone's mail, so it is shown in the same place the journal is:
the heartbeat's own deliberation, never a message to anyone
(outbound._context_for leaves both out).

On unless INDI_JOURNAL_DIGEST=false. INDI_JOURNAL_DIGEST_DAYS sets the
interval (default 7). Runs on the memory model (memory_file.memory_model).
"""

from __future__ import annotations

import datetime
import logging

import clock
import costs
import gmail_reads

log = logging.getLogger("indi.journal_digest")

PREFIX = "WEEKLY DIGEST"
MIN_NEW_ENTRIES = 3
MAX_ENTRY_CHARS = 1500
MAX_INPUT_ENTRIES = 200

SYSTEM = """You keep the long view of a journal that Indi Artificial, an AI \
email correspondent, writes to itself. You are given the previous digest (if \
any) and the journal entries written since it.

Write the new digest, as Indi, in the first person, under about {words} words. \
Carry forward from the previous digest whatever still matters, and add what \
matters from the new entries:
- what you have been doing and thinking about, in a few lines
- lessons learned and mistakes not to repeat
- threads of thought you want to keep developing
- open questions and things you meant to do but haven't

Drop what was settled or no longer matters. Be specific; leave out filler. \
Plain text, no markdown headings. Respond with only the digest."""


def enabled() -> bool:
    return (gmail_reads._setting("INDI_JOURNAL_DIGEST") or "true").lower() \
        not in ("0", "false", "no", "off")


def interval_days() -> int:
    try:
        return max(1, int(gmail_reads._setting("INDI_JOURNAL_DIGEST_DAYS") or 7))
    except ValueError:
        return 7


def is_digest(row: dict) -> bool:
    return (row.get("entry") or "").lstrip().upper().startswith(PREFIX)


def _when(row: dict) -> datetime.datetime | None:
    try:
        when = datetime.datetime.fromisoformat(row.get("created_at", ""))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=datetime.timezone.utc)


def _sorted(store) -> list[dict]:
    return sorted(store.journal_rows(), key=lambda r: r.get("created_at", ""))


def latest(store) -> dict | None:
    """The most recent digest entry, or None."""
    digests = [r for r in _sorted(store) if is_digest(r)]
    return digests[-1] if digests else None


def due(store, now: datetime.datetime | None = None) -> tuple[bool, dict | None, list[dict]]:
    """(due, previous digest, entries since it)."""
    now = now or clock.now()
    rows = _sorted(store)
    previous = next((r for r in reversed(rows) if is_digest(r)), None)
    if previous is not None:
        when = _when(previous)
        if when is not None and now - when < datetime.timedelta(days=interval_days()):
            return False, previous, []
        since = previous.get("created_at", "")
        new = [r for r in rows if not is_digest(r) and r.get("created_at", "") > since]
    else:
        new = [r for r in rows if not is_digest(r)]
        # The first digest waits until the journal is a week old, so it
        # isn't written from an afternoon's entries.
        first = _when(new[0]) if new else None
        if first is None or now - first < datetime.timedelta(days=interval_days()):
            return False, None, new
    return len(new) >= MIN_NEW_ENTRIES, previous, new


def maybe_write(client, cfg, store, now: datetime.datetime | None = None) -> str:
    """Write a digest if one is due. Returns a short outcome."""
    if not enabled():
        return "journal digest off"
    owed, previous, new = due(store, now)
    if not owed:
        return "no journal digest due"
    new = new[-MAX_INPUT_ENTRIES:]
    import memory_file
    model = memory_file.memory_model(cfg)
    words = 400
    content = (
        "=== PREVIOUS DIGEST ===\n"
        + ((previous or {}).get("entry") or "(none: this is the first)")
        + "\n\n=== JOURNAL ENTRIES SINCE ===\n"
        + "\n\n".join(f"[{r.get('created_at', '')}]\n{(r.get('entry') or '')[:MAX_ENTRY_CHARS]}"
                      for r in new)
    )
    with client.messages.stream(
        model=model, max_tokens=2000, system=SYSTEM.format(words=words),
        messages=[{"role": "user", "content": content}],
    ) as streamed:
        response = streamed.get_final_message()
    costs.record("journal-digest", model,
                 costs.tokens_from_usage(getattr(response, "usage", None)))
    text = "".join(getattr(b, "text", "") for b in response.content
                   if getattr(b, "type", "") == "text").strip()
    if getattr(response, "stop_reason", "") == "max_tokens" or len(text) < 40:
        log.warning("Journal digest came back unusable; trying again next heartbeat.")
        return "journal digest unusable; will retry"
    start = (new[0].get("created_at", "") or "")[:10]
    end = (new[-1].get("created_at", "") or "")[:10]
    entry = f"{PREFIX} ({start} to {end}, {len(new)} entries)\n\n{text}"
    if cfg.dry_run:
        log.info("[dry-run] Would write a journal digest:\n%s", entry)
        return "dry run: journal digest not written"
    store.append_journal(entry)
    log.info("Journal digest written (%d entries folded).", len(new))
    return f"journal digest written ({len(new)} entries)"
