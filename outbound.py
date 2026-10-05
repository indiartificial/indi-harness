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
  * A repeating reminder is bounded twice over: an interval of at least
    an hour, and at most MAX_OCCURRENCES_PER_SERIES messages in a series
    even when nobody gave an end date. It is the only thing in here that
    keeps sending without anyone asking again, so a series that stops on
    its own has to be the default rather than something to remember.

REPEATS AND CANCELLING
----------------------
"Every morning" and "on the first of the month" are most of what anyone
actually wants from a reminder, so a row can carry a recurrence: an
interval, a unit, a remaining count and an optional end date. A series
keeps exactly one pending row at a time and books the next occurrence
when the current one is delivered — or when it expires unsent, because a
machine that was off for two days must not silently end a daily series.
That roll-forward deliberately doesn't go through `_schedule`: the
pending quotas there exist to stop one correspondent booking fifty
reminders, and a series replacing its own single row is not that.

Missed occurrences are skipped rather than queued: the next due time is
advanced past now and at most the current one goes out, so two days off
the air costs a daily series two messages instead of delivering them all
at once over breakfast. Each occurrence is computed from the series
anchor rather than from the previous one, so a monthly reminder booked
for the 31st doesn't slide permanently to the 28th the first time it
passes through February.

And a repeating reminder has to be stoppable, which a one-shot never
really needed. `list_reminders` and `cancel_reminder` sit alongside
`schedule_reminder` on the reply path, scoped in code to the
correspondent's own address exactly the way scheduling is: nobody can
see, or cancel, a reminder addressed to anyone but themselves.

Reminders don't consume the outbound daily budget. That budget exists to
stop Indi from deciding on its own to write to people; a reminder was
asked for.

THE NEWSLETTER
--------------
A fourth thing a heartbeat can be for, and the only one that takes the
choice away from the model: if the day's newsletter issue is owed, that
run's job is to write and send it, and nothing else is offered. The
mechanism, the storage and the reasons live in newsletter.py; all this
file does is ask, first thing, whether today's issue is outstanding.

THE AGENDA
----------
Planned work agreed with the operator — a running order of newsletter
topics, say — lives in agenda.py. `_context` shows the operator's pending
items to the ordinary heartbeat, and the newsletter run is handed the
pending topics as its running order. With an empty agenda, neither run
changes at all.

Storage deliberately reuses SheetStore's generic tab primitives rather
than adding schema to sheets_queue.py, so this feature is one file.

Everything here is mail the recipient did not just ask for, so all of it
runs through the terms gate: the heartbeat is only shown correspondents
who have accepted, `_send` checks again before anything leaves, and a
due reminder for someone who has not accepted is held rather than sent.
See terms_gate.py.
"""

from __future__ import annotations

import calendar
import datetime
import clock
import logging
import os
import uuid

import agenda
import followups
import costs
import gmail_io
import newsletter
import sheets_queue
import terms_gate

log = logging.getLogger("indi.outbound")

SEND_TOOL = "send_new_message"
JOURNAL_TOOL = "write_journal_entry"
REMINDER_TOOL = "schedule_reminder"
LIST_REMINDERS_TOOL = "list_reminders"
CANCEL_REMINDER_TOOL = "cancel_reminder"
NOTHING_TOOL = "do_nothing"

# -- reminders ---------------------------------------------------------

REMINDERS_TAB = "Reminders"
# The recurrence columns are appended, never inserted, for two reasons.
# `_ensure_headers` upgrades a deployed sheet by adding whatever columns
# it is missing at the end, so rows written before repeats existed keep
# their values and read back with these blank — which is precisely how a
# one-shot reminder is spelled, and is the whole of the migration.
# `_set_reminder_status` also writes the status..sent_at range by index,
# so nothing may move above it either.
REMINDERS_HEADERS = [
    "due_at", "recipient", "subject", "body", "status", "created_at", "sent_at",
    "reminder_id", "series_id", "repeat_every", "repeat_unit",
    "repeat_count_left", "repeat_until", "repeat_anchor",
]

STATUS_SCHEDULED = "scheduled"
STATUS_SENT = "sent"
STATUS_EXPIRED = "expired"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

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

# Recurrence. Units are whole-numbered, so the shortest interval anyone
# can ask for is an hour; anything finer is a mailing list, not a
# reminder.
REPEAT_UNITS = ("hours", "days", "weeks", "months", "years")
# The hard ceiling on how many messages one series may ever send. It
# applies even when the caller gives no count and no end date, because
# the failure mode of an automated sender is not sending too few.
MAX_OCCURRENCES_PER_SERIES = 365
# A stop on the catch-up walk. It should never bind — the estimate in
# `_estimate_index` lands within a step or two — but an unbounded loop
# over dates is not something to leave running on a timer.
MAX_CATCHUP_STEPS = 1000

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


def repeat_properties() -> dict:
    """The repeat_* arguments, identical on both copies of the scheduling
    tool. Written out honestly, bounds included: a model that knows the
    limits asks for something inside them instead of promising a person
    a daily reminder forever and then reading a refusal."""
    return {
        "repeat_every": {
            "type": "integer",
            "description": (
                "Optional. With repeat_unit, makes this a repeating "
                "reminder: 'every 3 days' is repeat_every 3, repeat_unit "
                "days. Whole numbers of at least 1; the shortest interval "
                "possible is one hour. Leave both out for a single message. "
                "Everything is held in UTC, so a repeat keeps the same UTC "
                "time and lands an hour off someone's local clock either side "
                "of a daylight-saving change — worth saying if the exact hour "
                "matters to them."
            ),
        },
        "repeat_unit": {
            "type": "string",
            "enum": list(REPEAT_UNITS),
            "description": (
                "Optional, and required if repeat_every is given. Months "
                "and years keep the day of the month, clamped where it "
                "doesn't exist: the 31st becomes the 28th in February and "
                "is the 31st again in March."
            ),
        },
        "repeat_count": {
            "type": "integer",
            "description": (
                "Optional. How many messages the series sends in total, "
                "including the first. At least 2 and at most "
                f"{MAX_OCCURRENCES_PER_SERIES}, which is also the cap "
                "applied when you give neither this nor repeat_until — no "
                "series runs forever."
            ),
        },
        "repeat_until": {
            "type": "string",
            "description": (
                "Optional. ISO 8601 with a UTC offset, like due_at. The "
                "series stops after this; it must be in the future and "
                f"within {MAX_DAYS_AHEAD} days."
            ),
        },
    }


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
                "rather than sent late. It can repeat — see the repeat_* "
                "fields — and a repeat that was missed while nothing was "
                "running resumes at its next occurrence rather than sending "
                "the backlog. Same recipient rule as sending: "
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
                    **repeat_properties(),
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
    return clock.now()


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


# -- recurrence arithmetic ---------------------------------------------
#
# Written by hand rather than pulled from dateutil: a new dependency in
# the deploy in exchange for a dozen lines of month arithmetic is a poor
# trade, and the two cases that actually go wrong — the 31st of a short
# month and the 29th of February — are easier to test than to argue
# about.

def _days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _add_months(base: datetime.datetime, months: int) -> datetime.datetime:
    """`base` shifted by whole months, clamping the day of the month.

    31 January plus one month is 28 February — 29 in a leap year —
    because there is no 31st to land on. The clamp applies to the result
    only; it is never written back into the series anchor, which is what
    keeps the following month from inheriting the shortened day."""
    total = base.month - 1 + months
    year = base.year + total // 12
    month = total % 12 + 1
    return base.replace(year=year, month=month,
                        day=min(base.day, _days_in_month(year, month)))


def _occurrence(anchor: datetime.datetime, every: int, unit: str,
                index: int) -> datetime.datetime:
    """The `index`-th occurrence of a series, counted from its anchor.

    Always from the anchor, never from the previous occurrence. Stepping
    month by month from the last due date would move the 31st of January
    to the 28th of February and then leave the series on the 28th for
    good; from the anchor, March is the 31st again."""
    if unit == "hours":
        return anchor + datetime.timedelta(hours=every * index)
    if unit == "days":
        return anchor + datetime.timedelta(days=every * index)
    if unit == "weeks":
        return anchor + datetime.timedelta(weeks=every * index)
    if unit == "months":
        return _add_months(anchor, every * index)
    if unit == "years":
        return _add_months(anchor, every * 12 * index)
    raise ValueError(f"unknown repeat unit {unit!r}")


def _estimate_index(anchor: datetime.datetime, every: int, unit: str,
                    after: datetime.datetime) -> int:
    """A starting guess at or below the right index, so `next_occurrence`
    walks a step or two rather than counting up from the anchor: an
    hourly series a year in is nine thousand steps otherwise."""
    if after <= anchor:
        return 0
    if unit in ("hours", "days", "weeks"):
        return max(0, int((after - anchor) // datetime.timedelta(**{unit: every})))
    months = every * (12 if unit == "years" else 1)
    gap = (after.year - anchor.year) * 12 + (after.month - anchor.month)
    return max(0, gap // months)


def next_occurrence(anchor: datetime.datetime, every: int, unit: str,
                    after: datetime.datetime) -> datetime.datetime | None:
    """The first occurrence of a series strictly after `after`.

    Passing `now` in here is the whole of the catch-up rule: a series
    whose due time is three days in the past lands on the next
    occurrence from now, and the ones in between are skipped rather than
    sent in a burst."""
    if anchor is None or every < 1 or unit not in REPEAT_UNITS:
        return None
    index = _estimate_index(anchor, every, unit, after)
    for _ in range(MAX_CATCHUP_STEPS):
        when = _occurrence(anchor, every, unit, index)
        if when > after:
            return when
        index += 1
    # Only reachable if the estimate is badly wrong. Ending the series is
    # the safe way to fail: nothing more goes out.
    log.warning("Gave up advancing an every-%s-%s series past %s",
                every, unit, after.isoformat())
    return None


def _as_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _normalise_unit(value: str) -> str:
    """'Day', 'days', 'DAYS' -> 'days'; anything else -> ''."""
    unit = (value or "").strip().lower()
    if unit and not unit.endswith("s"):
        unit += "s"
    return unit if unit in REPEAT_UNITS else ""


def _repeat_of(row: dict) -> dict | None:
    """The recurrence stored on a row, or None if it is a one-shot.

    Every row written before repeats existed has these columns blank —
    or missing entirely, if the sheet has not been re-read since
    `_ensure_headers` extended it — and reads back here as a one-shot,
    which is exactly what it is."""
    every = _as_int(row.get("repeat_every", ""))
    unit = _normalise_unit(row.get("repeat_unit", ""))
    if not unit or every is None or every < 1:
        return None
    return {
        "every": every,
        "unit": unit,
        "count_left": max(0, _as_int(row.get("repeat_count_left", "")) or 0),
        "until": parse_when(row.get("repeat_until", "")),
        # Falling back to the row's own due time matters for a row that
        # was rolled forward by an older version of this code, or edited
        # by hand: a series without an anchor is still a series.
        "anchor": (parse_when(row.get("repeat_anchor", ""))
                   or parse_when(row.get("due_at", ""))),
    }


def describe_repeat(row: dict) -> str:
    """How a recurrence reads to a person: 'every 3 days, 4 more'."""
    repeat = _repeat_of(row)
    if repeat is None:
        return "one-off"
    every, unit = repeat["every"], repeat["unit"]
    text = f"every {unit[:-1]}" if every == 1 else f"every {every} {unit}"
    text += f", {repeat['count_left']} more" if repeat["count_left"] else ", last one"
    if repeat["until"]:
        text += f", ending by {repeat['until'].date().isoformat()}"
    return text


def _new_id(prefix: str) -> str:
    """Short, unguessable-enough ids. They are only ever quoted back by
    someone who was shown them, and the scope check does the real work."""
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def reminder_ref(row: dict) -> str:
    """How a reminder is named to a correspondent, and matched coming back.

    Rows written before this change have no reminder_id and still have to
    be listable and cancellable, so the sheet's own row number is the
    fallback — stable for as long as the row exists, which is forever."""
    return (row.get("reminder_id") or "").strip() or f"row{row.get('_row', '?')}"


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


def _roll_series_forward(store, row: dict, now: datetime.datetime) -> str:
    """Book the next occurrence of a recurring reminder, if there is one.

    Called when an occurrence is sent and also when one expires unsent:
    two days with the machine off must not quietly end a daily series,
    and the expired row is still the series' only pending row.

    Deliberately not `_schedule`. The pending quotas there exist to stop
    one correspondent booking fifty reminders at once; a series holds
    exactly one pending row and this one replaces the row that is
    finishing, so running it through those checks would mean a busy queue
    silently killing a series someone is relying on.

    Returns a short note for the run's outcome list, or "" if the row
    wasn't recurring in the first place."""
    repeat = _repeat_of(row)
    if repeat is None:
        return ""

    recipient = (row.get("recipient") or "").strip().lower()
    if repeat["count_left"] < 1:
        return f"the repeating reminder for {recipient} has finished its run"

    due = parse_when(row.get("due_at", "")) or now
    # max(due, now) is the catch-up rule: never book a time already past,
    # however long this row sat there.
    nxt = next_occurrence(repeat["anchor"], repeat["every"], repeat["unit"],
                          max(due, now))
    if nxt is None:
        return (f"could not work out the next occurrence for {recipient}; "
                "the series ends here")
    if repeat["until"] and nxt > repeat["until"]:
        return f"the repeating reminder for {recipient} has reached its end date"
    if nxt - now > datetime.timedelta(days=MAX_DAYS_AHEAD):
        return (f"the next occurrence for {recipient} would be more than "
                f"{MAX_DAYS_AHEAD} days away; the series ends here")

    _append_reminder(store, {
        "due_at": nxt.isoformat(),
        "recipient": recipient,
        "subject": row.get("subject", ""),
        "body": row.get("body", ""),
        "status": STATUS_SCHEDULED,
        "created_at": now.isoformat(),
        "sent_at": "",
        "reminder_id": _new_id("r"),
        # A series that somehow lost its id gets one now rather than
        # becoming uncancellable as a group.
        "series_id": (row.get("series_id") or "").strip() or _new_id("s"),
        "repeat_every": str(repeat["every"]),
        "repeat_unit": repeat["unit"],
        # One per message actually booked, not one per interval that went
        # by: the count is a budget on how much mail a series may send,
        # and occurrences skipped while the machine was off sent none.
        "repeat_count_left": str(repeat["count_left"] - 1),
        "repeat_until": repeat["until"].isoformat() if repeat["until"] else "",
        "repeat_anchor": repeat["anchor"].isoformat(),
    })
    log.info("Rolled the series for %s forward to %s (%d left).",
             recipient, nxt.isoformat(), repeat["count_left"] - 1)
    return f"next occurrence for {recipient} booked for {nxt.isoformat()}"


def deliver_due_reminders(cfg, gmail, store, people=None) -> list[str]:
    """Send every reminder that has come due. Called at the top of each
    heartbeat, before the model is asked anything — this is the part that
    has to work whether or not a model is feeling helpful.

    A reminder is still mail arriving unasked-for on the day, so it is
    subject to the terms gate like everything else. One scheduled before
    the gate existed, for someone who has not accepted, is left standing
    rather than failed: they have until the grace window closes to reply
    "I AGREE", and then it expires on its own."""
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
            # A missed occurrence must not take the rest of the series
            # with it. This is the case the roll-forward exists for: the
            # machine was off, today's message is past saving, tomorrow's
            # is not.
            rolled = _roll_series_forward(store, row, now)
            if rolled:
                outcomes.append(rolled)
            continue

        # After the expiry check above, deliberately: a reminder held
        # because its recipient never accepted the terms must still age
        # out, or it would sit in the queue being reconsidered forever.
        if not terms_gate.may_send_unsolicited(recipient, people):
            outcomes.append(
                f"reminder for {recipient} held: they have not accepted the "
                "Terms of Service"
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
        rolled = _roll_series_forward(store, row, now)
        if rolled:
            outcomes.append(rolled)

    return outcomes


def _read_repeat_request(payload: dict, due: datetime.datetime,
                         now: datetime.datetime) -> tuple[dict | None, str]:
    """Read the repeat_* arguments of a scheduling call.

    Returns (columns, refusal): the block of columns to store — empty for
    an ordinary one-shot — or a "refused: ..." string when the request
    can't be honoured as asked. Refusing out loud matters more here than
    anywhere else in this file, because the caller is a model about to
    write a person a sentence describing what it just arranged; an
    "every 3 days" quietly downgraded to a single message is a promise
    nobody kept."""
    raw_every = payload.get("repeat_every")
    raw_unit = payload.get("repeat_unit")
    asked_for_a_repeat = any(
        str(v or "").strip() for v in
        (raw_every, raw_unit, payload.get("repeat_count"),
         payload.get("repeat_until"))
    )
    if not asked_for_a_repeat:
        return {}, ""

    every = _as_int(raw_every)
    unit = _normalise_unit(str(raw_unit or ""))
    if every is None or not unit:
        return None, ("refused: a repeating reminder needs both repeat_every "
                      "(a whole number) and repeat_unit (one of "
                      + ", ".join(REPEAT_UNITS) + ")")
    if every < 1:
        # Which is also the one-hour floor: hours is the smallest unit,
        # so an interval of at least 1 is an interval of at least an hour.
        return None, ("refused: repeat_every must be a whole number of at "
                      "least 1, and the shortest interval allowed is one hour")

    count = _as_int(payload.get("repeat_count")) \
        if str(payload.get("repeat_count") or "").strip() else None
    if count is not None:
        if count < 2:
            return None, ("refused: repeat_count is how many messages the "
                          "series sends in total and must be at least 2; for "
                          "a single message, leave the repeat fields out")
        if count > MAX_OCCURRENCES_PER_SERIES:
            return None, (f"refused: {count} occurrences is more than the "
                          f"{MAX_OCCURRENCES_PER_SERIES} one series may send")

    until = None
    until_text = str(payload.get("repeat_until") or "").strip()
    if until_text:
        until = parse_when(until_text)
        if until is None:
            return None, (f"refused: could not read {until_text!r} as the date "
                          "the repeat should stop")
        if until <= now:
            return None, (f"refused: the repeat's end {until.isoformat()} is "
                          "not in the future")
        if until - now > datetime.timedelta(days=MAX_DAYS_AHEAD):
            return None, (f"refused: the repeat's end is more than "
                          f"{MAX_DAYS_AHEAD} days ahead")
        if until < due:
            return None, ("refused: the repeat ends before the first reminder "
                          "is even due")

    # No end condition still gets one. A series nobody bounded is the
    # thing this constant exists to stop.
    total = count or MAX_OCCURRENCES_PER_SERIES
    return {
        "series_id": _new_id("s"),
        "repeat_every": str(every),
        "repeat_unit": unit,
        # What is left *after* the occurrence being scheduled now.
        "repeat_count_left": str(total - 1),
        "repeat_until": until.isoformat() if until else "",
        # The anchor is the first due time, and every later occurrence is
        # counted from it rather than from its predecessor.
        "repeat_anchor": due.isoformat(),
    }, ""


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

    repeat_fields, refusal = _read_repeat_request(payload, due, now)
    if refusal:
        return refusal

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

    record = {
        "due_at": due.isoformat(),
        "recipient": to_addr,
        "subject": subject[:500],
        "body": body[: cfg.max_body_chars],
        "status": STATUS_SCHEDULED,
        "created_at": now.isoformat(),
        "sent_at": "",
        "reminder_id": _new_id("r"),
        "series_id": "",
        "repeat_every": "",
        "repeat_unit": "",
        "repeat_count_left": "",
        "repeat_until": "",
        "repeat_anchor": "",
    }
    record.update(repeat_fields)
    repeat_note = f", repeating {describe_repeat(record)}" if repeat_fields else ""

    if cfg.dry_run:
        print(f"\n[dry-run] reminder for {due.isoformat()}{repeat_note}\n"
              f"TO: {to_addr}\nSUBJECT: {subject}\n{'-'*60}\n{body}\n")
        return "dry run (reminder not scheduled)"

    _append_reminder(store, record)
    log.info("Reminder scheduled for %s at %s%s.", to_addr, due.isoformat(),
             repeat_note)
    return (f"reminder scheduled for {to_addr} at {due.isoformat()}"
            f"{repeat_note} (id {record['reminder_id']})")


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
            "depend on anything you do later. It can also repeat — see the "
            "repeat_* fields — in which case one occurrence is booked at a "
            "time and the next is arranged as each goes out, so a repeat that "
            "was missed while nothing was running picks up at its next "
            "occurrence rather than delivering the backlog at once. If a "
            "reminder cannot be delivered within "
            f"{REMINDER_GRACE_HOURS} hours of the due time it is dropped rather "
            "than sent uselessly late. It cannot be edited afterwards, but it "
            f"can be cancelled: {sender} can ask, and you have "
            f"{LIST_REMINDERS_TOOL} and {CANCEL_REMINDER_TOOL} for that. The "
            "tool result tells you whether it was actually scheduled — if it "
            "was refused, say so plainly rather than implying the reminder "
            "exists."
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
                             "later, and you won't see it again before it goes. "
                             "For a repeating reminder this same text arrives "
                             "every time, so don't write it as if it were the "
                             "only one."
                         )},
                **repeat_properties(),
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
# SEEING AND CANCELLING WHAT IS BOOKED
# ---------------------------------------------------------------------------
#
# Scheduling and cancelling are scoped the same way and for the same
# reason: the recipient is fixed in code to the person being replied to.
# Neither tool takes an address, so there is nothing for an injected
# email to point at someone else — "cancel every reminder Steve has" is
# not a sentence this surface can act on, whoever writes it.

def reply_list_reminders_schema(sender: str) -> dict:
    return {
        "name": LIST_REMINDERS_TOOL,
        "description": (
            f"List the reminders currently scheduled for {sender}, the person "
            "you are replying to. Takes no arguments: it can only ever show "
            "their own, because the address is fixed in code and not "
            "something you or they can choose. Use it when they ask what is "
            "booked, when they want to change something — a reminder cannot "
            "be edited, so that means cancelling and scheduling again — and "
            f"before {CANCEL_REMINDER_TOOL}, which needs the id this returns."
        ),
        "input_schema": {"type": "object", "properties": {}},
    }


def reply_cancel_reminder_schema(sender: str) -> dict:
    return {
        "name": CANCEL_REMINDER_TOOL,
        "description": (
            f"Cancel a pending reminder addressed to {sender}. Give the id "
            f"from {LIST_REMINDERS_TOOL}. If the reminder repeats, the whole "
            "series is cancelled, not just the next one — that is almost "
            "always what 'stop reminding me' means, so say plainly in your "
            "reply that the repeat has stopped. Only their own reminders can "
            "be cancelled; there is no way to reach anyone else's. Check the "
            "result before telling them it is done."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reminder_id": {
                    "type": "string",
                    "description": (
                        f"The id as {LIST_REMINDERS_TOOL} printed it. Don't "
                        "invent one; list first if you don't have it."
                    ),
                },
            },
            "required": ["reminder_id"],
        },
    }


def _own_pending_reminders(store, sender: str) -> list[dict]:
    """The pending rows this correspondent is allowed to know about.

    The one line that makes both tools safe to offer to anyone Indi
    replies to. It is a filter on the stored recipient, not a check of
    anything the model passed in, so there is no argument to get wrong."""
    return [row for row in pending_reminders(store)
            if (row.get("recipient") or "").strip().lower() == sender]


def handle_reply_list_reminders(cfg, store, sender: str) -> tuple[str, bool]:
    sender = (sender or "").strip().lower()
    if store is None:
        return "Reminders are unavailable: no spreadsheet store on this run.", True
    if not sender or "@" not in sender:
        return f"Reminders are unavailable: no usable sender address ({sender!r}).", True

    rows = _own_pending_reminders(store, sender)
    if not rows:
        return "Nothing is scheduled for you at the moment.", False
    lines = [
        f"{reminder_ref(row)} — due {row.get('due_at', '')} — "
        f"{(row.get('subject') or 'Reminder')[:80]} — {describe_repeat(row)}"
        for row in rows
    ]
    return (f"{len(lines)} reminder(s) scheduled for {sender}:\n"
            + "\n".join(lines)), False


def handle_reply_cancel_reminder(cfg, store, sender: str,
                                 payload: dict) -> tuple[str, bool]:
    """Cancel one of this correspondent's own reminders, series and all."""
    sender = (sender or "").strip().lower()
    if store is None:
        return "Reminders are unavailable: no spreadsheet store on this run.", True
    if not sender or "@" not in sender:
        return f"Reminders are unavailable: no usable sender address ({sender!r}).", True

    wanted = str((payload or {}).get("reminder_id") or "").strip().lstrip("#")
    if not wanted:
        return ("Cancelling needs the reminder's id — list them first.", True)

    rows = _own_pending_reminders(store, sender)
    key = wanted.lower()
    # A bare number is accepted as well as "row12": legacy rows are named
    # by their sheet row, and a person reading the list back out of an
    # email will not always copy the prefix.
    match = next((row for row in rows
                  if reminder_ref(row).lower() in (key, f"row{key}")), None)
    if match is None:
        # Deliberately the same answer whether the id doesn't exist or
        # belongs to somebody else. There is nothing to learn here about
        # other people's mail.
        return (f"You have no pending reminder with the id {wanted!r}.", True)

    series = (match.get("series_id") or "").strip()
    targets = ([row for row in rows
                if (row.get("series_id") or "").strip() == series]
               if series else [match])

    if cfg.dry_run:
        print(f"\n[dry-run] would cancel {len(targets)} reminder(s) for {sender}\n")
        return "dry run (nothing cancelled)", False

    for row in targets:
        # No timestamp: sent_at means what it says, and a cancelled
        # reminder was never sent. The status is the whole record.
        _set_reminder_status(store, row, STATUS_CANCELLED)
    log.info("Cancelled %d reminder(s) for %s.", len(targets), sender)

    repeated = _repeat_of(match) is not None
    when = match.get("due_at", "")
    if repeated:
        return (f"Cancelled the repeating reminder due {when} "
                f"({describe_repeat(match)}); nothing further in that series "
                "will be sent."), False
    return f"Cancelled the reminder due {when}.", False


# ---------------------------------------------------------------------------
# HEARTBEAT
# ---------------------------------------------------------------------------

def _context(cfg, store) -> str:
    """What Indi knows going into a heartbeat."""
    recent = store.recent_messages(8)
    replies = store.responses_for(recent)
    parts = []

    if recent:
        parts.append("=== RECENT CORRESPONDENCE ===\n"
                     + sheets_queue.format_exchanges(recent, replies, max_chars_each=800))

    journal = store.recent_journal(limit=cfg.heartbeat_journal_entries)
    if journal:
        parts.append("=== YOUR RECENT JOURNAL ENTRIES ===\n" + "\n\n".join(
            f"[{row.get('created_at', '')}]\n{row.get('entry', '')}" for row in journal
        ))

    sent = store.recent_outbound(5)
    if sent:
        parts.append("=== MESSAGES YOU STARTED RECENTLY ===\n" + "\n\n".join(
            f"[{r.get('created_at','')}] to {r.get('recipient','')} — "
            f"{r.get('subject','')}\n{(r.get('body','') or '')[:400]}" for r in sent
        ))

    upcoming = pending_reminders(store)[:10]
    if upcoming:
        parts.append("=== REMINDERS STILL PENDING ===\n" + "\n".join(
            f"[due {r.get('due_at','')}, {describe_repeat(r)}] to "
            f"{r.get('recipient','')} — {r.get('subject','')}" for r in upcoming
        ))

    # The operator's agenda (agenda.py). Empty means no section at all, so
    # a heartbeat with no plan sees exactly what it saw before. Not in
    # `_context_for`: an agenda is its owner's, not a recipient's.
    plan = agenda.heartbeat_section(store, terms_gate.OPERATOR_ADDRESS)
    if plan:
        parts.append(plan)

    if not parts:
        parts.append("(No correspondence yet. This is a quiet start.)")
    return "\n\n".join(parts)


def _context_for(cfg, store, recipients) -> str:
    """What Indi may draw on when writing to these people (Phase 2): only
    mail every one of them has already seen, the replies they saw, what
    Indi itself wrote to them, and their pending reminders. No journal —
    it is written from everyone's mail — and nothing from anyone else.

    Used for the message a heartbeat decides to send and for the
    newsletter, whose audience is its whole subscriber list."""
    wanted = {r.lower() for r in recipients if r}
    parts = []
    recent = store.recent_messages(30)
    if getattr(store, "supports_audience", False):
        keep = set(store._cleared([r["gmail_msg_id"] for r in recent], wanted))
        recent = [r for r in recent if r["gmail_msg_id"] in keep][-8:]
        replies = store.responses_for(recent, cleared_for=wanted)
    else:
        # Without the audience rule, only a person's own mail is theirs.
        only = next(iter(wanted)) if len(wanted) == 1 else None
        recent = [r for r in recent
                  if only and (r.get("sender_addr") or "").lower() == only][-8:]
        replies = store.responses_for(recent) if recent else {}
    if recent:
        parts.append("=== CORRESPONDENCE THEY HAVE ALREADY SEEN ===\n"
                     + sheets_queue.format_exchanges(recent, replies, max_chars_each=800))

    sent = [r for r in store.recent_outbound(10)
            if {a.strip().lower() for a in (r.get("recipient") or "").split(",") if a.strip()}
            and wanted <= {a.strip().lower() for a in (r.get("recipient") or "").split(",")}]
    if sent:
        parts.append("=== MESSAGES YOU STARTED WITH THEM ===\n" + "\n\n".join(
            f"[{r.get('created_at','')}] {r.get('subject','')}\n"
            f"{(r.get('body','') or '')[:400]}" for r in sent[-5:]))

    upcoming = [r for r in pending_reminders(store)
                if (r.get("recipient") or "").lower() in wanted][:10]
    if upcoming:
        parts.append("=== THEIR REMINDERS STILL PENDING ===\n" + "\n".join(
            f"[due {r.get('due_at','')}, {describe_repeat(r)}] — {r.get('subject','')}"
            for r in upcoming))
    if not parts:
        parts.append("(Nothing on record that they have seen.)")
    return "\n\n".join(parts)


COMPOSE_SUFFIX = """

--- WRITING TO {who} ---
You decided on this heartbeat to {action} {who}. Below is everything you
may draw on for it: only what {who} has already seen. Your other
correspondence is deliberately not shown — information one person gave you
is theirs, and does not reach anyone else through you. Write from what is
here, or call do_nothing if, seen this way, there is no reason after all.
Call exactly one tool."""


def _compose_for(client, cfg, store, system_prompt: str, model: str,
                 tool_name: str, recipient: str, budget_note: str):
    """Phase 2: the second, per-recipient step of a heartbeat. The first
    step chooses what to do over everyone's recent mail; its draft is
    discarded, and the message is written here from `_context_for` alone.
    Returns the tool call to carry out, or None for nothing."""
    action = "write to" if tool_name == SEND_TOOL else "set a reminder for"
    tools = [t for t in schemas(cfg, [recipient], False)
             if t["name"] in (tool_name, NOTHING_TOOL)]
    with client.messages.stream(
        model=model,
        max_tokens=cfg.max_tokens,
        system=system_prompt + HEARTBEAT_SYSTEM_SUFFIX
               + COMPOSE_SUFFIX.format(who=recipient, action=action),
        tools=tools,
        messages=[{"role": "user",
                   "content": _context_for(cfg, store, {recipient}) + budget_note}],
    ) as streamed:
        response = streamed.get_final_message()
    costs.record("heartbeat", model,
                 costs.tokens_from_usage(getattr(response, "usage", None)))
    calls = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
    if not calls or calls[0].name != tool_name:
        return None
    call = calls[0]
    target = (call.input.get("to") or call.input.get("recipient") or "").strip().lower()
    if target and target != recipient:
        log.warning("Per-recipient step tried to address %s instead of %s; ignored.",
                    target, recipient)
        return None
    return call


def run_heartbeat(client, cfg, gmail, store, system_prompt: str,
                  self_addrs: set[str], people=None) -> str:
    """One heartbeat. Returns a short outcome string."""
    # Reminders first, and unconditionally: they were asked for, so they
    # don't depend on the model being called at all, and they aren't
    # governed by the initiative budgets below.
    try:
        reminder_outcomes = deliver_due_reminders(cfg, gmail, store, people)
    except Exception:
        log.error("Reminder delivery failed", exc_info=True)
        reminder_outcomes = ["reminder delivery raised an error (see log)"]
    prefix = ("; ".join(reminder_outcomes) + "; ") if reminder_outcomes else ""

    # Follow-ups Indi planned for itself (followups.py) that are now due:
    # each is put in front of the model, which must send or skip. Their own
    # daily budget; not the initiative budget below.
    followups.SENT_THIS_RUN.clear()
    if followups.enabled():
        try:
            done = followups.run_due(client, cfg, gmail, store, system_prompt,
                                     self_addrs, people, heartbeat_model(cfg))
            if done:
                prefix += "; ".join(done) + "; "
        except Exception:
            log.error("Follow-ups failed", exc_info=True)

    # The newsletter, before the budgets below and before the model is
    # offered the ordinary menu. A day's issue is owed to people who
    # asked for it, so — like a reminder — it is neither stopped by the
    # self-initiative budget nor counted against it; see newsletter.py
    # for the limits it has instead. When an issue is already recorded
    # for today, nothing here runs and the heartbeat proceeds exactly as
    # it did before this existed.
    try:
        owed, why = newsletter.due(cfg, store, people)
        if owed and clock.staging():
            # A staging copy writes only to test accounts; its subscriber list
            # starts with the real operator. No newsletter there.
            owed, why = False, "staging copy: no newsletter"
        if owed:
            log.info("Newsletter: %s", why)
            # Written for everyone on the list, so drawn only from what
            # everyone on the list has seen (Phase 2).
            subscribers = set(newsletter.list_subscribers(store, cfg))
            # The agreed running order, if there is one (agenda.py). With
            # none, run_issue is called exactly as it always was.
            topics = agenda.offered(store, terms_gate.OPERATOR_ADDRESS,
                                    agenda.KIND_NEWSLETTER_TOPIC)[:agenda.MAX_SHOWN]
            extra = ({"agenda_owner": terms_gate.OPERATOR_ADDRESS, "topics": topics}
                     if topics else {})
            return prefix + newsletter.run_issue(
                client, cfg, gmail, store, system_prompt,
                heartbeat_model(cfg), people,
                context=_context_for(cfg, store, subscribers),
                **extra,
            )
        log.debug("No newsletter this run: %s", why)
    except Exception:
        # A broken newsletter must not cost the heartbeat its ordinary
        # run — the reply-adjacent work below is the part people notice.
        log.error("The newsletter check failed", exc_info=True)

    sent_today = store.outbound_today()
    journal_today = store.journal_today()

    if sent_today >= cfg.outbound_max_per_day and journal_today >= cfg.journal_max_per_day:
        return prefix + "budgets exhausted for today; skipping"

    # Only people who have accepted the terms are even named to the
    # model. `_send` checks again before anything goes out — this is the
    # cheaper half of the same rule, and it keeps Indi from composing a
    # message it is not allowed to send.
    recipients = sorted(
        addr for addr in (store.known_correspondents() - self_addrs)
        if terms_gate.may_send_unsolicited(addr, people)
    )
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

    if call.name in (REMINDER_TOOL, SEND_TOOL):
        # Chosen over everyone's mail; written over the recipient's alone.
        chosen = (call.input.get("to") or call.input.get("recipient") or "").strip().lower()
        if not chosen:
            return prefix + "no recipient given; skipped"
        if call.name == SEND_TOOL and chosen in followups.SENT_THIS_RUN:
            # The follow-up that just went to them already said it.
            return prefix + f"not writing to {chosen} again: a follow-up just went"
        call = _compose_for(client, cfg, store, system_prompt, model, call.name,
                            chosen, budget_note)
        if call is None:
            return prefix + f"decided against writing to {chosen} on reflection"

    if call.name == REMINDER_TOOL:
        return prefix + _schedule(cfg, store, call.input, recipients, self_addrs)

    if call.name == SEND_TOOL:
        return prefix + _send(client, cfg, gmail, store, call.input, recipients,
                              self_addrs, sent_today, people)

    return prefix + f"unknown action: {call.name}"


def _send(client, cfg, gmail, store, payload: dict, recipients: list[str],
          self_addrs: set[str], sent_today: int, people=None) -> str:
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

    if not terms_gate.may_send_unsolicited(to_addr, people):
        # The terms gate, checked here as well as in the recipient list
        # above, because INDI_OUTBOUND_ALLOW_NEW_RECIPIENTS can put an
        # address in front of the model that never went through it.
        log.warning("Refused outbound to %s: has not accepted the Terms.", to_addr)
        return (f"refused: {to_addr} has not accepted the Terms of Service, so "
                "nothing can be sent to them until they do")

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
        result = gmail.users().messages().send(userId="me", body=mime).execute()
        outcome = "sent"
        message_id = (result or {}).get("id", "")
    else:
        result = gmail.users().drafts().create(userId="me", body={"message": mime}).execute()
        outcome = "drafted"
        message_id = ((result or {}).get("message") or {}).get("id", "")

    store.append_outbound(to_addr, subject, body, rationale, message_id=message_id)
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
