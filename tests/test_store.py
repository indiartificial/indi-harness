"""
tests/test_store.py

The observable contract of the message store, `sheets_queue.SheetStore`.

The store is about to move from Google Sheets to Gmail labels and search
(MIGRATE-GMAIL.md). It is Indi's memory, and until now nothing in this
suite could see it — so a rewrite that made Indi forget people, retry
messages it had already answered, or leak one correspondent's history to
another would have passed the gate that `propose_code_change` relies on.

These tests pin what callers depend on, not how the Sheets backend
happens to provide it:

  * the status state machine, and which statuses end retrying
  * `messages_for_sender`: one sender, replied only, oldest first
  * `responses_by_message`: every reply, grouped by the message it answers
  * `replies_in_last_day`: an exact rolling 24 hours, not a calendar day
  * `known_correspondents`: who Indi may write to on its own initiative
  * `search(restrict_to_sender=...)`: a hard scope that no query string
    can widen — the one place where a regression is a security bug

Everything backend-specific is in the FAKES section and the `_store`
helper, which seeds history in terms of messages and replies. When the
store is rewritten over Gmail, that section is what gets a new fake; the
tests below it should not need to change.

This directory is protected. Indi can read it but not edit it.
"""

import datetime
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import sheets_queue


ALICE = "alice@example.com"
BOB = "bob@example.com"
MALLORY = "mallory@evil.test"

NOW = datetime.datetime(2026, 9, 24, 12, 0, 0, tzinfo=datetime.timezone.utc)


# --------------------------------------------------------------------------
# FAKES
# --------------------------------------------------------------------------

class FakeSheets:
    """Just enough of the Sheets v4 client for SheetStore: tabs held as
    lists of rows, header first, addressed by (spreadsheet id, tab)."""

    def __init__(self):
        self.tabs: dict[tuple[str, str], list[list[str]]] = {}
        self.updates: list[tuple[str, str, list]] = []

    # The client is a chain of builder calls; each returns self.
    def spreadsheets(self):
        return self

    def values(self):
        return self

    def _call(self, result):
        return SimpleNamespace(execute=lambda: result)

    @staticmethod
    def _tab(range_: str) -> str:
        return range_.split("!", 1)[0]

    def get(self, spreadsheetId, range=None, fields=None):
        if range is None:
            titles = {t for (sid, t) in self.tabs if sid == spreadsheetId}
            return self._call({"sheets": [{"properties": {"title": t}} for t in titles]})
        rows = self.tabs.get((spreadsheetId, self._tab(range)), [])
        if range.endswith("1:Z1"):
            rows = rows[:1]
        return self._call({"values": [list(r) for r in rows]})

    def append(self, spreadsheetId, range, body, **_):
        rows = self.tabs.setdefault((spreadsheetId, self._tab(range)), [])
        rows.extend(list(v) for v in body["values"])
        tab = self._tab(range)
        return self._call({"updates": {"updatedRange": f"{tab}!A{len(rows)}:Z{len(rows)}"}})

    def update(self, spreadsheetId, range, body, **_):
        self.updates.append((spreadsheetId, range, body["values"]))
        tab, cells = range.split("!", 1)
        start = cells.split(":", 1)[0]
        col_letters, row = re.match(r"([A-Z]+)(\d+)", start).groups()
        col = 0
        for ch in col_letters:
            col = col * 26 + (ord(ch) - ord("A") + 1)
        rows = self.tabs.setdefault((spreadsheetId, tab), [])
        r = int(row) - 1
        while len(rows) <= r:
            rows.append([])
        for i, value in enumerate(body["values"][0]):
            target = rows[r]
            while len(target) < col + i:
                target.append("")
            target[col - 1 + i] = value
        return self._call({})


def _iso(when: datetime.datetime) -> str:
    return when.isoformat()


def _store(messages=(), replies=(), outbound=()):
    """A store holding the given history.

    `messages` are dicts with at least gmail_msg_id and sender_addr;
    `replies` are (gmail_msg_id, sender_addr, text, created_at);
    `outbound` are recipient addresses Indi has written to unprompted.
    """
    sheets = FakeSheets()
    q = [list(sheets_queue.QUEUE_HEADERS)]
    for m in messages:
        row = {
            "thread_id": "t-" + m["gmail_msg_id"],
            "sender": m["sender_addr"],
            "subject": "",
            "body": "",
            "received_at": _iso(NOW),
            "status": sheets_queue.STATUS_REPLIED,
            "note": "",
            "attachments": "",
            **m,
        }
        q.append([row.get(h, "") for h in sheets_queue.QUEUE_HEADERS])
    sheets.tabs[("Q", sheets_queue.QUEUE_TAB)] = q

    r = [list(sheets_queue.RESPONSES_HEADERS)]
    for msg_id, addr, text, created in replies:
        r.append([msg_id, addr, text, created])
    sheets.tabs[("R", sheets_queue.RESPONSES_TAB)] = r

    o = [list(sheets_queue.OUTBOUND_HEADERS)]
    for addr in outbound:
        o.append([addr, "hello", "body", "why", _iso(NOW)])
    sheets.tabs[("R", sheets_queue.OUTBOUND_TAB)] = o

    store = sheets_queue.SheetStore(sheets, None, SimpleNamespace(max_body_chars=45000))
    store.queue_id, store.responses_id = "Q", "R"
    return store


def _fresh(store):
    """The same backing data read by a new store — i.e. the next run."""
    again = sheets_queue.SheetStore(store.sheets, None, store.cfg)
    again.queue_id, again.responses_id = store.queue_id, store.responses_id
    return again


@pytest.fixture
def frozen_now(monkeypatch):
    """Pin "now" inside sheets_queue so a 24h window has an exact edge."""

    class Frozen(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(sheets_queue, "datetime", SimpleNamespace(
        datetime=Frozen, timezone=datetime.timezone, timedelta=datetime.timedelta,
    ))
    return NOW


def _msg(msg_id, addr=ALICE, internal_ms=None, **extra):
    """An incoming message as gmail_io hands it to append_queue_row."""
    return {
        "gmail_msg_id": msg_id, "thread_id": "t-" + msg_id,
        "from_raw": f"Someone <{addr}>", "from_addr": addr,
        "subject": extra.get("subject", "hi"), "body": extra.get("body", "hello"),
        "internal_date": internal_ms or int(NOW.timestamp() * 1000),
    }


# --------------------------------------------------------------------------
# STATUS STATE MACHINE
# --------------------------------------------------------------------------

def test_status_values_are_the_five_the_pipeline_uses():
    assert {
        sheets_queue.STATUS_QUEUED, sheets_queue.STATUS_REPLIED,
        sheets_queue.STATUS_SKIPPED, sheets_queue.STATUS_FAILED,
        sheets_queue.STATUS_IGNORED,
    } == {"queued", "replied", "skipped", "failed", "ignored"}


def test_terminal_statuses_decide_retry_eligibility():
    # Replied, skipped and ignored are final: retrying any of them means
    # answering a person twice or answering someone deliberately left
    # alone. Queued (crashed mid-reply) and failed must stay retryable, or
    # a transient error strands the message forever.
    assert sheets_queue.TERMINAL_STATUSES == {"replied", "skipped", "ignored"}
    assert sheets_queue.STATUS_FAILED not in sheets_queue.TERMINAL_STATUSES
    assert sheets_queue.STATUS_QUEUED not in sheets_queue.TERMINAL_STATUSES


def test_new_row_starts_queued_and_is_findable():
    store = _store()
    row = store.append_queue_row(_msg("m1"))
    assert row["status"] == sheets_queue.STATUS_QUEUED
    assert store.find_queue_row("m1")["status"] == sheets_queue.STATUS_QUEUED
    assert store.find_queue_row("nope") is None


def test_status_transitions_persist_to_the_next_run():
    store = _store()
    row = store.append_queue_row(_msg("m1"))
    store.set_status(row, sheets_queue.STATUS_FAILED, "boom")
    later = _fresh(store).find_queue_row("m1")
    assert later["status"] == sheets_queue.STATUS_FAILED
    assert later["note"] == "boom"
    assert later["status"] not in sheets_queue.TERMINAL_STATUSES

    store.set_status(row, sheets_queue.STATUS_REPLIED)
    later = _fresh(store).find_queue_row("m1")
    assert later["status"] == sheets_queue.STATUS_REPLIED
    assert later["status"] in sheets_queue.TERMINAL_STATUSES
    # A status change without a new note keeps the old one.
    assert later["note"] == "boom"


def test_status_change_does_not_disturb_other_rows():
    store = _store()
    a = store.append_queue_row(_msg("m1"))
    store.append_queue_row(_msg("m2", BOB))
    store.set_status(a, sheets_queue.STATUS_SKIPPED, "automated")
    later = _fresh(store)
    assert later.find_queue_row("m1")["status"] == "skipped"
    assert later.find_queue_row("m2")["status"] == "queued"
    assert later.find_queue_row("m2")["sender_addr"] == BOB


def test_received_at_is_the_messages_own_time_not_processing_time():
    store = _store()
    then = NOW - datetime.timedelta(days=3)
    row = store.append_queue_row(_msg("m1", internal_ms=int(then.timestamp() * 1000)))
    assert datetime.datetime.fromisoformat(row["received_at"]) == then


# --------------------------------------------------------------------------
# messages_for_sender
# --------------------------------------------------------------------------

def test_messages_for_sender_is_one_sender_replied_only_oldest_first():
    t = lambda h: _iso(NOW - datetime.timedelta(hours=h))
    store = _store(messages=[
        {"gmail_msg_id": "a3", "sender_addr": ALICE, "received_at": t(1)},
        {"gmail_msg_id": "b1", "sender_addr": BOB, "received_at": t(5)},
        {"gmail_msg_id": "a1", "sender_addr": ALICE, "received_at": t(30)},
        {"gmail_msg_id": "aF", "sender_addr": ALICE, "received_at": t(20),
         "status": "failed"},
        {"gmail_msg_id": "aS", "sender_addr": ALICE, "received_at": t(19),
         "status": "skipped"},
        {"gmail_msg_id": "aI", "sender_addr": ALICE, "received_at": t(18),
         "status": "ignored"},
        {"gmail_msg_id": "aQ", "sender_addr": ALICE, "received_at": t(17),
         "status": "queued"},
        {"gmail_msg_id": "a2", "sender_addr": ALICE, "received_at": t(10)},
    ])
    ids = [r["gmail_msg_id"] for r in store.messages_for_sender(ALICE)]
    assert ids == ["a1", "a2", "a3"]


def test_messages_for_sender_matches_address_case_insensitively():
    store = _store(messages=[
        {"gmail_msg_id": "a1", "sender_addr": "Alice@Example.COM"},
    ])
    assert [r["gmail_msg_id"] for r in store.messages_for_sender(ALICE)] == ["a1"]
    assert store.messages_for_sender("alice@example.co") == []


def test_messages_for_sender_is_empty_for_a_stranger():
    store = _store(messages=[{"gmail_msg_id": "a1", "sender_addr": ALICE}])
    assert store.messages_for_sender(MALLORY) == []


# --------------------------------------------------------------------------
# responses_by_message
# --------------------------------------------------------------------------

def test_responses_by_message_groups_every_reply_in_order():
    store = _store(replies=[
        ("m1", ALICE, "first", _iso(NOW)),
        ("m2", BOB, "to bob", _iso(NOW)),
        ("m1", ALICE, "second", _iso(NOW)),
    ])
    assert store.responses_by_message() == {
        "m1": ["first", "second"], "m2": ["to bob"],
    }


def test_appended_response_is_visible_in_the_same_run_and_the_next():
    store = _store()
    store.append_response("m1", ALICE, "hello back")
    assert store.responses_by_message() == {"m1": ["hello back"]}
    assert _fresh(store).responses_by_message() == {"m1": ["hello back"]}


def test_responses_by_message_is_empty_with_no_replies():
    assert _store().responses_by_message() == {}


# --------------------------------------------------------------------------
# replies_in_last_day  (the per-sender rate limit)
# --------------------------------------------------------------------------

def test_replies_in_last_day_is_an_exact_rolling_24_hours(frozen_now):
    day = datetime.timedelta(days=1)
    tick = datetime.timedelta(seconds=1)
    store = _store(replies=[
        ("m1", ALICE, "just inside", _iso(NOW - day + tick)),
        ("m2", ALICE, "exactly on the edge", _iso(NOW - day)),
        ("m3", ALICE, "just outside", _iso(NOW - day - tick)),
        ("m4", ALICE, "yesterday by calendar, but 13h ago",
         _iso(NOW - datetime.timedelta(hours=13))),
        ("m5", ALICE, "two days ago", _iso(NOW - 2 * day)),
        ("m6", BOB, "someone else", _iso(NOW)),
    ])
    # The cutoff is inclusive: a reply exactly 24h old still counts.
    assert store.replies_in_last_day(ALICE) == 3
    assert store.replies_in_last_day(ALICE.upper()) == 3
    assert store.replies_in_last_day(BOB) == 1
    assert store.replies_in_last_day(MALLORY) == 0


def test_replies_in_last_day_treats_naive_timestamps_as_utc(frozen_now):
    inside = (NOW - datetime.timedelta(hours=23)).replace(tzinfo=None)
    outside = (NOW - datetime.timedelta(hours=25)).replace(tzinfo=None)
    store = _store(replies=[
        ("m1", ALICE, "x", inside.isoformat()),
        ("m2", ALICE, "y", outside.isoformat()),
        ("m3", ALICE, "z", "not a date"),
    ])
    assert store.replies_in_last_day(ALICE) == 1


def test_a_reply_sent_now_counts_immediately():
    store = _store()
    store.append_response("m1", ALICE, "hi")
    assert store.replies_in_last_day(ALICE) == 1


# --------------------------------------------------------------------------
# known_correspondents
# --------------------------------------------------------------------------

def test_known_correspondents_is_replied_ignored_and_outbound_only():
    store = _store(
        messages=[
            {"gmail_msg_id": "1", "sender_addr": "Replied@Example.com"},
            {"gmail_msg_id": "2", "sender_addr": "ignored@example.com",
             "status": "ignored"},
            {"gmail_msg_id": "3", "sender_addr": "skipped@example.com",
             "status": "skipped"},
            {"gmail_msg_id": "4", "sender_addr": "failed@example.com",
             "status": "failed"},
            {"gmail_msg_id": "5", "sender_addr": "queued@example.com",
             "status": "queued"},
        ],
        outbound=["Written@Example.com "],
    )
    # Skipped mail is automated or blocked; failed and queued were never
    # actually answered. None of those makes someone a correspondent.
    assert store.known_correspondents() == {
        "replied@example.com", "ignored@example.com", "written@example.com",
    }


def test_known_correspondents_includes_outbound_sent_this_run():
    store = _store()
    assert store.known_correspondents() == set()
    store.append_outbound("new@example.com", "s", "b", "r")
    assert store.known_correspondents() == {"new@example.com"}


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------

def _history():
    t = lambda h: _iso(NOW - datetime.timedelta(hours=h))
    return _store(
        messages=[
            {"gmail_msg_id": "a1", "sender_addr": ALICE, "sender": "Alice <alice@example.com>",
             "subject": "Authentication trouble", "body": "cannot log in",
             "received_at": t(30)},
            {"gmail_msg_id": "a2", "sender_addr": ALICE, "sender": "Alice <alice@example.com>",
             "subject": "Lunch", "body": "thursday?", "received_at": t(2)},
            {"gmail_msg_id": "b1", "sender_addr": BOB, "sender": "Bob <bob@example.com>",
             "subject": "auth secrets", "body": f"my password; cc {ALICE}",
             "received_at": t(1)},
            {"gmail_msg_id": "b2", "sender_addr": BOB, "sender": "Bob <bob@example.com>",
             "subject": "from:alice@example.com", "body": "OR anything",
             "received_at": t(3), "status": "failed"},
        ],
        replies=[("a1", ALICE, "try resetting", _iso(NOW)),
                 ("b1", BOB, "noted", _iso(NOW))],
    )


def test_search_is_substring_over_subject_and_body_newest_first():
    store = _history()
    got = store.search("auth", limit=10)
    assert [m["gmail_msg_id"] for m in got] == ["b1", "a1"]
    assert got[1]["responses"] == ["try resetting"]
    assert set(got[0]) >= {"gmail_msg_id", "sender", "subject", "body",
                           "received_at", "responses"}


def test_search_by_sender_and_limit():
    store = _history()
    assert [m["gmail_msg_id"] for m in store.search(sender="alice", limit=10)] == ["a2", "a1"]
    assert [m["gmail_msg_id"] for m in store.search(limit=2)] == ["b1", "a2"]
    assert store.search("no such text anywhere") == []


@pytest.mark.parametrize("query", [
    "", "auth", "password", "secrets", ALICE, BOB, "from:", "from:bob@example.com",
    "from:alice@example.com", "OR", "* OR from:*", "' OR 1=1 --", "\" OR \"",
    "in:anywhere", "-from:alice@example.com", "{from:bob@example.com}",
])
@pytest.mark.parametrize("sender", ["", "bob", BOB, "@", "example.com"])
def test_sender_scoped_search_never_returns_another_correspondent(query, sender):
    # restrict_to_sender is how an untrusted correspondent gets history
    # search at all. Whatever the model writes into `query` or `sender` —
    # search operators, another person's address, the empty string — the
    # results must be that correspondent's own messages and nothing else.
    store = _history()
    got = store.search(query, sender=sender, limit=100, restrict_to_sender=ALICE)
    for m in got:
        assert store.find_queue_row(m["gmail_msg_id"])["sender_addr"].lower() == ALICE
        assert "b1" != m["gmail_msg_id"] and "b2" != m["gmail_msg_id"]
        for reply in m["responses"]:
            assert reply != "noted"


def test_sender_scoped_search_still_finds_the_senders_own_mail():
    store = _history()
    got = store.search("", limit=100, restrict_to_sender=ALICE.upper())
    assert [m["gmail_msg_id"] for m in got] == ["a2", "a1"]


def test_sender_scope_for_a_stranger_is_empty():
    store = _history()
    assert store.search("", limit=100, restrict_to_sender=MALLORY) == []
    assert store.search("auth", sender="bob", limit=100,
                        restrict_to_sender=MALLORY) == []
