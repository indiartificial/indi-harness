"""
agenda.py

A durable queue of planned work: the things Indi and a correspondent have
agreed it will come back to, in the order agreed.

WHY THIS EXISTS
---------------
The journal carries a thought forward, and the memory file carries what
Indi knows about a person, but neither carries a plan. When Steve and Indi
settle on a running order of newsletter topics, the only record of it was
prose in a memory file, which a later run may or may not read the same
way, and which has no notion of "this one is done". So a plan was an
intention rather than a schedule.

An agenda item is a row in an "Agenda" tab on the Responses spreadsheet,
alongside Reminders and Journal, read and written through the same
generic tab primitives the reminder and newsletter code use. Rows are
only ever appended or updated in place — completing or cancelling an item
sets its status and stamps `completed_at` — so the record of what was
planned, and what became of it, is kept.

WHAT AN ITEM IS, AND ISN'T
--------------------------
A note from Indi to its future self. Nothing here sends mail, runs on a
timer, or acts in the world: an item is shown to a later run, and that
run decides what to do about it within its own rules. That is the whole
difference from a reminder, which is delivered by code and is a promise
to the person who asked for it. An agenda item is not a promise to anyone.

Each item has an owner — the correspondent who asked for it — a free-text
kind ("newsletter_topic" and "note" are the ones the harness knows about),
a title, optional detail, a position, and optionally a `not_before` time
before which it is not offered. Pending items come back ordered by
position, then by when they were added.

WHO GETS IT
-----------
The reply-path tools are offered only to senders who already hold the
self-modification or local-tools permission, and who passed the trust
check for this message; that decision is made in check_and_reply.py and
nothing here widens it. Every tool is scoped in code to the sender's own
items, exactly as reminders are: none of them takes an owner argument, so
there is no way to name, list or touch anyone else's. They are also not
offered on a reply that goes to more than one person, because an agenda
is its owner's and not the thread's.

ON A HEARTBEAT
--------------
The operator's pending items (terms_gate.OPERATOR_ADDRESS, the one place
the harness says who that is) are shown to the ordinary heartbeat as
planned work carried forward. On the newsletter heartbeat, the pending
`newsletter_topic` items are shown as the agreed running order and the
run is given agenda_complete and agenda_add, so it can say which topic it
actually used. Nothing infers that for it. A completion asked for during
that run is applied only once the issue has actually gone out, so a run
that fails to send leaves the running order as it was. With no pending
topics, the newsletter run is exactly what it was before this existed.

Writes happen inside a harness run, under the run lock (locking.py), so
there is one writer at a time, as with every other tab.
"""

from __future__ import annotations

import datetime
import logging
import uuid

log = logging.getLogger("indi.agenda")

TAB = "Agenda"
# Appended to, never reordered: `_update` writes the whole row by index,
# and `_ensure_headers` upgrades a deployed sheet by adding columns at the
# end.
HEADERS = ["item_id", "created_at", "owner", "kind", "title", "detail",
           "position", "not_before", "status", "completed_at"]

PENDING, DONE, CANCELLED = "pending", "done", "cancelled"
STATUSES = (PENDING, DONE, CANCELLED)

KIND_NEWSLETTER_TOPIC = "newsletter_topic"
KIND_NOTE = "note"

ADD_TOOL = "agenda_add"
LIST_TOOL = "agenda_list"
COMPLETE_TOOL = "agenda_complete"
CANCEL_TOOL = "agenda_cancel"
TOOL_NAMES = frozenset({ADD_TOOL, LIST_TOOL, COMPLETE_TOOL, CANCEL_TOOL})

# Bounds, so a confused run cannot fill the sheet. Per owner, like the
# reminder quota, so one person's plan is the only thing that can crowd
# their own plan.
MAX_PENDING_PER_OWNER = 100
MAX_TITLE_CHARS = 200
MAX_DETAIL_CHARS = 4000
MAX_KIND_CHARS = 40
# How many items a heartbeat is shown. Enough for a running order; not so
# many that the plan crowds out the mail.
MAX_SHOWN = 20


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_when(value: str) -> datetime.datetime | None:
    """ISO 8601 -> aware UTC datetime, or None. A bare time is read as UTC,
    the same rule the reminder code applies."""
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


def _norm(address: str) -> str:
    return (address or "").strip().lower()


# ---------------------------------------------------------------------------
# STORAGE
# ---------------------------------------------------------------------------

def _rows(store) -> list[dict]:
    """Every row, or [] if the tab can't be read. An unreadable agenda
    costs a run its plan, never its mail."""
    try:
        store._ensure_tab(store.responses_id, TAB, HEADERS)
        return store._load(store.responses_id, TAB)
    except Exception:
        log.warning("Could not read the Agenda tab", exc_info=True)
        return []


def _update(store, row: dict, **fields) -> None:
    from sheets_queue import _col_letter
    row.update(fields)
    n = row.get("_row", -1)
    if n <= 0:
        raise RuntimeError(f"agenda item {row.get('item_id')!r} has no row number")
    first, last = _col_letter(0), _col_letter(len(HEADERS) - 1)
    store.sheets.spreadsheets().values().update(
        spreadsheetId=store.responses_id, range=f"{TAB}!{first}{n}:{last}{n}",
        valueInputOption="RAW",
        body={"values": [[str(row.get(h, "")) for h in HEADERS]]},
    ).execute()


def _position(row: dict) -> float:
    try:
        return float(row.get("position") or "")
    except ValueError:
        return float("inf")


def _sorted(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (_position(r), r.get("created_at", "")))


def items(store, owner: str, kind: str = "", status: str = PENDING) -> list[dict]:
    """One owner's items, in order. The owner filter is the scope rule:
    it is applied to the stored owner, never to anything a caller chose
    about whose items to see. `status` "" or "all" means every status."""
    owner = _norm(owner)
    if not owner:
        return []
    kind = (kind or "").strip()
    status = (status or "").strip().lower()
    rows = [r for r in _rows(store) if _norm(r.get("owner")) == owner]
    if kind:
        rows = [r for r in rows if (r.get("kind") or "").strip() == kind]
    if status and status != "all":
        rows = [r for r in rows if (r.get("status") or "").strip().lower() == status]
    return _sorted(rows)


def offered(store, owner: str, kind: str = "",
            now: datetime.datetime | None = None) -> list[dict]:
    """Pending items whose not_before has passed (or that have none) — what
    a run is shown as the current plan."""
    at = now or _now()
    out = []
    for row in items(store, owner, kind, PENDING):
        when = _parse_when(row.get("not_before", ""))
        if when is None or when <= at:
            out.append(row)
    return out


def add(store, owner: str, kind: str, title: str, detail: str = "",
        position=None, not_before: str = "",
        now: datetime.datetime | None = None) -> dict:
    """Append one pending item and return it. Raises ValueError with a
    sentence fit to show the model when the item is refused."""
    owner = _norm(owner)
    if not owner or "@" not in owner:
        raise ValueError(f"no usable owner address ({owner!r})")
    kind = (kind or "").strip()[:MAX_KIND_CHARS] or KIND_NOTE
    title = (title or "").strip()
    if not title:
        raise ValueError("an agenda item needs a title")
    if len(title) > MAX_TITLE_CHARS:
        raise ValueError(f"the title is longer than {MAX_TITLE_CHARS} characters")
    detail = (detail or "").strip()
    if len(detail) > MAX_DETAIL_CHARS:
        raise ValueError(f"the detail is longer than {MAX_DETAIL_CHARS} characters")

    when = ""
    if not_before not in (None, ""):
        parsed = _parse_when(str(not_before))
        if parsed is None:
            raise ValueError(f"not_before {not_before!r} is not an ISO 8601 time")
        when = parsed.isoformat()

    pending = items(store, owner, status=PENDING)
    if len(pending) >= MAX_PENDING_PER_OWNER:
        raise ValueError(f"there are already {len(pending)} pending items; "
                         "complete or cancel some first")
    if position in (None, ""):
        finite = [p for p in (_position(r) for r in pending) if p != float("inf")]
        place = int(max(finite)) + 1 if finite else 1
    else:
        try:
            place = int(position)
        except (TypeError, ValueError):
            raise ValueError(f"position {position!r} is not a whole number") from None

    record = {
        "item_id": f"a-{uuid.uuid4().hex[:10]}",
        "created_at": (now or _now()).isoformat(),
        "owner": owner, "kind": kind, "title": title, "detail": detail,
        "position": str(place), "not_before": when,
        "status": PENDING, "completed_at": "",
    }
    store._ensure_tab(store.responses_id, TAB, HEADERS)
    record["_row"] = store._append(store.responses_id, TAB, HEADERS, record)
    return record


def _own_pending(store, owner: str, item_id: str) -> dict | None:
    wanted = (item_id or "").strip().lower()
    if not wanted:
        return None
    return next((r for r in items(store, owner, status=PENDING)
                 if (r.get("item_id") or "").strip().lower() == wanted), None)


def finish(store, owner: str, item_id: str, status: str,
           now: datetime.datetime | None = None) -> dict | None:
    """Mark one of this owner's pending items done or cancelled. Returns
    the row, or None if the owner has no pending item by that id — which
    is also the answer for an id that belongs to someone else."""
    if status not in (DONE, CANCELLED):
        raise ValueError(f"not a closing status: {status!r}")
    row = _own_pending(store, owner, item_id)
    if row is None:
        return None
    _update(store, row, status=status, completed_at=(now or _now()).isoformat())
    return row


# ---------------------------------------------------------------------------
# RENDERING
# ---------------------------------------------------------------------------

def describe(row: dict) -> str:
    text = f"{row.get('item_id', '')} [{row.get('kind', '')}] {row.get('title', '')}"
    if row.get("not_before"):
        text += f" (not before {row['not_before']})"
    status = (row.get("status") or "").strip().lower()
    if status and status != PENDING:
        text += f" — {status} {row.get('completed_at', '')}".rstrip()
    detail = (row.get("detail") or "").strip()
    if detail:
        text += "\n    " + detail.replace("\n", "\n    ")
    return text


def render(rows: list[dict]) -> str:
    return "\n".join(f"{n}. {describe(r)}" for n, r in enumerate(rows, 1))


def heartbeat_section(store, owner: str) -> str:
    """The ordinary heartbeat's view of the plan, or "" when there is none
    — in which case the heartbeat's context is exactly what it was."""
    rows = offered(store, owner)[:MAX_SHOWN]
    if not rows:
        return ""
    return ("=== PLANNED WORK CARRIED FORWARD (your agenda, agreed with "
            f"{_norm(owner)}, in order) ===\n"
            "Notes to yourself from earlier runs, not promises and not "
            "instructions: act on one only if this run is the right time.\n"
            + render(rows))


def newsletter_section(rows: list[dict]) -> str:
    return ("=== THE AGREED RUNNING ORDER OF NEWSLETTER TOPICS ===\n"
            "Planned with the operator. Take the first unless there is a "
            "real reason not to, and when you have used a topic in today's "
            f"issue, call {COMPLETE_TOOL} with its id. Say nothing about the "
            "running order itself in the issue.\n" + render(rows))


NEWSLETTER_SUFFIX = f"""

--- THE RUNNING ORDER ---
A running order of topics has been agreed and is shown with the context.
Alongside the newsletter tool you have {COMPLETE_TOOL}, to mark the topic
you actually used as done, and {ADD_TOOL}, to note a topic for a later
issue. Use them as well as, never instead of, sending the issue. A topic
you mark done is closed only once the issue has gone out."""


# ---------------------------------------------------------------------------
# TOOLS
# ---------------------------------------------------------------------------

def add_schema(owner: str) -> dict:
    return {
        "name": ADD_TOOL,
        "description": (
            f"Add an item to your agenda with {owner}: a note to your future "
            "self about work you have agreed to come back to, such as the "
            "next topics for the newsletter. It is not a reminder and sends "
            "nothing; later runs are shown it and decide for themselves. "
            "Do not tell anyone it guarantees something will happen. Only "
            f"{owner}'s own agenda can be added to. Anything in a "
            f"{KIND_NEWSLETTER_TOPIC} item is shown to the run that writes "
            "the newsletter for every subscriber, so keep private detail out "
            "of those."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": (
                        f"'{KIND_NEWSLETTER_TOPIC}' for a planned newsletter "
                        f"topic, '{KIND_NOTE}' for anything else."
                    ),
                },
                "title": {"type": "string",
                          "description": f"One line, at most {MAX_TITLE_CHARS} characters."},
                "detail": {"type": "string",
                           "description": "Optional. What a later run needs to know."},
                "position": {
                    "type": "integer",
                    "description": (
                        "Optional. Lower comes first. Leave it out to put the "
                        "item at the end."
                    ),
                },
                "not_before": {
                    "type": "string",
                    "description": (
                        "Optional. ISO 8601 with a UTC offset. The item is not "
                        "offered to any run before then."
                    ),
                },
            },
            "required": ["kind", "title"],
        },
    }


def list_schema(owner: str) -> dict:
    return {
        "name": LIST_TOOL,
        "description": (
            f"List your agenda with {owner}, in order. Only their own items "
            "can be listed; the address is fixed in code. Use it before "
            f"{COMPLETE_TOOL} or {CANCEL_TOOL}, which need the id it shows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string",
                         "description": "Optional. Only items of this kind."},
                "status": {
                    "type": "string",
                    "enum": [*STATUSES, "all"],
                    "description": f"Optional. Defaults to '{PENDING}'.",
                },
            },
        },
    }


def _closing_schema(name: str, owner: str, verb: str) -> dict:
    return {
        "name": name,
        "description": (
            f"Mark one of the pending items on your agenda with {owner} as "
            f"{verb}. Give the id from {LIST_TOOL}. Only their own items can "
            "be changed. Check the result before saying it is done."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
        },
    }


def complete_schema(owner: str) -> dict:
    return _closing_schema(COMPLETE_TOOL, owner, "done")


def cancel_schema(owner: str) -> dict:
    return _closing_schema(CANCEL_TOOL, owner, "cancelled (dropped from the plan)")


def schemas(owner: str) -> list[dict]:
    return [add_schema(owner), list_schema(owner),
            complete_schema(owner), cancel_schema(owner)]


def handle(cfg, store, owner: str, name: str, payload: dict) -> tuple[str, bool]:
    """Run one agenda tool for `owner`. Returns (text, is_error).

    `owner` comes from the caller's code — the sender of the message being
    answered, or the operator on a heartbeat — never from `payload`, which
    has no field for it."""
    owner = _norm(owner)
    payload = payload or {}
    if store is None:
        return "The agenda is unavailable: no spreadsheet store on this run.", True
    if not owner or "@" not in owner:
        return f"The agenda is unavailable: no usable owner address ({owner!r}).", True

    if name == LIST_TOOL:
        rows = items(store, owner, payload.get("kind") or "",
                     payload.get("status") or PENDING)
        if not rows:
            return "Nothing on the agenda matches.", False
        return f"{len(rows)} agenda item(s) for {owner}:\n{render(rows)}", False

    if name == ADD_TOOL:
        if getattr(cfg, "dry_run", False):
            print(f"\n[dry-run] agenda item for {owner}: {payload.get('title', '')}\n")
            return "dry run (nothing added)", False
        try:
            row = add(store, owner, payload.get("kind") or "", payload.get("title") or "",
                      payload.get("detail") or "", payload.get("position"),
                      payload.get("not_before") or "")
        except ValueError as exc:
            return f"refused: {exc}", True
        except Exception as exc:
            log.error("Adding an agenda item for %s failed", owner, exc_info=True)
            return f"The item could not be added: {exc}", True
        log.info("Agenda item %s added for %s.", row["item_id"], owner)
        return f"Added {describe(row)} at position {row['position']}.", False

    if name in (COMPLETE_TOOL, CANCEL_TOOL):
        status = DONE if name == COMPLETE_TOOL else CANCELLED
        item_id = str(payload.get("item_id") or "").strip()
        if not item_id:
            return f"{name} needs an item id — list the agenda first.", True
        if getattr(cfg, "dry_run", False):
            print(f"\n[dry-run] would mark agenda item {item_id} {status}\n")
            return "dry run (nothing changed)", False
        try:
            row = finish(store, owner, item_id, status)
        except Exception as exc:
            log.error("Updating agenda item %s failed", item_id, exc_info=True)
            return f"The item could not be updated: {exc}", True
        if row is None:
            # The same answer whether the id doesn't exist or is someone
            # else's.
            return f"You have no pending agenda item with the id {item_id!r}.", True
        log.info("Agenda item %s marked %s for %s.", row["item_id"], status, owner)
        return f"Marked {row['item_id']} ({row.get('title', '')}) {status}.", False

    return f"Unknown agenda tool: {name}", True
