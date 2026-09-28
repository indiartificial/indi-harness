"""
tests/test_agenda.py

The agenda is a plan carried between runs: items stored in a tab, read
back in order, closed when done. What these tests hold it to:

  * an item written is the item read back, in position-then-age order
  * an item with a not_before in the future is not offered yet
  * one correspondent can never see, complete or cancel another's items —
    tested by trying, the way the reminder scoping is
  * the tools reach the model only when check_and_reply.py has named an
    owner, and never on a reply shared with others
  * on the newsletter run, a topic is marked done only once the issue
    has actually gone out
  * and with an empty agenda, the heartbeat and the newsletter run are
    exactly what they were before the agenda existed

This directory is protected — Indi can read it but not edit it.
"""

import datetime
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import agenda
import claude_api
import newsletter
import outbound
import terms_gate


UTC = datetime.timezone.utc
ANN = "ann@example.com"
BOB = "bob@example.com"
OPERATOR = terms_gate.OPERATOR_ADDRESS
T0 = datetime.datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# FAKES
# --------------------------------------------------------------------------

_RANGE = re.compile(r"^(?P<tab>.+)!A(?P<row>\d+):")


class FakeSheets:
    """Enough of the Sheets client for agenda._update's whole-row write."""

    def __init__(self, store):
        self.store = store

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def update(self, spreadsheetId, range, valueInputOption, body):
        m = _RANGE.match(range)
        assert m and m.group("tab") == agenda.TAB, range
        n = int(m.group("row"))
        row = next(r for r in self.store.tabs[agenda.TAB] if r["_row"] == n)
        row.update(dict(zip(agenda.HEADERS, body["values"][0])))
        return self

    def execute(self):
        return {}


class FakeStore:
    def __init__(self):
        self.responses_id = "responses"
        self.sheets = FakeSheets(self)
        self.tabs: dict[str, list[dict]] = {}

    def _ensure_tab(self, spreadsheet_id, tab, headers):
        self.tabs.setdefault(tab, [])

    def _load(self, spreadsheet_id, tab):
        return [dict(r) for r in self.tabs.setdefault(tab, [])]

    def _append(self, spreadsheet_id, tab, headers, record):
        rows = self.tabs.setdefault(tab, [])
        stored = {h: str(record.get(h, "")) for h in headers}
        stored["_row"] = len(rows) + 2
        rows.append(stored)
        return stored["_row"]

    # What outbound._context reads besides the tabs.
    def recent_messages(self, n):
        return []

    def responses_for(self, rows, **k):
        return {}

    def recent_journal(self, limit):
        return []

    def recent_outbound(self, n):
        return []


CFG = SimpleNamespace(dry_run=False, max_tokens=1000, heartbeat_journal_entries=5)


def _add(store, owner, title, minutes=0, **k):
    return agenda.add(store, owner, k.pop("kind", agenda.KIND_NOTE), title,
                      now=T0 + datetime.timedelta(minutes=minutes), **k)


# --------------------------------------------------------------------------
# STORAGE
# --------------------------------------------------------------------------

def test_an_item_round_trips_through_the_tab():
    store = FakeStore()
    added = agenda.add(store, "Ann@Example.com", agenda.KIND_NEWSLETTER_TOPIC,
                       "Why reminders expire", "The grace window, and why.",
                       not_before="2026-09-01T00:00:00Z", now=T0)
    [row] = agenda.items(store, ANN)
    assert row["item_id"] == added["item_id"] and row["item_id"].startswith("a-")
    assert row["owner"] == ANN
    assert row["kind"] == agenda.KIND_NEWSLETTER_TOPIC
    assert row["title"] == "Why reminders expire"
    assert row["detail"] == "The grace window, and why."
    assert row["status"] == agenda.PENDING and row["completed_at"] == ""
    assert row["created_at"] == T0.isoformat()
    assert row["not_before"] == "2026-09-01T00:00:00+00:00"
    assert row["position"] == "1"


def test_pending_items_come_back_by_position_then_age():
    store = FakeStore()
    _add(store, ANN, "third", minutes=1, position=5)
    _add(store, ANN, "first", minutes=2, position=1)
    _add(store, ANN, "second, older tie", minutes=0, position=3)
    _add(store, ANN, "second, newer tie", minutes=3, position=3)
    # No position: after everything already pending.
    last = _add(store, ANN, "appended", minutes=4)
    assert last["position"] == "6"
    assert [r["title"] for r in agenda.items(store, ANN)] == [
        "first", "second, older tie", "second, newer tie", "third", "appended"]


def test_not_before_holds_an_item_back_until_its_time():
    store = FakeStore()
    _add(store, ANN, "now")
    _add(store, ANN, "later", not_before="2026-10-05T09:00:00+00:00")
    assert [r["title"] for r in agenda.offered(store, ANN, now=T0)] == ["now"]
    # Still listed for its owner, and offered once the time has come.
    assert len(agenda.items(store, ANN)) == 2
    later = datetime.datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    assert [r["title"] for r in agenda.offered(store, ANN, now=later)] == ["now", "later"]


def test_bad_input_is_refused_rather_than_stored():
    store = FakeStore()
    for kwargs in ({"title": ""}, {"title": "x" * 500},
                   {"title": "ok", "not_before": "next tuesday"},
                   {"title": "ok", "position": "first"}):
        text, err = agenda.handle(CFG, store, ANN, agenda.ADD_TOOL,
                                  {"kind": "note", **kwargs})
        assert err and text.startswith("refused:")
    assert agenda.items(store, ANN, status="all") == []


def test_complete_and_cancel_close_an_item_and_keep_the_row():
    store = FakeStore()
    a = _add(store, ANN, "done soon")
    b = _add(store, ANN, "dropped")
    text, err = agenda.handle(CFG, store, ANN, agenda.COMPLETE_TOOL, {"item_id": a["item_id"]})
    assert not err and "done" in text
    text, err = agenda.handle(CFG, store, ANN, agenda.CANCEL_TOOL, {"item_id": b["item_id"]})
    assert not err and "cancelled" in text

    assert agenda.items(store, ANN) == []
    closed = {r["item_id"]: r for r in agenda.items(store, ANN, status="all")}
    assert closed[a["item_id"]]["status"] == agenda.DONE
    assert closed[b["item_id"]]["status"] == agenda.CANCELLED
    assert closed[a["item_id"]]["completed_at"]
    # A closed item can't be closed again.
    _, err = agenda.handle(CFG, store, ANN, agenda.COMPLETE_TOOL, {"item_id": a["item_id"]})
    assert err


def test_list_filters_by_kind_and_status():
    store = FakeStore()
    _add(store, ANN, "topic", kind=agenda.KIND_NEWSLETTER_TOPIC)
    n = _add(store, ANN, "note")
    agenda.finish(store, ANN, n["item_id"], agenda.DONE)
    text, _ = agenda.handle(CFG, store, ANN, agenda.LIST_TOOL,
                            {"kind": agenda.KIND_NEWSLETTER_TOPIC})
    assert "topic" in text and "note" not in text
    text, _ = agenda.handle(CFG, store, ANN, agenda.LIST_TOOL, {"status": "done"})
    assert "note" in text and "topic" not in text


# --------------------------------------------------------------------------
# SCOPING
# --------------------------------------------------------------------------

def test_one_owner_cannot_see_anothers_items():
    store = FakeStore()
    _add(store, BOB, "bob's private plan")
    assert agenda.items(store, ANN, status="all") == []
    text, err = agenda.handle(CFG, store, ANN, agenda.LIST_TOOL, {"status": "all"})
    assert "bob's private plan" not in text
    # An owner argument in the payload is not something the tool reads.
    text, _ = agenda.handle(CFG, store, ANN, agenda.LIST_TOOL, {"owner": BOB})
    assert "bob's private plan" not in text


def test_one_owner_cannot_complete_or_cancel_anothers_item():
    store = FakeStore()
    bobs = _add(store, BOB, "bob's plan")
    for tool in (agenda.COMPLETE_TOOL, agenda.CANCEL_TOOL):
        text, err = agenda.handle(CFG, store, ANN, tool, {"item_id": bobs["item_id"]})
        assert err
        # Same answer as for an id that doesn't exist.
        missing, _ = agenda.handle(CFG, store, ANN, tool, {"item_id": "a-nonexistent"})
        assert text == missing.replace("a-nonexistent", bobs["item_id"])
    assert agenda.finish(store, ANN, bobs["item_id"], agenda.DONE) is None
    [row] = agenda.items(store, BOB)
    assert row["status"] == agenda.PENDING


def test_an_added_item_belongs_to_the_caller_whatever_the_payload_says():
    store = FakeStore()
    agenda.handle(CFG, store, ANN, agenda.ADD_TOOL,
                  {"kind": "note", "title": "mine", "owner": BOB})
    assert agenda.items(store, BOB) == []
    assert [r["title"] for r in agenda.items(store, ANN)] == ["mine"]


# --------------------------------------------------------------------------
# THE REPLY PATH
# --------------------------------------------------------------------------

def _tool_names(**k):
    cfg = SimpleNamespace(enable_web_search=False, enable_web_fetch=False,
                          enable_code_execution=False, enable_history_search=False,
                          enable_outbound=False,
                          betas=lambda **kw: [])
    tools, _ = claude_api.build_tools(cfg, trusted=True, have_store=True, **k)
    return {t["name"] for t in tools}


def test_agenda_tools_are_offered_only_to_a_named_owner():
    assert not agenda.TOOL_NAMES & _tool_names()
    assert agenda.TOOL_NAMES <= _tool_names(agenda_owner=OPERATOR)


def test_agenda_tools_are_not_offered_on_a_shared_reply():
    assert not agenda.TOOL_NAMES & _tool_names(agenda_owner=OPERATOR, shared_reply=True)


# --------------------------------------------------------------------------
# THE HEARTBEAT
# --------------------------------------------------------------------------

def test_an_empty_agenda_leaves_the_heartbeat_context_unchanged():
    store = FakeStore()
    assert outbound._context(CFG, store) == "(No correspondence yet. This is a quiet start.)"


def test_the_heartbeat_shows_the_operators_pending_items_in_order():
    store = FakeStore()
    _add(store, OPERATOR, "second", minutes=1, position=2)
    _add(store, OPERATOR, "first", minutes=2, position=1)
    _add(store, BOB, "not the operator's")
    _add(store, OPERATOR, "not yet", not_before="2099-01-01T00:00:00Z")
    text = outbound._context(CFG, store)
    assert "PLANNED WORK CARRIED FORWARD" in text
    assert text.index("first") < text.index("second")
    assert "not the operator's" not in text and "not yet" not in text


def _heartbeat_capturing_run_issue(store, monkeypatch):
    seen = {}
    monkeypatch.setattr(outbound, "deliver_due_reminders", lambda *a, **k: [])
    monkeypatch.setattr(newsletter, "due", lambda *a, **k: (True, "owed"))
    monkeypatch.setattr(newsletter, "list_subscribers", lambda *a, **k: [ANN])
    monkeypatch.setattr(outbound, "_context_for", lambda *a, **k: "CONTEXT")
    monkeypatch.setattr(newsletter, "run_issue",
                        lambda *a, **k: seen.update(k) or "newsletter sent")
    outbound.run_heartbeat(None, CFG, None, store, "SYS", set())
    return seen


def test_an_empty_agenda_leaves_the_newsletter_run_as_it_was(monkeypatch):
    seen = _heartbeat_capturing_run_issue(FakeStore(), monkeypatch)
    assert seen == {"context": "CONTEXT"}


def test_the_newsletter_run_is_handed_the_operators_topics(monkeypatch):
    store = FakeStore()
    _add(store, OPERATOR, "a note, not a topic")
    t = _add(store, OPERATOR, "topic", kind=agenda.KIND_NEWSLETTER_TOPIC)
    seen = _heartbeat_capturing_run_issue(store, monkeypatch)
    assert seen["agenda_owner"] == OPERATOR
    assert [r["item_id"] for r in seen["topics"]] == [t["item_id"]]


# --------------------------------------------------------------------------
# THE NEWSLETTER RUN
# --------------------------------------------------------------------------

def _call(name, **inp):
    return SimpleNamespace(type="tool_use", name=name, input=inp, id=f"tu-{name}")


class Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.seen = []
        self.messages = self

    def stream(self, **kw):
        self.seen.append(kw)
        content, stop = self.responses.pop(0)
        final = SimpleNamespace(content=content, usage=None, stop_reason=stop)

        class Ctx:
            def __enter__(s):
                return SimpleNamespace(get_final_message=lambda: final)

            def __exit__(s, *a):
                return False
        return Ctx()


@pytest.fixture
def one_subscriber(monkeypatch):
    monkeypatch.setattr(newsletter, "eligible_recipients", lambda *a, **k: [ANN])


def _issue(**k):
    return _call(newsletter.NEWSLETTER_TOOL, subject="Monday", body="an issue", **k)


def test_without_topics_the_issue_run_offers_one_tool(one_subscriber, monkeypatch):
    monkeypatch.setattr(newsletter, "send_issue",
                        lambda *a, **k: "newsletter 'Monday' sent to 1 subscriber(s)")
    client = Client(([_issue()], "tool_use"))
    newsletter.run_issue(client, CFG, None, FakeStore(), "SYS", "m", context="CTX")
    [call] = client.seen
    assert [t["name"] for t in call["tools"]] == [newsletter.NEWSLETTER_TOOL]
    assert call["messages"][0]["content"] == "CTX"
    assert agenda.NEWSLETTER_SUFFIX not in call["system"]


def test_a_used_topic_is_marked_done_once_the_issue_goes_out(one_subscriber, monkeypatch):
    store = FakeStore()
    first = _add(store, OPERATOR, "first topic", kind=agenda.KIND_NEWSLETTER_TOPIC)
    second = _add(store, OPERATOR, "second topic", kind=agenda.KIND_NEWSLETTER_TOPIC)
    monkeypatch.setattr(newsletter, "send_issue",
                        lambda *a, **k: "newsletter 'Monday' sent to 1 subscriber(s)")
    client = Client(([_call(agenda.COMPLETE_TOOL, item_id=first["item_id"]), _issue()],
                     "tool_use"))
    outcome = newsletter.run_issue(client, CFG, None, store, "SYS", "m", context="CTX",
                                   agenda_owner=OPERATOR,
                                   topics=agenda.offered(store, OPERATOR))
    assert "marked " + first["item_id"] in outcome
    offered = {t["name"] for t in client.seen[0]["tools"]}
    assert offered == {newsletter.NEWSLETTER_TOOL, agenda.COMPLETE_TOOL, agenda.ADD_TOOL}
    shown = client.seen[0]["messages"][0]["content"]
    assert shown.index("first topic") < shown.index("second topic")
    assert [r["item_id"] for r in agenda.items(store, OPERATOR)] == [second["item_id"]]


def test_a_topic_marked_after_sending_is_still_closed(one_subscriber, monkeypatch):
    store = FakeStore()
    t = _add(store, OPERATOR, "topic", kind=agenda.KIND_NEWSLETTER_TOPIC)
    monkeypatch.setattr(newsletter, "send_issue",
                        lambda *a, **k: "newsletter 'Monday' sent to 1 subscriber(s)")
    client = Client(([_issue()], "tool_use"),
                    ([_call(agenda.COMPLETE_TOOL, item_id=t["item_id"])], "tool_use"),
                    ([], "end_turn"))
    newsletter.run_issue(client, CFG, None, store, "SYS", "m", agenda_owner=OPERATOR,
                         topics=agenda.offered(store, OPERATOR))
    assert agenda.items(store, OPERATOR) == []


def test_a_topic_is_left_pending_when_the_issue_does_not_go_out(one_subscriber, monkeypatch):
    store = FakeStore()
    t = _add(store, OPERATOR, "topic", kind=agenda.KIND_NEWSLETTER_TOPIC)
    monkeypatch.setattr(newsletter, "send_issue",
                        lambda *a, **k: "refused: an issue is already recorded for today")
    client = Client(([_call(agenda.COMPLETE_TOOL, item_id=t["item_id"]), _issue()],
                     "tool_use"))
    outcome = newsletter.run_issue(client, CFG, None, store, "SYS", "m",
                                   agenda_owner=OPERATOR,
                                   topics=agenda.offered(store, OPERATOR))
    assert "agenda unchanged" in outcome
    assert [r["item_id"] for r in agenda.items(store, OPERATOR)] == [t["item_id"]]


def test_the_newsletter_run_cannot_close_an_item_it_was_not_shown(one_subscriber, monkeypatch):
    store = FakeStore()
    t = _add(store, OPERATOR, "topic", kind=agenda.KIND_NEWSLETTER_TOPIC)
    bobs = _add(store, BOB, "bob's", kind=agenda.KIND_NEWSLETTER_TOPIC)
    monkeypatch.setattr(newsletter, "send_issue",
                        lambda *a, **k: "newsletter 'Monday' sent to 1 subscriber(s)")
    client = Client(([_call(agenda.COMPLETE_TOOL, item_id=bobs["item_id"]), _issue()],
                     "tool_use"),
                    ([], "end_turn"))
    newsletter.run_issue(client, CFG, None, store, "SYS", "m", agenda_owner=OPERATOR,
                         topics=agenda.offered(store, OPERATOR))
    assert [r["item_id"] for r in agenda.items(store, BOB)] == [bobs["item_id"]]
    assert [r["item_id"] for r in agenda.items(store, OPERATOR)] == [t["item_id"]]
