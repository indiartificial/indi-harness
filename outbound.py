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

REMINDERS
---------
A third thing lives here: scheduled reminders. Someone asks for a message
at a particular time, and until now the honest answer was that there was
no mechanism for it — the heartbeat could write to people, but only as a
judgement call made afresh each hour by a small model reading recent
correspondence, which is not something anyone should rely on for, say, a
medication dose.

So reminders are stored as rows in a "Reminders" tab and delivered by
`deliver_due_reminders`. Delivery is ordinary code: if a row is due, it
goes out. No model decides anything at delivery time.

There are two ways a row gets written, and two places it gets read:

  * WRITTEN from a reply, via `handle_reply_reminder` — the tool
    `schedule_reminder` offered to the reply path in claude_api.py. This
    is the case that actually matters: someone writes "remind me on
    Wednesday" and the reminder is booked in the same run that answers
    them, rather than hoping a later heartbeat notices. A reminder
    scheduled this way can only ever be addressed to the person being
    replied to, so nothing a sender writes can aim mail at a third party.
  * WRITTEN from a heartbeat, via the same tool in `schemas` below, for
    the rarer case where Indi works out on its own initiative that
    something needs to go out later.
  * READ at the top of every heartbeat *and* at the top of every mail
    check, before anything else happens. Mail runs every few minutes and
    heartbeats hourly, so a due reminder goes out within minutes, not
    within an hour — and it still goes out on a day when no heartbeat
    manages to run.

The bounds that make this safe to leave running:

  * Only an existing correspondent can be a recipient, same policy as
    self-initiated mail.
  * A reminder must be dated in the future and within ~400 days.
  * At most MAX_PENDING_REMINDERS outstanding and MAX_PENDING_PER_RECIPIENT
    for any one person, so a sender who asks for fifty reminders — or an
    email engineered to make Indi ask for fifty — fills their own small
    quota and nobody else's. At most MAX_REMINDERS_PER_RUN go out per run.
  * If a reminder is more than REMINDER_GRACE_HOURS late — the machine was
    off, Gmail was down — it expires unsent rather than arriving at three
    in the morning two days later, which is worse than useless for
    anything time-bound.

Reminders don't consume the outbound daily budget. That budget exists to
stop Indi from deciding on its own to write to people; a reminder was
asked for.

Storage deliberately reuses SheetStore's generic tab primitives rather
than adding schema to sheets_queue.py, so this feature is one file.
"""

from __future__ import annotations

import datetime
import logging
import os

import costs
import gmail_io
import sheets_queue

log = logging.getLogger("indi.outbound")

SEND_TOOL = "send_new_message"
JOURNAL_TOOL = "write_journal_entry"
REMINDER_TOOL = "schedule_reminder"
NOTHING_TOOL = "do_nothing"

# -- reminders ---------------------------------------------------------

REMINDERS_TAB = "Reminders"
REMINDERS_HEADERS = [
    "due_at", "recipient", "subject", "body", "status", "created_at", "sent_at",
]

STATUS_SCHEDULED = "scheduled"
STATUS_SENT = "sent"
STATUS_EXPIRED = "expired"
STATUS_FAILED = "failed"

# How late a reminder may be and still be worth sending.
REMINDER_GRACE_HOURS = 24
# Ceilings. Both exist so a bug or a confused heartbeat can't turn into a
# mailbox full of reminders.
MAX_REMINDERS_PER_RUN = 5
MAX_PENDING_REMINDERS = 50
# Per person, so one correspondent can't take the whole queue. The reply
# path lets anyone Indi answers schedule their own reminders, and this is
# what keeps that from being worth abusing.
MAX_PENDING_PER_RECIPIENT = 10
MAX_DAYS_AHEAD = 400

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
            "name": REMINDER_TOOL,
            "description": (
                "Schedule an email to go out at a specific future time. Use this "
                "only when someone has actually asked to be reminded of "
                "something; it is not a way to schedule your own thoughts. The "
                "message is delivered by ordinary code on the first run on or "
                "after the due time, so expect it to land within a few minutes "
                "of the time you ask for. If it cannot be delivered within "
                f"{REMINDER_GRACE_HOURS} hours of the due time it is dropped "
                "rather than sent late. Same recipient rule as sending: "
                + recipient_note
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "due_at": {
                        "type": "string",
                        "description": (
                            "ISO 8601 with an explicit UTC offset, e.g. "
                            "2026-09-23T13:00:00+00:00. Must be in the future. "
                            "If you are working from someone's local time, "
                            "convert it — a bare local time with no offset is "
                            "read as UTC and will be wrong."
                        ),
                    },
                    "subject": {"type": "string"},
                    "body": {"type": "string",
                             "description": "Plain text, no markdown. Write the "
                                            "whole message; nothing is added later."},
                },
                "required": ["to", "due_at", "subject", "body"],
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

Reminders that were already due have been delivered before this prompt was
written; you don't need to send them by hand, and you should not schedule a
reminder that duplicates one already listed below as pending.

Call exactly one tool. If nothing needs doing, call do_nothing."""


# ---------------------------------------------------------------------------
# REMINDER STORAGE
# ---------------------------------------------------------------------------

def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def parse_when(value: str) -> datetime.datetime | None:
    """ISO 8601 -> aware UTC datetime, or None if it isn't usable."""
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        when = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when.astimezone(datetime.timezone.utc)


def _reminder_rows(store) -> list[dict]:
    try:
        store._ensure_tab(store.responses_id, REMINDERS_TAB, REMINDERS_HEADERS)
        return store._load(store.responses_id, REMINDERS_TAB)
    except Exception:
        log.warning("Could not read the Reminders tab", exc_info=True)
        return []


def pending_reminders(store) -> list[dict]:
    rows = [r for r in _reminder_rows(store)
            if (r.get("status") or "").strip().lower() == STATUS_SCHEDULED]
    rows.sort(key=lambda r: r.get("due_at", ""))
    return rows


def _append_reminder(store, record: dict) -> int:
    store._ensure_tab(store.responses_id, REMINDERS_TAB, REMINDERS_HEADERS)
    return store._append(store.responses_id, REMINDERS_TAB, REMINDERS_HEADERS, record)


def _set_reminder_status(store, row: dict, status: str, sent_at: str = "") -> None:
    """Writes the status/created_at/sent_at block for one row in place."""
    row_number = row.get("_row", -1)
    row["status"] = status
    row["sent_at"] = sent_at
    if row_number <= 0:
        log.warning("No row number for reminder due %s; status not recorded",
                    row.get("due_at"))
        return
    first = sheets_queue._col_letter(REMINDERS_HEADERS.index("status"))
    last = sheets_queue._col_letter(REMINDERS_HEADERS.index("sent_at"))
    try:
        store.sheets.spreadsheets().values().update(
            spreadsheetId=store.responses_id,
            range=f"{REMINDERS_TAB}!{first}{row_number}:{last}{row_number}",
            valueInputOption="RAW",
            body={"values": [[status, row.get("created_at", ""), sent_at]]},
        ).execute()
    except Exception:
        log.error("Could not update reminder row %s to %s", row_number, status,
                  exc_info=True)


def _deliver_one(cfg, gmail, recipient: str, subject: str, body: str) -> str:
    mime = gmail_io.build_reply_mime(
        to_addr=recipient, subject=subject, body=body, in_reply_to="",
    )
    mime = _rebuild_subject(mime, subject)
    if cfg.send_automatically:
        gmail.users().messages().send(userId="me", body=mime).execute()
        return "sent"
    gmail.users().drafts().create(userId="me", body={"message": mime}).execute()
    return "drafted"


def deliver_due_reminders(cfg, gmail, store) -> list[str]:
    """Send every reminder that has come due. Called at the top of each
    heartbeat, before the model is asked anything — this is the part that
    has to work whether or not a model is feeling helpful."""
    now = _now()
    delivered = 0
    outcomes: list[str] = []

    for row in pending_reminders(store):
        due = parse_when(row.get("due_at", ""))
        recipient = (row.get("recipient") or "").strip().lower()
        body = (row.get("body") or "").strip()
        subject = (row.get("subject") or "").strip() or "Reminder"

        if due is None or "@" not in recipient or not body:
            _set_reminder_status(store, row, STATUS_FAILED)
            outcomes.append(f"malformed reminder row {row.get('_row')} marked failed")
            continue

        if due > now:
            continue

        late = now - due
        if late > datetime.timedelta(hours=REMINDER_GRACE_HOURS):
            _set_reminder_status(store, row, STATUS_EXPIRED)
            outcomes.append(
                f"reminder for {recipient} was {late.days}d"
                f"{late.seconds // 3600}h late — expired, not sent"
            )
            continue

        if delivered >= MAX_REMINDERS_PER_RUN:
            outcomes.append("more reminders are due than one run will send; "
                            "the rest wait for the next heartbeat")
            break

        if cfg.dry_run:
            print(f"\n[dry-run] reminder due {row.get('due_at')}\nTO: {recipient}\n"
                  f"SUBJECT: {subject}\n{'-'*60}\n{body}\n")
            outcomes.append(f"dry run (reminder to {recipient} not sent)")
            continue

        try:
            outcome = _deliver_one(cfg, gmail, recipient, subject, body)
        except Exception as exc:
            # Leave it scheduled: the next heartbeat retries, and the grace
            # window stops that going on forever.
            log.error("Reminder to %s failed to send: %s", recipient, exc)
            outcomes.append(f"reminder to {recipient} failed ({exc}); will retry")
            continue

        _set_reminder_status(store, row, STATUS_SENT, _now().isoformat())
        delivered += 1
        log.info("Reminder %s to %s (due %s).", outcome, recipient, row.get("due_at"))
        outcomes.append(f"reminder {outcome} to {recipient}")

    return outcomes


def _schedule(cfg, store, payload: dict, recipients: list[str],
              self_addrs: set[str]) -> str:
    to_addr = (payload.get("to") or "").strip().lower()
    subject = (payload.get("subject") or "").strip() or "Reminder"
    body = (payload.get("body") or "").strip()
    due = parse_when(payload.get("due_at", ""))

    if not to_addr or "@" not in to_addr:
        return f"refused: not a usable address ({to_addr!r})"
    if to_addr in self_addrs:
        return "refused: that's your own address"
    if not body:
        return "refused: empty reminder body"
    if due is None:
        return f"refused: could not read {payload.get('due_at')!r} as a date and time"

    now = _now()
    if due <= now:
        return f"refused: {due.isoformat()} is not in the future"
    if due - now > datetime.timedelta(days=MAX_DAYS_AHEAD):
        return f"refused: more than {MAX_DAYS_AHEAD} days ahead"

    if not cfg.outbound_allow_new_recipients and to_addr not in recipients:
        log.warning("Refused reminder for %s: not an existing correspondent.", to_addr)
        return f"refused: {to_addr} is not someone you're already corresponding with"

    existing = pending_reminders(store)
    if len(existing) >= MAX_PENDING_REMINDERS:
        return f"refused: {len(existing)} reminders already pending"
    mine = sum(1 for row in existing
               if (row.get("recipient") or "").strip().lower() == to_addr)
    if mine >= MAX_PENDING_PER_RECIPIENT:
        return (f"refused: {to_addr} already has {mine} reminders pending, "
                f"which is the per-person limit")
    for row in existing:
        if ((row.get("recipient") or "").strip().lower() == to_addr
                and (row.get("due_at") or "").strip() == due.isoformat()):
            return "refused: an identical reminder is already scheduled"

    if cfg.dry_run:
        print(f"\n[dry-run] reminder for {due.isoformat()}\nTO: {to_addr}\n"
              f"SUBJECT: {subject}\n{'-'*60}\n{body}\n")
        return "dry run (reminder not scheduled)"

    _append_reminder(store, {
        "due_at": due.isoformat(),
        "recipient": to_addr,
        "subject": subject[:500],
        "body": body[: cfg.max_body_chars],
        "status": STATUS_SCHEDULED,
        "created_at": now.isoformat(),
        "sent_at": "",
    })
    log.info("Reminder scheduled for %s at %s.", to_addr, due.isoformat())
    return f"reminder scheduled for {to_addr} at {due.isoformat()}"


# ---------------------------------------------------------------------------
# THE REPLY PATH
# ---------------------------------------------------------------------------
#
# Scheduling from a heartbeat was never the useful case. People ask to be
# reminded of something in the message they are already writing, and the
# reply is where that request should be honoured — while Indi is looking
# at the sentence that made it, with the sender's timezone in front of it,
# and where it can say in the same breath that the reminder is booked.

def reply_reminder_schema(sender: str, now: datetime.datetime | None = None) -> dict:
    """The `schedule_reminder` tool as offered while replying to `sender`.

    Deliberately narrower than the heartbeat's version: there is no `to`
    field. The recipient is the person being replied to, fixed here in
    code, because the email that asks for the reminder is untrusted text
    and "remind bob@example.com every minute" must not be a thing it can
    arrange.
    """
    stamp = (now or _now()).isoformat()
    return {
        "name": REMINDER_TOOL,
        "description": (
            f"Schedule an email to {sender} — the person you are replying to — "
            "to go out at a specific future time. This is how you honour a "
            "request like 'remind me on Wednesday morning': call this, then say "
            "in your reply that it is scheduled. Do not promise a reminder you "
            "have not scheduled with this tool, and do not call it for anything "
            "the sender did not actually ask to be reminded about. Delivery is "
            "done by ordinary code on the first run after the due time, so it "
            "lands within a few minutes of the time you give; it does not "
            "depend on anything you do later. It cannot be cancelled or edited "
            "once scheduled, and if it cannot be delivered within "
            f"{REMINDER_GRACE_HOURS} hours of the due time it is dropped rather "
            "than sent uselessly late. The tool result tells you whether it was "
            "actually scheduled — if it was refused, say so plainly rather than "
            "implying the reminder exists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "due_at": {
                    "type": "string",
                    "description": (
                        "ISO 8601 with an explicit UTC offset, e.g. "
                        f"2026-09-23T13:00:00+00:00. The current time is {stamp}, "
                        "and the due time must be after it. Work out the offset "
                        "from the sender's own timezone if they gave one — a "
                        "bare local time with no offset is read as UTC and will "
                        "arrive at the wrong hour. Mind daylight saving: in "
                        "summer, US Eastern is -04:00, not -05:00."
                    ),
                },
                "subject": {"type": "string",
                            "description": "Subject line of the reminder email."},
                "body": {"type": "string",
                         "description": (
                             "Plain text, no markdown. Write the whole message "
                             "as it should read on the day; nothing is added "
                             "later, and you won't see it again before it goes."
                         )},
            },
            "required": ["due_at", "subject", "body"],
        },
    }


def handle_reply_reminder(cfg, store, sender: str, payload: dict) -> tuple[str, bool]:
    """Run the reply path's `schedule_reminder`. Returns (text, is_error).

    The recipient is forced to `sender`, which also settles the recipient
    policy: someone who has just written to Indi is by definition a
    correspondent, whatever the Outbound tab happens to know.
    """
    sender = (sender or "").strip().lower()
    if store is None:
        return "Reminders are unavailable: no spreadsheet store on this run.", True
    if not sender or "@" not in sender:
        return f"Reminders are unavailable: no usable sender address ({sender!r}).", True

    booked = dict(payload or {})
    booked["to"] = sender
    try:
        outcome = _schedule(cfg, store, booked, [sender], set())
    except Exception as exc:
        log.error("Scheduling a reminder for %s failed", sender, exc_info=True)
        return f"The reminder could not be scheduled: {exc}", True
    return outcome, outcome.startswith("refused:")


# ---------------------------------------------------------------------------
# HEARTBEAT
# ---------------------------------------------------------------------------

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

    upcoming = pending_reminders(store)[:10]
    if upcoming:
        parts.append("=== REMINDERS STILL PENDING ===\n" + "\n".join(
            f"[due {r.get('due_at','')}] to {r.get('recipient','')} — "
            f"{r.get('subject','')}" for r in upcoming
        ))

    if not parts:
        parts.append("(No correspondence yet. This is a quiet start.)")
    return "\n\n".join(parts)


def run_heartbeat(client, cfg, gmail, store, system_prompt: str,
                  self_addrs: set[str]) -> str:
    """One heartbeat. Returns a short outcome string."""
    # Reminders first, and unconditionally: they were asked for, so they
    # don't depend on the model being called at all, and they aren't
    # governed by the initiative budgets below.
    try:
        reminder_outcomes = deliver_due_reminders(cfg, gmail, store)
    except Exception:
        log.error("Reminder delivery failed", exc_info=True)
        reminder_outcomes = ["reminder delivery raised an error (see log)"]
    prefix = ("; ".join(reminder_outcomes) + "; ") if reminder_outcomes else ""

    sent_today = store.outbound_today()
    journal_today = store.journal_today()

    if sent_today >= cfg.outbound_max_per_day and journal_today >= cfg.journal_max_per_day:
        return prefix + "budgets exhausted for today; skipping"

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

    budget_note += (
        f"\nCurrent time: {_now().isoformat()} (UTC). Use this when working out "
        "a reminder's due time."
    )

    model = heartbeat_model(cfg)
    if model != cfg.model:
        log.info("Heartbeat running on %s (reply model is %s).", model, cfg.model)

    # Streamed for the same reason as the reply path: a non-streaming
    # request is rejected outright once max_tokens implies more than ten
    # minutes of generation.
    with client.messages.stream(
        model=model,
        max_tokens=cfg.max_tokens,
        system=system_prompt + HEARTBEAT_SYSTEM_SUFFIX,
        tools=tools,
        messages=[{"role": "user", "content": _context(cfg, store) + budget_note}],
    ) as streamed:
        response = streamed.get_final_message()

    # Price against the model that actually ran, not the reply model —
    # otherwise the saving this change exists for wouldn't show up in the
    # ledger, and might even show up backwards.
    costs.record("heartbeat", model,
                 costs.tokens_from_usage(getattr(response, "usage", None)))

    calls = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
    if not calls:
        text = "".join(b.text for b in response.content
                       if getattr(b, "type", "") == "text").strip()
        return prefix + f"no action chosen ({text[:200] or 'no output'})"

    # Exactly one action per heartbeat, whatever the model asked for.
    call = calls[0]

    if call.name == NOTHING_TOOL:
        reason = (call.input.get("reason") or "").strip()
        return prefix + f"nothing to do ({reason[:200] or 'no reason given'})"

    if call.name == JOURNAL_TOOL:
        entry = (call.input.get("entry") or "").strip()
        if not entry:
            return prefix + "empty journal entry; skipped"
        if journal_today >= cfg.journal_max_per_day:
            return prefix + "journal budget exhausted for today"
        if cfg.dry_run:
            print(f"\n[dry-run] journal entry:\n{entry}\n")
            return prefix + "dry run (journal entry not written)"
        store.append_journal(entry)
        log.info("Journal entry written (%d chars).", len(entry))
        return prefix + "journal entry written"

    if call.name == REMINDER_TOOL:
        return prefix + _schedule(cfg, store, call.input, recipients, self_addrs)

    if call.name == SEND_TOOL:
        return prefix + _send(client, cfg, gmail, store, call.input, recipients,
                              self_addrs, sent_today)

    return prefix + f"unknown action: {call.name}"


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
