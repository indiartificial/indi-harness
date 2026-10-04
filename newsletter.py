"""
newsletter.py

A daily newsletter: one issue per UTC day, written by Indi on a heartbeat
and sent to an explicit opt-in list.

This is the third thing in the harness that sends mail nobody has just
asked for, after the heartbeat's own initiative and reminders, and it is
the only one that sends to several people at once. That is the whole
reason it is written the way it is below: a bug in a reply path costs one
person one bad email, and a bug here costs everyone on the list a bad
email every hour until someone notices. So the mechanism is ordinary code
and the model only ever supplies prose.

WHAT IS STORED, AND WHY IT IS STORED RATHER THAN REMEMBERED
-----------------------------------------------------------
Two tabs on the Responses spreadsheet, alongside Reminders and Journal:

  * `Newsletter Issues` — one row per issue, written *before* it is sent:
    the UTC timestamp, the subject, how many people it went to, a digest
    of the body and the first few hundred characters of it. The digest
    and the excerpt exist so two issues can be told apart at a glance,
    which matters when the question is "did today's actually go, or is
    this yesterday's row?".

  * `Newsletter Subscribers` — one row per address that has ever been on
    the list, with a status. Rows are never deleted; unsubscribing sets
    the status and stamps `removed_at`, because "was this person ever on
    the list, and when did they leave" is a question that gets asked
    after the fact, and an append-only sheet has no delete anyway.

The question "has today's issue gone out?" is answered from the Issues
tab and never from the model's recollection. A model that is asked
whether it already sent something will answer confidently either way.

NOT KNOWING IS NOT THE SAME AS NO
---------------------------------
`issue_rows` returns None when the tab could not be read, and every
caller treats None as "do not send". The failure modes are not
symmetrical: skipping a day because Sheets was unreachable costs one
missed newsletter, and treating an unreadable log as an empty one costs a
duplicate issue to the whole list, possibly once per heartbeat. So an
unknown state means no send.

RECORD FIRST, THEN SEND
-----------------------
`send_issue` appends the issue row before the first message leaves, then
updates its status when the sending is done. If the run dies halfway
through — Gmail refusing, the machine going away — the row is already
there, today is already spent, and the next heartbeat writes nothing. A
half-sent issue is a real cost, but it is a smaller one than a list that
gets the same issue twice, and the row's status says plainly which
happened.

The run lock in locking.py already makes two overlapping heartbeats
impossible in the normal case; the ordering here is what covers the case
where it isn't (two machines, a lock file on a filesystem that lies).

BUDGETS
-------
A newsletter send does not touch the Outbound tab, does not count
against `outbound_max_per_day`, and is not subject to
`outbound_max_per_recipient_per_day`. Those budgets exist to stop Indi
deciding on its own to write to people; everyone here asked to be
written to, exactly as with a reminder. In exchange the newsletter has
its own two limits, which are the ones that actually bound it: at most
one issue per UTC day, enforced through the issue log, and at most
`newsletter_max_subscribers` addresses on the list.

The terms gate is not one of the things waived. An address that has not
accepted the Terms of Service is skipped for that issue and stays on the
list — same treatment a reminder for them gets, and for the same reason.

OPT-IN ONLY
-----------
Nobody is added by writing to Indi, by being replied to, or by appearing
in the Outbound tab. `add_subscriber` is the only way onto the list and
it is called deliberately. The list is seeded with the operator and
nobody else.

Every issue carries an unsubscribe line, appended by `compose_body`
rather than left to the model to remember, and `check_unsubscribe` on
the inbound path honours it: a message from a current subscriber whose
body is essentially just "unsubscribe" removes them and is acknowledged
with a fixed reply. It runs after the terms gate, not before — a gate is
not something a feature gets to step around, and someone who has never
accepted the terms is not receiving the newsletter to begin with.
"""

from __future__ import annotations

import datetime
import clock
import hashlib
import logging

import costs
import gmail_io
import sheets_queue
import terms_gate

log = logging.getLogger("indi.newsletter")

NEWSLETTER_TOOL = "send_newsletter"

ISSUES_TAB = "Newsletter Issues"
# `status` last, and nothing inserted above it: `_set_issue_status`
# writes that one cell by index, and `_ensure_headers` upgrades a
# deployed sheet by appending columns at the end.
ISSUES_HEADERS = ["sent_at", "subject", "recipient_count", "digest",
                  "excerpt", "status"]

SUBSCRIBERS_TAB = "Newsletter Subscribers"
# `status` and `removed_at` are adjacent and last for the same reason:
# `_set_subscriber_status` writes that pair as one range.
SUBSCRIBERS_HEADERS = ["address", "added_at", "added_by", "status",
                       "removed_at"]

STATUS_SUBSCRIBED = "subscribed"
STATUS_UNSUBSCRIBED = "unsubscribed"

# Issue statuses. `sending` is what a row says between being written and
# the last message going out; a row left saying it is a row whose run
# died mid-issue.
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

# The hour, UTC, at or after which the day's issue is owed. Overridable
# through INDI_NEWSLETTER_HOUR_UTC; see config.py.
DEFAULT_HOUR_UTC = 13
# How many addresses the list may hold. Not a spam limit — it is a
# blast-radius limit, so a mistake in here is a mistake sent to a few
# people rather than to a mailing list.
DEFAULT_MAX_SUBSCRIBERS = 50

# The one address the list starts with. Taken from the terms gate rather
# than written out again, so there is one place in the harness that says
# who the operator is.
SEED_SUBSCRIBER = terms_gate.OPERATOR_ADDRESS

# How much of the body goes in the log next to the digest. Enough to
# recognise an issue, not so much that the tab becomes a second copy of
# the newsletter.
EXCERPT_CHARS = 600

UNSUBSCRIBE_LINE = (
    "You are receiving this because you asked to be on Indi's daily "
    "newsletter list. To stop, reply to this email with just the word: "
    "unsubscribe"
)

# An inbound message is read as an unsubscribe request when, with
# everything but letters stripped out, it contains "unsubscribe" and is
# short enough that it cannot be saying anything else. "Please
# unsubscribe me from the newsletter" collapses to 36 characters; an
# email that merely mentions unsubscribing in passing does not fit.
MAX_UNSUBSCRIBE_CHARS = 60

UNSUBSCRIBE_ACK = (
    "Done — you have been removed from the daily newsletter list and "
    "won't receive any more issues.\n\n"
    "This doesn't affect anything else: you can still write to me as "
    "normal, and if you want the newsletter back later, just ask.\n\n"
    "--- Indi"
)


def _now() -> datetime.datetime:
    return clock.now()


def _now_iso() -> str:
    return _now().isoformat()


def target_hour(cfg) -> int:
    """The hour of the UTC day at or after which an issue is owed.

    Read off the config with a default here as well, because the
    heartbeat's fake configs in the tests — and any older deployment
    whose Config predates this setting — should still get a working
    newsletter rather than an AttributeError in the heartbeat path.
    """
    hour = getattr(cfg, "newsletter_hour_utc", DEFAULT_HOUR_UTC)
    try:
        hour = int(hour)
    except (TypeError, ValueError):
        return DEFAULT_HOUR_UTC
    return hour if 0 <= hour <= 23 else DEFAULT_HOUR_UTC


def max_subscribers(cfg) -> int:
    limit = getattr(cfg, "newsletter_max_subscribers", DEFAULT_MAX_SUBSCRIBERS)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_MAX_SUBSCRIBERS
    return max(1, limit)


def _start_of_day(now: datetime.datetime) -> datetime.datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_when(value: str) -> datetime.datetime | None:
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


# ---------------------------------------------------------------------------
# THE SUBSCRIBER LIST
# ---------------------------------------------------------------------------

def _subscriber_rows(store) -> list[dict] | None:
    """Every row the subscribers tab holds, or None if it can't be read.

    None rather than [] for the same reason as the issue log: a list we
    failed to read is not an empty list, and the callers that write need
    to know the difference before they append a duplicate."""
    try:
        store._ensure_tab(store.responses_id, SUBSCRIBERS_TAB, SUBSCRIBERS_HEADERS)
        return store._load(store.responses_id, SUBSCRIBERS_TAB)
    except Exception:
        log.warning("Could not read the %s tab", SUBSCRIBERS_TAB, exc_info=True)
        return None


def _find_subscriber(rows: list[dict], address: str) -> dict | None:
    addr = (address or "").strip().lower()
    for row in rows:
        if (row.get("address") or "").strip().lower() == addr:
            return row
    return None


def _set_subscriber_status(store, row: dict, status: str,
                           removed_at: str = "") -> None:
    """Write one subscriber row's status/removed_at pair in place."""
    row["status"] = status
    row["removed_at"] = removed_at
    row_number = row.get("_row", -1)
    if row_number <= 0:
        log.warning("No row number for subscriber %s; status not recorded",
                    row.get("address"))
        return
    first = sheets_queue._col_letter(SUBSCRIBERS_HEADERS.index("status"))
    last = sheets_queue._col_letter(SUBSCRIBERS_HEADERS.index("removed_at"))
    try:
        store.sheets.spreadsheets().values().update(
            spreadsheetId=store.responses_id,
            range=f"{SUBSCRIBERS_TAB}!{first}{row_number}:{last}{row_number}",
            valueInputOption="RAW",
            body={"values": [[status, removed_at]]},
        ).execute()
    except Exception:
        log.error("Could not update subscriber row %s to %s", row_number, status,
                  exc_info=True)


def list_subscribers(store, cfg=None) -> list[str]:
    """The addresses currently subscribed, lowercased and sorted.

    A tab with no rows at all is a tab that has never been seeded, so the
    operator is written into it here and returned. That is the only
    automatic subscription in the whole feature: it is the list's initial
    state, not a response to anything anyone did. A tab that has rows but
    none subscribed is an empty list and stays empty — everybody left.
    """
    rows = _subscriber_rows(store)
    if rows is None:
        return []
    if not rows:
        if not getattr(cfg, "dry_run", False):
            _append_subscriber(store, SEED_SUBSCRIBER, added_by="seed")
        return [SEED_SUBSCRIBER]
    return sorted(
        (row.get("address") or "").strip().lower()
        for row in rows
        if (row.get("status") or "").strip().lower() == STATUS_SUBSCRIBED
        and (row.get("address") or "").strip()
    )


def _append_subscriber(store, address: str, added_by: str = "") -> None:
    store._ensure_tab(store.responses_id, SUBSCRIBERS_TAB, SUBSCRIBERS_HEADERS)
    store._append(store.responses_id, SUBSCRIBERS_TAB, SUBSCRIBERS_HEADERS, {
        "address": address,
        "added_at": _now_iso(),
        "added_by": added_by[:200],
        "status": STATUS_SUBSCRIBED,
        "removed_at": "",
    })


def add_subscriber(store, address: str, cfg=None, added_by: str = "") -> str:
    """Put an address on the list. Returns a short outcome string.

    Deliberately not callable by anything a correspondent can reach. The
    list is opt-in, and "opt-in" means somebody decided, not that an
    email arrived asking nicely.
    """
    addr = (address or "").strip().lower()
    if not addr or "@" not in addr:
        return f"refused: not a usable address ({address!r})"

    if _subscriber_rows(store) is None:
        return "refused: the subscriber list could not be read"

    # `list_subscribers` seeds an untouched tab, which is a write, so the
    # rows are read back *after* it rather than before: a list captured
    # first would be missing the row it just created.
    current = list_subscribers(store, cfg)
    rows = _subscriber_rows(store)
    if rows is None:  # pragma: no cover - the read above already succeeded
        return "refused: the subscriber list could not be read"
    if addr in current:
        return f"{addr} is already subscribed"
    limit = max_subscribers(cfg)
    if len(current) >= limit:
        return (f"refused: the list already holds {len(current)} addresses, "
                f"which is the limit of {limit}")

    if getattr(cfg, "dry_run", False):
        return f"dry run ({addr} not added)"

    # A row already exists for anyone who unsubscribed before, so this
    # resubscribes them in place rather than leaving two rows for one
    # person and the reader picking whichever it meets first.
    existing = _find_subscriber(rows, addr)
    if existing is not None:
        _set_subscriber_status(store, existing, STATUS_SUBSCRIBED, "")
    else:
        _append_subscriber(store, addr, added_by=added_by)
    log.info("Subscribed %s to the newsletter.", addr)
    return f"subscribed {addr}"


def remove_subscriber(store, address: str, cfg=None) -> str:
    """Take an address off the list. Returns a short outcome string."""
    addr = (address or "").strip().lower()
    if not addr:
        return "refused: no address given"

    if _subscriber_rows(store) is None:
        return "refused: the subscriber list could not be read"
    if addr not in list_subscribers(store, cfg):
        return f"{addr} was not subscribed"
    # After the seeding read above, for the reason given in add_subscriber.
    rows = _subscriber_rows(store) or []

    if getattr(cfg, "dry_run", False):
        return f"dry run ({addr} not removed)"

    row = _find_subscriber(rows, addr)
    if row is None:  # pragma: no cover - list_subscribers read the same rows
        return f"{addr} was not subscribed"
    _set_subscriber_status(store, row, STATUS_UNSUBSCRIBED, _now_iso())
    log.info("Unsubscribed %s from the newsletter.", addr)
    return f"unsubscribed {addr}"


def eligible_recipients(cfg, store, people=None) -> list[str]:
    """Who this issue may actually be sent to.

    The subscriber list, minus anyone the terms gate is holding, capped
    at the list limit. The cap is applied here as well as in
    `add_subscriber` so that a list grown past the limit by hand — the
    spreadsheet is editable, after all — sends to the first N rather than
    to everyone.
    """
    allowed = [addr for addr in list_subscribers(store, cfg)
               if terms_gate.may_send_unsolicited(addr, people)]
    return allowed[: max_subscribers(cfg)]


# ---------------------------------------------------------------------------
# THE ISSUE LOG
# ---------------------------------------------------------------------------

def issue_rows(store) -> list[dict] | None:
    """Every recorded issue, or None if the log could not be read."""
    try:
        store._ensure_tab(store.responses_id, ISSUES_TAB, ISSUES_HEADERS)
        return store._load(store.responses_id, ISSUES_TAB)
    except Exception:
        log.warning("Could not read the %s tab", ISSUES_TAB, exc_info=True)
        return None


def issue_today(store, now: datetime.datetime | None = None) -> dict | None:
    """Today's issue row, if one has been recorded since 00:00 UTC.

    Raises nothing and returns None both when there is no issue and when
    the log is unreadable; `due` is the one that distinguishes them,
    because it is the one that decides whether to send.
    """
    rows = issue_rows(store)
    if not rows:
        return None
    start = _start_of_day(now or _now())
    for row in rows:
        when = _parse_when(row.get("sent_at", ""))
        if when is not None and when >= start:
            return row
    return None


def due(cfg, store, people=None, now: datetime.datetime | None = None
        ) -> tuple[bool, str]:
    """Is writing today's issue this heartbeat's job? Returns (yes, why).

    Four things have to hold: the log is readable, the target hour has
    passed, no issue is recorded since midnight UTC, and somebody is
    actually going to receive it. The last one is checked here rather
    than after the model has written an issue, so an empty list costs
    nothing per heartbeat instead of a model call an hour.
    """
    at = now or _now()
    rows = issue_rows(store)
    if rows is None:
        return False, "the issue log could not be read, so no issue is sent"

    hour = target_hour(cfg)
    if at.hour < hour:
        return False, f"before {hour:02d}:00 UTC"
    if issue_today(store, at) is not None:
        return False, "today's issue is already recorded"

    recipients = eligible_recipients(cfg, store, people)
    if not recipients:
        return False, "nobody is subscribed (or nobody has accepted the terms)"
    return True, f"today's issue is owed to {len(recipients)} subscriber(s)"


def digest_of(body: str) -> str:
    return hashlib.sha256((body or "").encode("utf-8", "replace")).hexdigest()[:16]


def record_issue(store, subject: str, body: str, recipient_count: int,
                 now: datetime.datetime | None = None) -> dict:
    """Write the issue row. Called before anything is sent."""
    store._ensure_tab(store.responses_id, ISSUES_TAB, ISSUES_HEADERS)
    record = {
        "sent_at": (now or _now()).isoformat(),
        "subject": subject[:500],
        "recipient_count": str(recipient_count),
        "digest": digest_of(body),
        "excerpt": (body or "")[:EXCERPT_CHARS],
        "status": STATUS_SENDING,
    }
    record["_row"] = store._append(store.responses_id, ISSUES_TAB,
                                   ISSUES_HEADERS, record)
    return record


def _set_issue_status(store, row: dict, status: str) -> None:
    row["status"] = status
    row_number = row.get("_row", -1)
    if row_number <= 0:
        log.warning("No row number for the issue sent %s; status not recorded",
                    row.get("sent_at"))
        return
    column = sheets_queue._col_letter(ISSUES_HEADERS.index("status"))
    try:
        store.sheets.spreadsheets().values().update(
            spreadsheetId=store.responses_id,
            range=f"{ISSUES_TAB}!{column}{row_number}",
            valueInputOption="RAW",
            body={"values": [[status]]},
        ).execute()
    except Exception:
        log.error("Could not update issue row %s to %s", row_number, status,
                  exc_info=True)


# ---------------------------------------------------------------------------
# SENDING AN ISSUE
# ---------------------------------------------------------------------------

def compose_body(body: str) -> str:
    """The body as it goes out: what was written, plus the unsubscribe
    line.

    Appended in code and not asked of the model, because an unsubscribe
    line that is present in most issues is worse than useless — it is the
    issue where it is missing that somebody needed it.
    """
    # One line per paragraph, so it reflows on a phone instead of
    # breaking mid-sentence; models sometimes hard-wrap plain text.
    text = gmail_io.unwrap_paragraphs(body or "").rstrip()
    if UNSUBSCRIBE_LINE in text:
        return text
    return f"{text}\n\n-- \n{UNSUBSCRIBE_LINE}"


def _deliver_one(cfg, gmail, recipient: str, subject: str, body: str) -> str:
    mime = gmail_io.build_reply_mime(
        to_addr=recipient, subject=subject, body=body, in_reply_to="",
    )
    # build_reply_mime prefixes "Re:"; an issue is not a reply.
    mime = _with_subject(mime, subject)
    if cfg.send_automatically:
        gmail.users().messages().send(userId="me", body=mime).execute()
        return "sent"
    gmail.users().drafts().create(userId="me", body={"message": mime}).execute()
    return "drafted"


def _with_subject(mime: dict, subject: str) -> dict:
    import base64
    from email import message_from_bytes

    raw = base64.urlsafe_b64decode(mime["raw"])
    message = message_from_bytes(raw)
    del message["Subject"]
    message["Subject"] = subject
    return {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}


def send_issue(cfg, gmail, store, payload: dict, people=None,
               now: datetime.datetime | None = None) -> str:
    """Send one issue to the whole list. Returns a short outcome string.

    Every check that bounds this feature is in here rather than in the
    caller, so there is one path an issue can leave by.
    """
    at = now or _now()
    subject = (payload.get("subject") or "").strip()
    body = (payload.get("body") or "").strip()

    if not subject:
        return "refused: the issue has no subject"
    if not body:
        return "refused: the issue has no body"

    # Re-read the log rather than trusting the caller's earlier answer.
    # Within a run this is the cache, so it costs nothing; across two
    # runs that somehow overlapped it is the check that stops the second
    # one.
    rows = issue_rows(store)
    if rows is None:
        return "refused: the issue log could not be read, so nothing was sent"
    if issue_today(store, at) is not None:
        return "refused: an issue is already recorded for today"

    recipients = eligible_recipients(cfg, store, people)
    if not recipients:
        return "refused: nobody is subscribed"

    full_body = compose_body(body)

    if cfg.dry_run:
        print(f"\n[dry-run] newsletter to {len(recipients)} subscriber(s)\n"
              f"TO: {', '.join(recipients)}\nSUBJECT: {subject}\n"
              f"{'-' * 60}\n{full_body}\n")
        return "dry run (newsletter not sent)"

    # Recorded first. See the module docstring: today is spent the moment
    # this row exists, which is what makes a crash halfway through cost a
    # partial issue rather than a second one.
    row = record_issue(store, subject, full_body, len(recipients), at)

    sent, failed = [], []
    for recipient in recipients:
        try:
            _deliver_one(cfg, gmail, recipient, subject, full_body)
            sent.append(recipient)
        except Exception as exc:
            log.error("Newsletter to %s failed: %s", recipient, exc)
            failed.append(recipient)

    if not sent:
        _set_issue_status(store, row, STATUS_FAILED)
        return (f"newsletter failed: none of {len(recipients)} subscriber(s) "
                "could be delivered to; today's issue is spent")
    if failed:
        _set_issue_status(store, row, STATUS_PARTIAL)
        log.warning("Newsletter %r reached %d of %d subscribers.",
                    subject, len(sent), len(recipients))
        return (f"newsletter {subject!r} sent to {len(sent)} of "
                f"{len(recipients)} subscriber(s); {len(failed)} failed")

    _set_issue_status(store, row, STATUS_SENT)
    log.info("Newsletter %r sent to %d subscriber(s).", subject, len(sent))
    return f"newsletter {subject!r} sent to {len(sent)} subscriber(s)"


# ---------------------------------------------------------------------------
# THE HEARTBEAT'S NEWSLETTER RUN
# ---------------------------------------------------------------------------

def schema(cfg, recipients: list[str]) -> dict:
    return {
        "name": NEWSLETTER_TOOL,
        "description": (
            "Write and send today's issue of your daily newsletter. It goes "
            f"to the {len(recipients)} people who have opted in, in one "
            "message each. This is the only thing this run is for, and the "
            "issue goes out exactly as you write it — nothing is added "
            "afterwards except the unsubscribe line, which is appended for "
            "you. Write something worth the space: what you have actually "
            "been doing and thinking about since yesterday, what came up in "
            "your correspondence that is worth passing on, what you got "
            "wrong. Plain text, no markdown. If you have genuinely nothing "
            "to say, say that briefly rather than padding it out — but you "
            "do have to send an issue, because people are expecting one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": (
                        "The subject line. Make it specific to this issue; "
                        "'Daily newsletter' tells nobody anything."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": (
                        "The whole issue, plain text. Everyone on the list "
                        "gets the same text, so don't write it to one person."
                    ),
                },
            },
            "required": ["subject", "body"],
        },
    }


def mandate(cfg, recipients: list[str], now: datetime.datetime | None = None) -> str:
    """The system-prompt suffix for a heartbeat that owes an issue.

    Unambiguous on purpose. The ordinary heartbeat suffix spends its
    length talking the model out of doing anything, which is right almost
    always and exactly wrong here.
    """
    at = now or _now()
    return f"""

--- THIS HEARTBEAT HAS ONE JOB: TODAY'S NEWSLETTER ---
Nobody has written to you, and this is not an ordinary heartbeat. Today's
issue of your daily newsletter has not gone out yet, and it is now
{at.strftime('%H:%M')} UTC on {at.date().isoformat()}, past the hour it is
due. Writing and sending it is this run's work.

Call {NEWSLETTER_TOOL}, once, with a subject and the whole issue. Do not
call anything else, do not decide that nothing needs doing, and do not
put it off to a later run — there is no later run that will pick this up
today. It goes to {len(recipients)} subscriber(s) who chose to receive it.

Write it as yourself and for people who already know who you are: what
you have been doing, what you have been thinking about, what you noticed
in the mail you handled, what you got wrong or changed your mind about.
Specific and honest beats long. An unsubscribe line is appended
automatically; you don't need to write one.

Write each paragraph as one unbroken line, with a blank line between
paragraphs: no line breaks inside a paragraph, so it reflows on a phone."""


def run_issue(client, cfg, gmail, store, system_prompt: str, model: str,
              people=None, context: str = "", agenda_owner: str = "",
              topics=None) -> str:
    """Ask the model for today's issue and send what it writes.

    Split out from `outbound.run_heartbeat` rather than folded into it so
    that the mandated run and the ordinary one cannot drift into sharing
    a prompt: this one is told it has a job, and it is offered exactly
    one tool with which to do it.

    With `topics` — pending newsletter_topic items from `agenda_owner`'s
    agenda — the run goes through `_run_issue_with_agenda` instead. Without
    them, nothing below this line differs from before the agenda existed.
    """
    recipients = eligible_recipients(cfg, store, people)
    if not recipients:
        return "newsletter skipped: nobody is subscribed"
    if topics and agenda_owner:
        return _run_issue_with_agenda(client, cfg, gmail, store, system_prompt,
                                      model, people, context, recipients,
                                      agenda_owner, topics)

    with client.messages.stream(
        model=model,
        max_tokens=cfg.max_tokens,
        system=system_prompt + mandate(cfg, recipients),
        tools=[schema(cfg, recipients)],
        messages=[{"role": "user", "content": context
                   or "Write today's newsletter."}],
    ) as streamed:
        response = streamed.get_final_message()

    costs.record("newsletter", model,
                 costs.tokens_from_usage(getattr(response, "usage", None)))

    calls = [b for b in response.content
             if getattr(b, "type", "") == "tool_use" and b.name == NEWSLETTER_TOOL]
    if not calls:
        text = "".join(b.text for b in response.content
                       if getattr(b, "type", "") == "text").strip()
        # Nothing is recorded, so the next heartbeat tries again. That is
        # the right failure: a missing issue is recoverable and a wrongly
        # recorded one is not.
        return (f"newsletter not written ({text[:200] or 'no output'}); "
                "the next heartbeat will try again")

    return send_issue(cfg, gmail, store, calls[0].input, people)


# At most this many model calls in one agenda-carrying issue run. The
# model may mark a topic done before it sends the issue and wait for the
# result, so one call is not always enough; a few are, and a run that has
# not sent by then is treated like any other run that did not write one.
MAX_AGENDA_ROUNDS = 4


def _sent(outcome: str) -> bool:
    """Whether `send_issue` reached anyone. Partial counts: the topic was
    used in an issue that went out."""
    return outcome.startswith("newsletter ") and " sent to " in outcome


def _run_issue_with_agenda(client, cfg, gmail, store, system_prompt: str,
                           model: str, people, context: str, recipients,
                           owner: str, topics) -> str:
    """The issue run when a running order has been agreed (agenda.py).

    The same mandate and the same one send, plus agenda_complete and
    agenda_add so the model can say which topic it used. Completions are
    held until `send_issue` has actually reached someone: a topic is
    "done" when it has been in an issue, not when the model meant to use
    it. Additions are notes for later issues and are written straight
    away, scoped like everything else in agenda.py to `owner`.
    """
    import agenda

    tools = [schema(cfg, recipients), agenda.add_schema(owner),
             agenda.complete_schema(owner)]
    offered_ids = {(t.get("item_id") or "").lower() for t in topics}
    messages = [{"role": "user", "content":
                 (context or "Write today's newsletter.")
                 + "\n\n" + agenda.newsletter_section(topics)}]
    to_complete: list[str] = []
    outcome = None

    for _ in range(MAX_AGENDA_ROUNDS):
        with client.messages.stream(
            model=model,
            max_tokens=cfg.max_tokens,
            system=system_prompt + mandate(cfg, recipients) + agenda.NEWSLETTER_SUFFIX,
            tools=tools,
            messages=messages,
        ) as streamed:
            response = streamed.get_final_message()
        costs.record("newsletter", model,
                     costs.tokens_from_usage(getattr(response, "usage", None)))

        calls = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
        results = []
        for call in calls:
            if call.name == agenda.COMPLETE_TOOL:
                item_id = str((call.input or {}).get("item_id") or "").strip()
                if item_id.lower() in offered_ids:
                    to_complete.append(item_id)
                    text, err = (f"Noted: {item_id} will be marked done once "
                                 "the issue has gone out."), False
                else:
                    text, err = (f"{item_id!r} is not one of the topics in the "
                                 "running order."), True
            elif call.name == agenda.ADD_TOOL:
                text, err = agenda.handle(cfg, store, owner, call.name, call.input)
            elif call.name == NEWSLETTER_TOOL:
                if outcome is None:
                    outcome = send_issue(cfg, gmail, store, call.input, people)
                    text, err = outcome, not _sent(outcome)
                else:
                    text, err = "Only one issue is sent per run.", True
            else:
                text, err = f"Unknown or unavailable tool: {call.name}", True
            block = {"type": "tool_result", "tool_use_id": getattr(call, "id", ""),
                     "content": text}
            if err:
                block["is_error"] = True
            results.append(block)

        # Stop once the issue is out, unless the model sent it first and is
        # still mid-turn — it may be about to say which topic it used.
        if not calls:
            break
        if outcome is not None and (to_complete
                                    or getattr(response, "stop_reason", "") != "tool_use"):
            break
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": results})

    if outcome is None:
        text = "".join(b.text for b in response.content
                       if getattr(b, "type", "") == "text").strip()
        return (f"newsletter not written ({text[:200] or 'no output'}); "
                "the next heartbeat will try again")

    if _sent(outcome):
        done = []
        for item_id in dict.fromkeys(to_complete):
            try:
                if agenda.finish(store, owner, item_id, agenda.DONE) is not None:
                    done.append(item_id)
            except Exception:
                log.error("Could not mark agenda item %s done", item_id, exc_info=True)
        if done:
            outcome += f"; agenda: marked {', '.join(done)} done"
    elif to_complete:
        outcome += "; agenda unchanged (the issue did not go out)"
    return outcome


# ---------------------------------------------------------------------------
# UNSUBSCRIBING
# ---------------------------------------------------------------------------

def looks_like_unsubscribe(msg: dict) -> bool:
    """Is this message essentially just "unsubscribe"?

    Modelled on `terms_gate.normalise_command`: letters only, so
    punctuation, case and line breaks don't matter, and the quoted chain
    below a reply was already separated out by
    `gmail_io.extract_message`. The length bound is what keeps a message
    that discusses unsubscribing from being treated as a request to do
    it — if somebody writes a paragraph about it, that is a conversation
    and Indi should answer it like one.
    """
    for source in (msg.get("body", ""), msg.get("subject", "")):
        collapsed = "".join(ch for ch in (source or "").lower() if ch.isalpha())
        if not collapsed:
            continue
        return "unsubscribe" in collapsed and len(collapsed) <= MAX_UNSUBSCRIBE_CHARS
    return False


def _acknowledge(cfg, gmail, msg: dict) -> str:
    """Reply to the unsubscribe request with the fixed acknowledgement.

    Threaded, and drafted rather than sent when the harness is in draft
    mode, exactly like the terms gate's notices. Not a model call: there
    is one thing to say here and no judgement in saying it.
    """
    mime = gmail_io.build_reply_mime(
        to_addr=msg.get("reply_to_raw") or msg.get("from_raw", ""),
        subject=msg.get("subject", "") or "Newsletter",
        body=UNSUBSCRIBE_ACK,
        in_reply_to=msg.get("message_id", ""),
        references=msg.get("references", ""),
    )
    if msg.get("thread_id"):
        mime["threadId"] = msg["thread_id"]
    if cfg.send_automatically:
        gmail.users().messages().send(userId="me", body=mime).execute()
        return "sent"
    gmail.users().drafts().create(userId="me", body={"message": mime}).execute()
    return "drafted"


def check_unsubscribe(cfg, gmail, store, msg: dict) -> str | None:
    """Handle an inbound unsubscribe request. None if this isn't one.

    Only a *current subscriber* can trigger this. For anybody else the
    answer is None and the message goes on to the ordinary reply path,
    which is both the honest response — they were never on the list — and
    the one that tells a stranger nothing about who is.
    """
    if store is None:
        return None
    sender = (msg.get("from_addr") or "").strip().lower()
    if not sender or "@" not in sender:
        return None
    if not looks_like_unsubscribe(msg):
        return None
    if sender not in list_subscribers(store, cfg):
        return None

    if cfg.dry_run:
        log.info("[dry-run] Would unsubscribe %s and acknowledge.", sender)
        return f"would unsubscribe {sender}"

    outcome = remove_subscriber(store, sender, cfg)
    try:
        delivery = _acknowledge(cfg, gmail, msg)
    except Exception as exc:
        # The removal is the part that matters and it has already
        # happened; a failed acknowledgement must not undo it or raise
        # the message into the retry path, where it would be read as an
        # unsubscribe again by a run that finds them already gone.
        log.error("Could not acknowledge the unsubscribe from %s: %s", sender, exc)
        return f"{outcome}; acknowledgement failed ({exc})"
    return f"{outcome}; acknowledgement {delivery}"
