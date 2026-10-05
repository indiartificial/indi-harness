"""
followups.py

Follow-ups: Indi deciding, while it replies, to come back to a thread
later — and then, when the time comes, being made to look again and either
write or say why not.

HOW IT DIFFERS FROM WHAT WAS THERE
----------------------------------
A reminder is a fixed email promised to someone who asked for it, sent by
code. An agenda item is a note to self that a later run may notice. A
follow-up is Indi's own intention, tied to one thread, and *due*: on the
first heartbeat after its time, the harness puts it in front of the model
with the thread as it stands, and the model must either send a follow-up
in that thread or skip it with a reason. Nothing is left to chance noticing.

PRIVACY
-------
The note is written in the reply path, whose context is already limited
to what the reply's recipients have seen (Phase 2), and it is kept with
exactly those recipients. When it fires, the thread is shown filtered the
same way (audience.py), the follow-up goes only to those recipients — and
only those who still accept the terms — and, with consultation on, it goes
through the same pre-send check as a reply. A follow-up can't carry one
person's information to another.

LIMITS
------
At most one pending follow-up per thread (a new one replaces the old), at
most MAX_PENDING pending overall, due between an hour and MAX_DAYS ahead,
and at most INDI_FOLLOWUPS_MAX_PER_DAY sent per rolling day (default 5) —
its own budget, separate from the heartbeat's. A follow-up never schedules
another. One that can't be acted on (an API error, the check unavailable)
is tried on up to MAX_ATTEMPTS heartbeats, then dropped and logged.

People can list and cancel the follow-ups that concern them, in an email
to Indi alone.

This module decides what Indi sends and to whom, so it is protected from
self-modification.
"""

from __future__ import annotations

import datetime
import clock
import logging
import uuid

import audience
import costs
import gmail_io
import gmail_reads

log = logging.getLogger("indi.followups")

TAB = "Follow-ups"
HEADERS = ["followup_id", "created_at", "due_at", "thread_id", "msg_id", "owner",
           "recipients", "note", "status", "attempts", "completed_at", "outcome",
           # The ledger entry (ledger.py) this follow-up is about, if any: when
           # that entry is resolved or dropped, the follow-up is cancelled.
           "about",
           # How many times Indi has put it off ("not yet"), at most MAX_POSTPONES.
           "postponed"]
PENDING, SENT, SKIPPED, CANCELLED, FAILED, REPLACED = (
    "pending", "sent", "skipped", "cancelled", "failed", "replaced")

SCHEDULE_TOOL = "schedule_follow_up"
LIST_TOOL = "list_follow_ups"
CANCEL_TOOL = "cancel_follow_up"
SEND_TOOL = "send_follow_up"
SKIP_TOOL = "skip_follow_up"
POSTPONE_TOOL = "postpone_follow_up"
MAX_POSTPONES = 2
REPLY_TOOL_NAMES = {SCHEDULE_TOOL, LIST_TOOL, CANCEL_TOOL}

MIN_HOURS = 1
MAX_DAYS = 60
MAX_PENDING = 50
MAX_ATTEMPTS = 3
MAX_NOTE_CHARS = 500
PER_RUN = 3
DEFAULT_PER_DAY = 5


def _now() -> datetime.datetime:
    return clock.now()


def enabled() -> bool:
    """INDI_FOLLOWUPS, on unless set to false."""
    return (gmail_reads._setting("INDI_FOLLOWUPS") or "true").lower() not in (
        "0", "false", "no", "off")


def per_day() -> int:
    try:
        return max(0, int(gmail_reads._setting("INDI_FOLLOWUPS_MAX_PER_DAY") or DEFAULT_PER_DAY))
    except ValueError:
        return DEFAULT_PER_DAY


def _parse(value: str) -> datetime.datetime | None:
    try:
        when = datetime.datetime.fromisoformat((value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        try:
            from zoneinfo import ZoneInfo
            when = when.replace(tzinfo=ZoneInfo(gmail_reads._setting("INDI_TIMEZONE") or "UTC"))
        except Exception:
            when = when.replace(tzinfo=datetime.timezone.utc)
    return when


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------

def _rows(store) -> list[dict]:
    store._ensure_tab(store.responses_id, TAB, HEADERS)
    return store._load(store.responses_id, TAB, declared=HEADERS)


def _update(store, row: dict, **fields) -> None:
    from sheets_queue import _col_letter
    row.update(fields)
    n = row.get("_row", -1)
    if n <= 0:
        return
    store.sheets.spreadsheets().values().update(
        spreadsheetId=store.responses_id,
        range=f"{TAB}!A{n}:{_col_letter(len(HEADERS) - 1)}{n}",
        valueInputOption="RAW", body={"values": [[str(row.get(h, "")) for h in HEADERS]]},
    ).execute()


def pending(store) -> list[dict]:
    return [r for r in _rows(store) if r.get("status") == PENDING]


def _recipients(row: dict) -> list[str]:
    return [a.strip() for a in (row.get("recipients") or "").split(",") if a.strip()]


# ---------------------------------------------------------------------------
# scheduling, in the reply path
# ---------------------------------------------------------------------------

def schedule_schema(now: datetime.datetime | None = None) -> dict:
    stamp = (now or _now()).isoformat()
    return {
        "name": SCHEDULE_TOOL,
        "description": (
            "Arrange to come back to THIS thread yourself at a later time. Use it "
            "when you've said you'll check back, or when something here is "
            "waiting on an event and a nudge later would genuinely help (a quote "
            "that should arrive, a decision due Friday). At the time you give, "
            "you'll be shown the thread as it stands and your note, and will "
            "either write a follow-up to the same people or decide it isn't "
            "needed. Not for things someone asked to be reminded about (use "
            "schedule_reminder) and not just to keep a conversation going. One "
            "per thread: scheduling again replaces the earlier one. Mention it "
            f"in your reply if it would help them to know. The current time is {stamp}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "due_at": {"type": "string", "description":
                           "When to look again: ISO 8601 with an offset, between an "
                           f"hour and {MAX_DAYS} days from now."},
                "note": {"type": "string", "description":
                         "A short note to yourself: what to check and why. Only "
                         "what the people on this thread have seen."},
                "about": {"type": "string", "description":
                          "Optional: the id of the open ledger entry ([L-...]) this "
                          "is about. If that entry is settled first, the follow-up "
                          "is cancelled."},
            },
            "required": ["due_at", "note"],
        },
    }


def list_schema(sender: str) -> dict:
    return {"name": LIST_TOOL,
            "description": f"List the follow-ups Indi has planned on threads that include {sender}.",
            "input_schema": {"type": "object", "properties": {}}}


def cancel_schema(sender: str) -> dict:
    return {"name": CANCEL_TOOL,
            "description": (f"Cancel a planned follow-up on a thread that includes {sender}, "
                            "when they ask. Give its id from the list, or \"all\"."),
            "input_schema": {"type": "object",
                             "properties": {"which": {"type": "string"}},
                             "required": ["which"]}}


def schedule(store, msg: dict, recipients, payload: dict, now=None) -> tuple[str, bool]:
    now = now or _now()
    due = _parse(payload.get("due_at", ""))
    note = (payload.get("note") or "").strip()[:MAX_NOTE_CHARS]
    if due is None:
        return "Give due_at as an ISO 8601 time with an offset.", True
    if due < now + datetime.timedelta(hours=MIN_HOURS) - datetime.timedelta(minutes=5):
        return f"Too soon: a follow-up must be at least {MIN_HOURS} hour ahead.", True
    if due > now + datetime.timedelta(days=MAX_DAYS):
        return f"Too far: at most {MAX_DAYS} days ahead.", True
    if not note:
        return "Add a note saying what to check.", True
    thread = msg.get("thread_id", "")
    rows = pending(store)
    for r in rows:
        if r.get("thread_id") == thread:
            _update(store, r, status=REPLACED, completed_at=now.isoformat(),
                    outcome="replaced by a later follow-up")
    if len([r for r in rows if r.get("thread_id") != thread]) >= MAX_PENDING:
        return "Too many follow-ups are already planned; not scheduled.", True
    fid = f"f-{uuid.uuid4().hex[:8]}"
    store._append(store.responses_id, TAB, HEADERS, {
        "followup_id": fid, "created_at": now.isoformat(),
        "due_at": due.astimezone(datetime.timezone.utc).isoformat(),
        "thread_id": thread, "msg_id": msg.get("gmail_msg_id", ""),
        "owner": (msg.get("from_addr") or "").lower(),
        "recipients": ", ".join(sorted({r.lower() for r in recipients if r})),
        "note": note, "status": PENDING, "attempts": "0", "completed_at": "", "outcome": "",
        "about": (payload.get("about") or "").strip()[:20],
    })
    return f"Follow-up {fid} planned for {due.isoformat()}.", False


def cancel_about(store, entry_id: str, why: str) -> int:
    """Cancel pending follow-ups about a ledger entry that has been closed."""
    n = 0
    for r in pending(store):
        if entry_id and r.get("about") == entry_id:
            _update(store, r, status=CANCELLED, completed_at=_now().isoformat(), outcome=why[:300])
            n += 1
    return n


def schedule_for_commitment(store, entry: dict, msg: dict) -> str:
    """A dated commitment gets a follow-up at its date — unless the thread
    already has one pending (one per thread; the earlier plan stands)."""
    thread = entry.get("thread_id", "")
    if any(r.get("thread_id") == thread for r in pending(store)):
        return "thread already has a follow-up"
    due = _parse(entry.get("due_at", ""))
    if due is None or due < _now() + datetime.timedelta(hours=MIN_HOURS):
        return "no usable date"
    text, err = schedule(store, msg, _split_audience(entry.get("audience", "")),
                         {"due_at": due.isoformat(), "note": f"You committed to: {entry['text']}",
                          "about": entry["entry_id"]})
    return text


def _split_audience(value: str) -> list[str]:
    return [a.strip() for a in (value or "").split(",") if a.strip()]


def _concerning(store, sender: str) -> list[dict]:
    return [r for r in pending(store) if sender in _recipients(r)]


def handle_reply_tool(store, msg: dict, recipients, name: str, payload: dict,
                      shared: bool) -> tuple[str, bool]:
    sender = (msg.get("from_addr") or "").lower()
    try:
        if name == SCHEDULE_TOOL:
            return schedule(store, msg, recipients, payload or {})
        if shared:
            return "Not available in a reply to several people.", True
        if name == LIST_TOOL:
            rows = _concerning(store, sender)
            if not rows:
                return "No follow-ups planned on threads that include you.", False
            return "\n".join(f"{r['followup_id']}: due {r['due_at'][:16]} UTC, with "
                             f"{r['recipients']} — {r['note']}" for r in rows), False
        if name == CANCEL_TOOL:
            which = ((payload or {}).get("which") or "").strip().lower()
            rows = _concerning(store, sender)
            targets = rows if which == "all" else [r for r in rows
                                                  if r["followup_id"].lower() == which]
            if not targets:
                return f"No planned follow-up matches {which!r}.", True
            for r in targets:
                _update(store, r, status=CANCELLED, completed_at=_now().isoformat(),
                        outcome=f"cancelled at {sender}'s request")
            return f"Cancelled {len(targets)} follow-up(s).", False
    except Exception as exc:
        return f"That didn't work: {exc.__class__.__name__}.", True
    return f"Unknown tool {name}.", True


# ---------------------------------------------------------------------------
# acting on them, on the heartbeat
# ---------------------------------------------------------------------------

FIRE_SUFFIX = """

--- A FOLLOW-UP YOU PLANNED ---
On {created} you decided to come back to this thread around now. It is now
{now} UTC. Your note to yourself: {note}

Below is the thread as it stands, showing only what everyone it would go to
({recipients}) has already seen. Look at it fresh, and call exactly one tool:
- send_follow_up, with the message (plain text, short, as yourself, written
  for all of them, no sign-off) if a follow-up is useful now;
- postpone_follow_up, with a later time and the reason, if it's too early —
  what you're waiting on can't have happened yet ({postpones_left} postponement(s)
  left);
- skip_follow_up, with the reason, if it isn't needed — it has been dealt
  with, someone already answered, the moment has passed.
Don't promise further follow-ups."""

FIRE_TOOLS = [
    {"name": SEND_TOOL, "description": "Send the follow-up in the thread.",
     "input_schema": {"type": "object", "properties": {"body": {"type": "string"}},
                      "required": ["body"]}},
    {"name": POSTPONE_TOOL, "description": "Too early: look again at a later time.",
     "input_schema": {"type": "object", "properties": {
         "due_at": {"type": "string", "description": "ISO 8601 with an offset."},
         "reason": {"type": "string"}}, "required": ["due_at", "reason"]}},
    {"name": SKIP_TOOL, "description": "Don't send; say why.",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}},
                      "required": ["reason"]}},
]


def sent_today(store, now=None) -> int:
    now = now or _now()
    count = 0
    for r in _rows(store):
        if r.get("status") != SENT:
            continue
        try:
            when = datetime.datetime.fromisoformat(r.get("completed_at", ""))
        except ValueError:
            continue
        if now - when < datetime.timedelta(days=1):
            count += 1
    return count


def _thread_view(gmail, store, row: dict, selves) -> tuple[str, dict | None]:
    """The thread filtered to what all recipients have seen, and its latest
    such message (to reply to)."""
    thread = gmail.users().threads().get(userId="me", id=row["thread_id"],
                                         format="full").execute()
    msgs = sorted(thread.get("messages", []) or [], key=lambda m: int(m.get("internalDate", 0)))
    recipients = set(_recipients(row))
    seen = []
    for m in msgs:
        hdrs = gmail_reads._headers(m)
        meta = {"from": hdrs.get("from", ""), "to": hdrs.get("to", ""), "cc": hdrs.get("cc", "")}
        if audience.cleared(m.get("id", ""), meta, recipients, selves, None):
            seen.append(m)
    parts = []
    for m in seen[-10:]:
        e = gmail_io.extract_message(m, 6000)
        parts.append(f"From: {e['from_raw']}\nSubject: {e['subject']}\n\n{e['body'][:3000]}")
    return "\n\n-----\n\n".join(parts) or "(no messages they have all seen)", (seen[-1] if seen else None)


def _decide(client, cfg, model: str, system_prompt: str, row: dict, view: str):
    response = client.messages.create(
        model=model, max_tokens=min(getattr(cfg, "max_tokens", 4000), 4000),
        system=system_prompt + FIRE_SUFFIX.format(
            created=row.get("created_at", "")[:16], note=row.get("note", ""),
            recipients=row.get("recipients", ""),
            now=_now().strftime("%A %d %B %Y, %H:%M"),
            postpones_left=max(0, MAX_POSTPONES - int(row.get("postponed") or 0))),
        tools=FIRE_TOOLS,
        messages=[{"role": "user", "content": f"THE THREAD:\n{view}"}])
    costs.record("followup", model, costs.tokens_from_usage(getattr(response, "usage", None)))
    for b in response.content:
        if getattr(b, "type", "") == "tool_use" and b.name in (SEND_TOOL, SKIP_TOOL,
                                                                POSTPONE_TOOL):
            return b.name, b.input
    return None, {}


def run_due(client, cfg, gmail, store, system_prompt: str, selves, people,
            model: str, now=None) -> list[str]:
    """Act on follow-ups that are due. Called from each heartbeat."""
    import reply_all
    now = now or _now()
    outcomes = []
    due = [r for r in pending(store)
           if (_parse(r.get("due_at", "")) or now) <= now][:PER_RUN]
    for row in due:
        fid = row["followup_id"]
        if sent_today(store, now) >= per_day():
            outcomes.append(f"follow-up {fid} waits: daily budget reached")
            break
        recipients = [a for a in _recipients(row)
                      if people is None or reply_all.accepted(a, people)]
        if row.get("owner") not in recipients:
            _update(store, row, status=SKIPPED, completed_at=now.isoformat(),
                    outcome="the person it was for no longer accepts the terms")
            outcomes.append(f"follow-up {fid} skipped (terms)")
            continue
        try:
            view, latest = _thread_view(gmail, store, {**row, "recipients": ", ".join(recipients)},
                                        selves)
            if cfg.dry_run:
                outcomes.append(f"dry run: would consider follow-up {fid}")
                continue
            choice, data = _decide(client, cfg, model, system_prompt,
                                   {**row, "recipients": ", ".join(recipients)}, view)
            if choice == POSTPONE_TOOL:
                reason = (data.get("reason") or "").strip()[:300]
                later = _parse(data.get("due_at", ""))
                done = int(row.get("postponed") or 0)
                created = _parse(row.get("created_at", "")) or now
                if (later is not None and done < MAX_POSTPONES
                        and later >= now + datetime.timedelta(hours=MIN_HOURS)
                        - datetime.timedelta(minutes=5)
                        and later <= created + datetime.timedelta(days=MAX_DAYS)):
                    _update(store, row, due_at=later.astimezone(datetime.timezone.utc).isoformat(),
                            postponed=str(done + 1), attempts="0",
                            outcome=f"postponed: {reason}")
                    outcomes.append(f"follow-up {fid} postponed to {later.isoformat()[:16]}: "
                                    f"{reason[:60]}")
                    continue
                choice, data = SKIP_TOOL, {"reason": "wanted to postpone again, but "
                                           + ("it has been put off twice already"
                                              if done >= MAX_POSTPONES else "the new time "
                                              "wasn't usable") + f" ({reason[:120]})"}
            if choice != SEND_TOOL:
                reason = (data.get("reason") or "no decision").strip()[:300]
                _update(store, row, status=SKIPPED, completed_at=now.isoformat(), outcome=reason)
                outcomes.append(f"follow-up {fid} skipped: {reason[:80]}")
                continue
            body = (data.get("body") or "").strip()
            body = _checked(client, cfg, store, body, set(recipients), view)
            if body is None:
                _update(store, row, status=SKIPPED, completed_at=now.isoformat(),
                        outcome="held back by the pre-send check")
                outcomes.append(f"follow-up {fid} held back by the check")
                continue
            _send(cfg, gmail, store, row, recipients, latest, body)
            _update(store, row, status=SENT, completed_at=now.isoformat(), outcome="sent")
            outcomes.append(f"follow-up {fid} sent")
        except Exception as exc:
            attempts = int(row.get("attempts") or 0) + 1
            if attempts >= MAX_ATTEMPTS:
                log.error("Giving up on follow-up %s after %d attempts.", fid, attempts,
                          exc_info=True)
                _update(store, row, status=FAILED, attempts=str(attempts),
                        completed_at=now.isoformat(), outcome=exc.__class__.__name__)
            else:
                log.warning("Follow-up %s: %s; will try again.", fid, exc.__class__.__name__,
                            exc_info=True)
                _update(store, row, attempts=str(attempts))
            outcomes.append(f"follow-up {fid} not done ({exc.__class__.__name__})")
    return outcomes


def _checked(client, cfg, store, body: str, recipients: set[str], seen: str):
    """The pre-send check, when consultation is on. Raises CheckUnavailable
    to the caller's retry; returns None when it blocks."""
    import consult
    if not body:
        return None
    if not (consult.enabled(cfg) and getattr(store, "supports_audience", False)):
        return body
    private = consult.candidates(store, recipients)
    if not private:
        return body
    text, _ = consult.vet(client, cfg, body, recipients, private, seen)
    return text


# Who has had a follow-up from this process: the heartbeat's own
# initiative step mustn't write to them again in the same run.
SENT_THIS_RUN: set[str] = set()


def _send(cfg, gmail, store, row: dict, recipients: list[str], latest: dict | None,
          body: str) -> None:
    owner = row["owner"]
    others = [a for a in recipients if a != owner]
    if latest is not None:
        e = gmail_io.extract_message(latest, 2000)
        subject, in_reply_to = e["subject"], e["message_id"]
        references = (e.get("references", "") + " " + e["message_id"]).strip()
    else:
        subject, in_reply_to, references = "Following up", "", ""
    text = gmail_io.unwrap_paragraphs(body).rstrip()
    if "--- Indi" not in text:
        text += "\n\n--- Indi"
    mime = gmail_io.build_reply_mime(to_addr=owner, subject=subject, body=text,
                                     in_reply_to=in_reply_to, references=references, cc=others)
    mime["threadId"] = row["thread_id"]
    SENT_THIS_RUN.update(a.lower() for a in recipients)
    if cfg.send_automatically:
        result = gmail.users().messages().send(userId="me", body=mime).execute() or {}
        sent_id = result.get("id", "")
    else:
        result = gmail.users().drafts().create(userId="me", body={"message": mime}).execute() or {}
        sent_id = (result.get("message") or {}).get("id", "")
    if sent_id and hasattr(store, "_label_add"):
        from sheets_queue import LABEL_REPLY
        store._label_add(sent_id, LABEL_REPLY)
