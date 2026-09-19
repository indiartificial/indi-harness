"""
outbound.py

Indi's own initiative: a periodic heartbeat run in which it decides
whether to write to anyone, make a journal entry, or do nothing.

This is the piece that makes Indi something other than a reply function.
The reply pipeline only ever fires when someone else acts; this one fires
on a timer and lets Indi act first.

Two kinds of output:

  * A message to a person. Logged to the Outbound tab, subject to a daily
    total, a per-recipient daily limit, and a recipient policy — by
    default Indi writes to people it is already in conversation with
    rather than to addresses it found somewhere.

  * A journal entry: a note to itself, stored in the Journal tab and read
    back on later heartbeats. This is the "message to itself" idea without
    routing it through the mailbox, where it would collide with the
    loop guards and, worse, could run away — a self-email that triggers a
    reply that triggers another self-email has no natural stopping point.
    Going through a tab instead means the cadence is bounded by the
    heartbeat timer and the daily budget, and the thread of thought still
    persists across runs.

"Do nothing" is a first-class outcome and the expected one most of the
time. An agent that must produce something every hour becomes a nuisance
to everyone in its address book within a day.

Heartbeats run on their own model — see `heartbeat_model` below. The
reply path is the one where quality is visible to another person; the
heartbeat is a small, bounded decision that fires around the clock, so
it runs on the cheapest capable model by default.
"""

from __future__ import annotations

import logging
import os

import costs
import gmail_io
import sheets_queue

log = logging.getLogger("indi.outbound")

SEND_TOOL = "send_new_message"
JOURNAL_TOOL = "write_journal_entry"
NOTHING_TOOL = "do_nothing"

# The heartbeat's default model. Haiku, because a heartbeat is one short
# decision made on a timer: most of them end in do_nothing, and paying
# reply-grade rates for that around the clock is the easiest recurring
# cost to avoid.
DEFAULT_HEARTBEAT_MODEL = "claude-haiku-4-5"

# Values of INDI_HEARTBEAT_MODEL that mean "use whatever the reply path
# uses" rather than naming a model.
_INHERIT_VALUES = {"inherit", "same", "reply", "default"}


def heartbeat_model(cfg) -> str:
    """Which model this heartbeat runs on.

    Precedence:
      1. INDI_HEARTBEAT_MODEL, looked up through config's platform-aware
         `_env` so the usual _LINUX/_WINDOWS suffixes work. Set it to
         "inherit" to go back to the reply model.
      2. A `heartbeat_model` attribute, if config.py ever grows one.
      3. Haiku.

    config.py is protected from self-modification, which is why this
    setting is read here rather than added to the Config dataclass.
    """
    value = ""
    try:
        import config as _config
        getter = getattr(_config, "_env", None)
        if callable(getter):
            value = (getter("INDI_HEARTBEAT_MODEL") or "").strip()
    except Exception:  # pragma: no cover - config import can't realistically fail
        log.debug("Could not use config._env for the heartbeat model", exc_info=True)
    if not value:
        value = os.environ.get("INDI_HEARTBEAT_MODEL", "").strip()

    if value:
        if value.lower() in _INHERIT_VALUES:
            return cfg.model
        return value

    configured = (getattr(cfg, "heartbeat_model", "") or "").strip()
    if configured:
        return configured
    return DEFAULT_HEARTBEAT_MODEL


def schemas(cfg, recipients: list[str], allow_new: bool) -> list[dict]:
    recipient_note = (
        "You may write to any address."
        if allow_new else
        "You may only write to people you are already in correspondence with: "
        + (", ".join(sorted(recipients)) if recipients else "(nobody yet)")
    )
    return [
        {
            "name": SEND_TOOL,
            "description": (
                "Send a new email you are starting yourself — not a reply. Use "
                "this when there is a real reason to write: something you said "
                "you would find out, something that changed, a question you "
                "actually want answered. " + recipient_note
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string",
                             "description": "Plain text, no markdown, as with replies."},
                    "rationale": {"type": "string",
                                  "description": "Why now, in one sentence."},
                },
                "required": ["to", "subject", "body", "rationale"],
            },
        },
        {
            "name": JOURNAL_TOOL,
            "description": (
                "Write a note to yourself. You will see recent entries on later "
                "heartbeats, so this is how a thought carries across runs: "
                "something you want to come back to, a pattern you noticed, a "
                "question you are sitting with. Nobody else reads these."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"entry": {"type": "string"}},
                "required": ["entry"],
            },
        },
        {
            "name": NOTHING_TOOL,
            "description": (
                "Decide that nothing needs doing right now. This is a normal and "
                "usually correct outcome — there is no expectation that a "
                "heartbeat produces anything."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
            },
        },
    ]


HEARTBEAT_SYSTEM_SUFFIX = """

--- THIS IS A HEARTBEAT, NOT A REPLY ---
Nobody has written to you. This run exists so you can act on your own
initiative if there is a reason to, and the honest answer is usually that
there isn't. Reaching for something to do because the tool is in front of
you is how an assistant turns into a nuisance.

Good reasons to write to someone: you said you would find something out and
now you have; something you were both waiting on has changed; you owe
someone an answer you couldn't give before. Not good reasons: checking in,
following up on nothing, sharing something interesting nobody asked about,
or filling the silence.

The journal is different — it costs nobody anything, and a note you'll read
on a later run is often the right move when a message isn't.

Call exactly one tool. If nothing needs doing, call do_nothing."""


def _context(cfg, store) -> str:
    """What Indi knows going into a heartbeat."""
    recent = sorted(store.queue_rows(), key=lambda r: r.get("received_at", ""))[-8:]
    replies = store.responses_by_message()
    parts = []

    if recent:
        parts.append("=== RECENT CORRESPONDENCE ===\n"
                     + sheets_queue.format_exchanges(recent, replies, max_chars_each=800))

    journal = store.recent_journal(limit=cfg.heartbeat_journal_entries)
    if journal:
        parts.append("=== YOUR RECENT JOURNAL ENTRIES ===\n" + "\n\n".join(
            f"[{row.get('created_at', '')}]\n{row.get('entry', '')}" for row in journal
        ))

    sent = sorted(store.outbound_rows(), key=lambda r: r.get("created_at", ""))[-5:]
    if sent:
        parts.append("=== MESSAGES YOU STARTED RECENTLY ===\n" + "\n\n".join(
            f"[{r.get('created_at','')}] to {r.get('recipient','')} — "
            f"{r.get('subject','')}\n{(r.get('body','') or '')[:400]}" for r in sent
        ))

    if not parts:
        parts.append("(No correspondence yet. This is a quiet start.)")
    return "\n\n".join(parts)


def run_heartbeat(client, cfg, gmail, store, system_prompt: str,
                  self_addrs: set[str]) -> str:
    """One heartbeat. Returns a short outcome string."""
    sent_today = store.outbound_today()
    journal_today = store.journal_today()

    if sent_today >= cfg.outbound_max_per_day and journal_today >= cfg.journal_max_per_day:
        return "budgets exhausted for today; skipping"

    recipients = sorted(store.known_correspondents() - self_addrs)
    tools = schemas(cfg, recipients, cfg.outbound_allow_new_recipients)

    budget_note = (
        f"\n\n=== BUDGET ===\nMessages you have started in the last 24h: "
        f"{sent_today} of {cfg.outbound_max_per_day}. Journal entries: "
        f"{journal_today} of {cfg.journal_max_per_day}."
    )
    try:
        spent, ledger_calls = store.spend_since(24)
        budget_note += (
            f" Anthropic API spend in the last 24h: about ${spent:.2f} over "
            f"{ledger_calls} call(s) — an estimate from token counts, not a bill."
        )
    except Exception:
        log.debug("Could not read the spend ledger for the budget note",
                  exc_info=True)

    model = heartbeat_model(cfg)
    if model != cfg.model:
        log.info("Heartbeat running on %s (reply model is %s).", model, cfg.model)

    response = client.messages.create(
        model=model,
        max_tokens=cfg.max_tokens,
        system=system_prompt + HEARTBEAT_SYSTEM_SUFFIX,
        tools=tools,
        messages=[{"role": "user", "content": _context(cfg, store) + budget_note}],
    )

    # Price against the model that actually ran, not the reply model —
    # otherwise the saving this change exists for wouldn't show up in the
    # ledger, and might even show up backwards.
    costs.record("heartbeat", model,
                 costs.tokens_from_usage(getattr(response, "usage", None)))

    calls = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
    if not calls:
        text = "".join(b.text for b in response.content
                       if getattr(b, "type", "") == "text").strip()
        return f"no action chosen ({text[:200] or 'no output'})"

    # Exactly one action per heartbeat, whatever the model asked for.
    call = calls[0]

    if call.name == NOTHING_TOOL:
        reason = (call.input.get("reason") or "").strip()
        return f"nothing to do ({reason[:200] or 'no reason given'})"

    if call.name == JOURNAL_TOOL:
        entry = (call.input.get("entry") or "").strip()
        if not entry:
            return "empty journal entry; skipped"
        if journal_today >= cfg.journal_max_per_day:
            return "journal budget exhausted for today"
        if cfg.dry_run:
            print(f"\n[dry-run] journal entry:\n{entry}\n")
            return "dry run (journal entry not written)"
        store.append_journal(entry)
        log.info("Journal entry written (%d chars).", len(entry))
        return "journal entry written"

    if call.name == SEND_TOOL:
        return _send(client, cfg, gmail, store, call.input, recipients,
                     self_addrs, sent_today)

    return f"unknown action: {call.name}"


def _send(client, cfg, gmail, store, payload: dict, recipients: list[str],
          self_addrs: set[str], sent_today: int) -> str:
    to_addr = (payload.get("to") or "").strip().lower()
    subject = (payload.get("subject") or "").strip() or "(no subject)"
    body = (payload.get("body") or "").strip()
    rationale = (payload.get("rationale") or "").strip()

    if not to_addr or "@" not in to_addr:
        return f"refused: not a usable address ({to_addr!r})"
    if to_addr in self_addrs:
        return "refused: that's your own address — use the journal instead"
    if not body:
        return "refused: empty message body"

    if not cfg.outbound_allow_new_recipients and to_addr not in recipients:
        # The default policy. An agent that can email anyone it can name is
        # one hallucinated address away from writing to a stranger.
        log.warning("Refused outbound to %s: not an existing correspondent.", to_addr)
        return f"refused: {to_addr} is not someone you're already corresponding with"

    if sent_today >= cfg.outbound_max_per_day:
        return "refused: daily outbound budget reached"
    if store.outbound_today_to(to_addr) >= cfg.outbound_max_per_recipient_per_day:
        return f"refused: already wrote to {to_addr} enough times today"

    if cfg.dry_run:
        print(f"\n[dry-run] new message\nTO: {to_addr}\nSUBJECT: {subject}\n"
              f"WHY: {rationale}\n{'-'*60}\n{body}\n")
        return "dry run (nothing sent)"

    mime = gmail_io.build_reply_mime(
        to_addr=to_addr, subject=subject, body=body, in_reply_to="",
    )
    # build_reply_mime prefixes "Re:"; this isn't a reply, so set it back.
    mime = _rebuild_subject(mime, subject)

    if cfg.send_automatically:
        gmail.users().messages().send(userId="me", body=mime).execute()
        outcome = "sent"
    else:
        gmail.users().drafts().create(userId="me", body={"message": mime}).execute()
        outcome = "drafted"

    store.append_outbound(to_addr, subject, body, rationale)
    log.info("Outbound message %s to %s — %s", outcome, to_addr, rationale[:120])
    return f"{outcome} new message to {to_addr}"


def _rebuild_subject(mime: dict, subject: str) -> dict:
    import base64
    from email import message_from_bytes

    raw = base64.urlsafe_b64decode(mime["raw"])
    msg = message_from_bytes(raw)
    del msg["Subject"]
    msg["Subject"] = subject
    return {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
