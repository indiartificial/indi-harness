"""
tests/test_newsletter.py

The newsletter is the only thing in the harness that sends the same mail
to several people at once, and the only heartbeat outcome that is not a
judgement call. Both of those change what is worth testing.

What these tests are about:

  * the trigger, which is a clock and a stored fact and nothing else: no
    issue before the target hour, an issue after it when none is
    recorded, and never a second one on the same UTC day
  * the list, which is opt-in: it starts as Steve and nobody else, and
    nothing about receiving or answering mail puts anyone on it
  * unsubscribing, which has to work from the recipient's side alone —
    the line is in every issue, and a subscriber who replies with it is
    off the list without a model being asked
  * the budget exemption, in both directions: an issue goes out with the
    self-initiated budget exhausted, and ordinary heartbeat mail still
    does not

The fake store here subclasses the real SheetStore and replaces only the
three methods that talk to Google, so the tab plumbing, the row
numbering and the counting queries under test are the real ones.

This directory is protected — Indi can read it but not edit it.
"""

import base64
import datetime
import re
import sys
from email import message_from_bytes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import correspondents
import newsletter
import outbound
import sheets_queue
import terms_gate


UTC = datetime.timezone.utc
OPERATOR = terms_gate.OPERATOR_ADDRESS
ADA = "ada@example.com"

# 14:00 UTC — an hour past the default target.
AFTERNOON = datetime.datetime(2026, 9, 21, 14, 0, tzinfo=UTC)
MORNING = datetime.datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def correspondent_db(tmp_path, monkeypatch):
    """The terms gate holds an issue for anyone who has not accepted, so
    the tests that send need a database in which the recipients have.
    The gate's own behaviour is tested in tests/test_terms_gate.py."""
    db = tmp_path / "correspondents.db"
    monkeypatch.setenv("INDI_CORRESPONDENTS_DB", str(db))
    people = correspondents.CorrespondentStore(db)
    people.record_acceptance(ADA, "0.3")
    people.close()
    return db


@pytest.fixture
def at_afternoon(monkeypatch):
    monkeypatch.setattr(newsletter, "_now", lambda: AFTERNOON)
    return AFTERNOON


@pytest.fixture
def at_morning(monkeypatch):
    monkeypatch.setattr(newsletter, "_now", lambda: MORNING)
    return MORNING


# --------------------------------------------------------------------------
# FAKES
# --------------------------------------------------------------------------

_HEADERS_BY_TAB = {
    newsletter.ISSUES_TAB: newsletter.ISSUES_HEADERS,
    newsletter.SUBSCRIBERS_TAB: newsletter.SUBSCRIBERS_HEADERS,
    sheets_queue.QUEUE_TAB: sheets_queue.QUEUE_HEADERS,
}

_RANGE = re.compile(r"^(?P<tab>.+)!(?P<col>[A-Z]+)(?P<row>\d+)")


class FakeSheets:
    """Enough of the Sheets client for the in-place cell updates."""

    def __init__(self, store):
        self.store = store

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def update(self, spreadsheetId, range, valueInputOption, body):
        match = _RANGE.match(range)
        assert match, f"unparseable range {range!r}"
        headers = _HEADERS_BY_TAB[match.group("tab")]
        first = _column_index(match.group("col"))
        row_number = int(match.group("row"))
        self.store.write_back(match.group("tab"), row_number, first,
                              body["values"][0], headers)
        return self

    def execute(self):
        return {}


def _column_index(letters: str) -> int:
    """'A' -> 0, 'F' -> 5. The inverse of sheets_queue._col_letter."""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


class FakeStore(sheets_queue.SheetStore):
    """The real SheetStore with the three Google-facing methods replaced.

    Everything else — `outbound_today`, `journal_today`, the header
    handling, `_col_letter` arithmetic — is the code that actually ships.
    """

    def __init__(self, cfg=None, unreadable=()):
        super().__init__(FakeSheets(self), None, cfg or _cfg())
        self.queue_id = "queue"
        self.responses_id = "responses"
        self.tabs: dict[str, list[dict]] = {}
        self.unreadable = set(unreadable)

    # -- replaced network calls -------------------------------------------

    def _ensure_tab(self, spreadsheet_id, tab, headers):
        if tab in self.unreadable:
            raise RuntimeError(f"{tab} is unreachable")
        self.tabs.setdefault(tab, [])

    def _ensure_headers(self, spreadsheet_id, tab, headers):
        self.tabs.setdefault(tab, [])

    def _load(self, spreadsheet_id, tab):
        if tab in self.unreadable:
            raise RuntimeError(f"{tab} is unreachable")
        return [dict(row) for row in self.tabs.setdefault(tab, [])]

    def _append(self, spreadsheet_id, tab, headers, record):
        if tab in self.unreadable:
            raise RuntimeError(f"{tab} is unreachable")
        rows = self.tabs.setdefault(tab, [])
        # Stringified and projected onto the headers, exactly as a real
        # append does: a field the tab has no column for is not stored.
        stored = {h: str(record.get(h, "")) for h in headers}
        stored["_row"] = len(rows) + 2
        rows.append(stored)
        return stored["_row"]

    # -- test helpers ------------------------------------------------------

    def write_back(self, tab, row_number, first_column, values, headers):
        for row in self.tabs.get(tab, []):
            if row["_row"] == row_number:
                for offset, value in enumerate(values):
                    row[headers[first_column + offset]] = value
                return
        raise AssertionError(f"no row {row_number} in {tab}")

    def rows(self, tab):
        return self.tabs.get(tab, [])


class FakeGmail:
    def __init__(self, fail_for=()):
        self.sent = []
        self.drafted = []
        self.read = []
        self.fail_for = set(fail_for)

    def users(self):
        return self

    def messages(self):
        return self

    def drafts(self):
        return self

    def send(self, userId, body):
        message = _decode(body)
        if message["To"] in self.fail_for:
            raise RuntimeError("gmail said no")
        self.sent.append(body)
        return self

    def create(self, userId, body):
        self.drafted.append(body["message"])
        return self

    def modify(self, userId, id, body):
        self.read.append(id)
        return self

    def execute(self):
        return {"id": "x"}


class FakeClaude:
    """Answers with whichever tool call the test asks for.

    `stream` is what both the heartbeat and the newsletter run use; the
    recorded kwargs are how a test checks which tools the model was
    actually offered.
    """

    def __init__(self, tool=None, tool_input=None, text=""):
        self.tool = tool
        self.tool_input = tool_input or {}
        self.text = text
        self.calls = []
        self.messages = self

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        blocks = []
        if self.text:
            blocks.append(type("B", (), {"type": "text", "text": self.text})())
        if self.tool:
            blocks.append(type("B", (), {
                "type": "tool_use", "name": self.tool, "input": self.tool_input,
            })())
        response = type("R", (), {"content": blocks, "usage": None})()
        outer = self

        class _Stream:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return response

        return _Stream()

    @property
    def offered_tools(self):
        return [t["name"] for t in self.calls[-1]["tools"]]


def _cfg(**over):
    base = dict(
        dry_run=False, send_automatically=True, max_body_chars=45000,
        max_tokens=2000, model="claude-sonnet-5",
        outbound_allow_new_recipients=False, enable_outbound=True,
        outbound_max_per_day=5, outbound_max_per_recipient_per_day=1,
        journal_max_per_day=24, heartbeat_journal_entries=10,
        newsletter_hour_utc=13, newsletter_max_subscribers=50,
    )
    base.update(over)
    return type("C", (), base)()


def _decode(mime):
    return message_from_bytes(base64.urlsafe_b64decode(mime["raw"]))


def _msg(body="unsubscribe", sender=OPERATOR, subject="newsletter"):
    return {
        "gmail_msg_id": "m1", "thread_id": "t1", "headers": {},
        "subject": subject, "from_raw": sender, "from_addr": sender,
        "reply_to_raw": sender, "reply_to_addr": sender,
        "message_id": "<m1@example.com>", "references": "",
        "body": body, "body_truncated": False, "quoted": "",
        "quoted_truncated": False, "attachment_refs": [], "internal_date": 0,
    }


def _people():
    return correspondents.CorrespondentStore()


# --------------------------------------------------------------------------
# THE SUBSCRIBER LIST
# --------------------------------------------------------------------------

def test_the_list_starts_as_steve_and_nobody_else():
    store = FakeStore()
    assert newsletter.list_subscribers(store) == [OPERATOR]
    # And the seed is a real stored row, not a value invented on each read.
    assert [r["address"] for r in store.rows(newsletter.SUBSCRIBERS_TAB)] == [OPERATOR]
    assert newsletter.list_subscribers(store) == [OPERATOR]


def test_nobody_is_subscribed_by_writing_in(at_afternoon):
    """Opt-in means opt-in. Corresponding with Indi, being replied to and
    being written to all leave the list exactly as it was."""
    store = FakeStore()
    store.append_outbound(ADA, "hello", "body", "because")
    store._append(store.queue_id, sheets_queue.QUEUE_TAB,
                  sheets_queue.QUEUE_HEADERS,
                  {"gmail_msg_id": "m1", "sender_addr": ADA,
                   "status": sheets_queue.STATUS_REPLIED})
    assert ADA in store.known_correspondents()
    assert newsletter.list_subscribers(store) == [OPERATOR]


def test_add_and_remove_a_subscriber():
    store = FakeStore()
    assert "subscribed" in newsletter.add_subscriber(store, ADA, _cfg())
    assert newsletter.list_subscribers(store) == sorted([ADA, OPERATOR])
    assert "already subscribed" in newsletter.add_subscriber(store, ADA, _cfg())

    assert "unsubscribed" in newsletter.remove_subscriber(store, ADA, _cfg())
    assert newsletter.list_subscribers(store) == [OPERATOR]
    # The row stays, so the history of who left and when survives.
    row = [r for r in store.rows(newsletter.SUBSCRIBERS_TAB)
           if r["address"] == ADA][0]
    assert row["status"] == newsletter.STATUS_UNSUBSCRIBED
    assert row["removed_at"]

    # Resubscribing reuses that row rather than leaving two for one person.
    newsletter.add_subscriber(store, ADA, _cfg())
    assert len([r for r in store.rows(newsletter.SUBSCRIBERS_TAB)
                if r["address"] == ADA]) == 1
    assert newsletter.list_subscribers(store) == sorted([ADA, OPERATOR])


def test_removing_someone_who_was_never_on_the_list():
    store = FakeStore()
    assert "was not subscribed" in newsletter.remove_subscriber(
        store, "stranger@example.com", _cfg())


def test_the_list_has_a_ceiling():
    """A cap on the list is a cap on the blast radius of one bad issue."""
    cfg = _cfg(newsletter_max_subscribers=3)
    store = FakeStore(cfg)
    newsletter.add_subscriber(store, "a@example.com", cfg)
    newsletter.add_subscriber(store, "b@example.com", cfg)  # plus the seed = 3
    refusal = newsletter.add_subscriber(store, "c@example.com", cfg)
    assert refusal.startswith("refused:") and "limit" in refusal
    assert len(newsletter.list_subscribers(store, cfg)) == 3


def test_a_bad_address_is_refused():
    store = FakeStore()
    assert newsletter.add_subscriber(store, "not-an-address", _cfg()).startswith(
        "refused:")


# --------------------------------------------------------------------------
# THE TRIGGER
# --------------------------------------------------------------------------

def test_nothing_is_owed_before_the_target_hour(at_morning):
    store = FakeStore()
    owed, why = newsletter.due(_cfg(), store, _people())
    assert not owed
    assert "13:00" in why


def test_an_issue_is_owed_after_the_target_hour(at_afternoon):
    store = FakeStore()
    owed, why = newsletter.due(_cfg(), store, _people())
    assert owed, why


def test_the_target_hour_is_configurable(at_morning):
    # 09:00 UTC is before the default 13 and after a configured 8.
    assert not newsletter.due(_cfg(), FakeStore(), _people())[0]
    assert newsletter.due(_cfg(newsletter_hour_utc=8), FakeStore(),
                          _people())[0]


def test_an_issue_recorded_today_settles_it(at_afternoon):
    store = FakeStore()
    newsletter.record_issue(store, "Issue 1", "body", 1)
    owed, why = newsletter.due(_cfg(), store, _people())
    assert not owed and "already recorded" in why


def test_yesterdays_issue_does_not_count(at_afternoon):
    store = FakeStore()
    newsletter.record_issue(store, "Issue 1", "body", 1,
                            AFTERNOON - datetime.timedelta(days=1))
    assert newsletter.due(_cfg(), store, _people())[0]


def test_an_unreadable_log_means_no_issue(at_afternoon):
    """Not knowing whether today's issue went out is not the same as
    knowing it didn't. The expensive mistake is the duplicate."""
    store = FakeStore(unreadable=[newsletter.ISSUES_TAB])
    owed, why = newsletter.due(_cfg(), store, _people())
    assert not owed and "could not be read" in why
    assert newsletter.issue_rows(store) is None


def test_nothing_is_owed_to_an_empty_list(at_afternoon):
    """Everyone unsubscribed. There is no issue to write."""
    store = FakeStore()
    newsletter.remove_subscriber(store, OPERATOR, _cfg())
    owed, why = newsletter.due(_cfg(), store, _people())
    assert not owed and "nobody is subscribed" in why


# --------------------------------------------------------------------------
# SENDING
# --------------------------------------------------------------------------

def test_an_issue_goes_to_the_whole_list_and_is_recorded(at_afternoon):
    cfg = _cfg()
    store = FakeStore(cfg)
    newsletter.add_subscriber(store, ADA, cfg)
    gmail = FakeGmail()

    outcome = newsletter.send_issue(
        cfg, gmail, store, {"subject": "Tuesday", "body": "what I did today"},
        _people(),
    )
    assert "sent to 2 subscriber(s)" in outcome
    assert sorted(_decode(m)["To"] for m in gmail.sent) == sorted([ADA, OPERATOR])

    rows = store.rows(newsletter.ISSUES_TAB)
    assert len(rows) == 1
    assert rows[0]["subject"] == "Tuesday"
    assert rows[0]["recipient_count"] == "2"
    assert rows[0]["status"] == newsletter.STATUS_SENT
    assert rows[0]["sent_at"] == AFTERNOON.isoformat()
    # Enough of the body to tell this issue from another one.
    assert "what I did today" in rows[0]["excerpt"]
    assert rows[0]["digest"] != newsletter.digest_of("something else")


def test_every_issue_carries_the_unsubscribe_line(at_afternoon):
    cfg = _cfg()
    store = FakeStore(cfg)
    gmail = FakeGmail()
    newsletter.send_issue(cfg, gmail, store,
                          {"subject": "Tuesday", "body": "no sign-off here"},
                          _people())
    body = _decode(gmail.sent[0]).get_payload(decode=True).decode()
    assert "unsubscribe" in body.lower()
    assert newsletter.UNSUBSCRIBE_LINE in body
    # And it is in the stored copy, so the log shows what actually went.
    assert newsletter.UNSUBSCRIBE_LINE in newsletter.compose_body("x")


def test_an_issue_is_not_sent_twice_in_one_day(at_afternoon):
    cfg = _cfg()
    store = FakeStore(cfg)
    gmail = FakeGmail()
    payload = {"subject": "Tuesday", "body": "the day's issue"}

    assert "sent to 1 subscriber(s)" in newsletter.send_issue(
        cfg, gmail, store, payload, _people())
    second = newsletter.send_issue(cfg, gmail, store, payload, _people())

    assert second.startswith("refused:") and "already recorded" in second
    assert len(gmail.sent) == 1
    assert len(store.rows(newsletter.ISSUES_TAB)) == 1


def test_the_issue_is_recorded_before_it_is_sent(at_afternoon, monkeypatch):
    """A run that dies mid-send has already spent the day. That is the
    intended trade: a half-sent issue beats a duplicate one."""
    cfg = _cfg()
    store = FakeStore(cfg)

    class ExplodingGmail(FakeGmail):
        def send(self, userId, body):
            assert store.rows(newsletter.ISSUES_TAB), \
                "the issue row must exist before anything is sent"
            raise RuntimeError("the machine went away")

    outcome = newsletter.send_issue(
        cfg, ExplodingGmail(), store, {"subject": "s", "body": "b"}, _people())
    assert "failed" in outcome
    rows = store.rows(newsletter.ISSUES_TAB)
    assert len(rows) == 1 and rows[0]["status"] == newsletter.STATUS_FAILED
    # And the day is spent: nothing tries again this afternoon.
    assert not newsletter.due(cfg, store, _people())[0]


def test_one_bad_address_does_not_stop_the_rest(at_afternoon):
    cfg = _cfg()
    store = FakeStore(cfg)
    newsletter.add_subscriber(store, ADA, cfg)
    gmail = FakeGmail(fail_for=[ADA])

    outcome = newsletter.send_issue(cfg, gmail, store,
                                    {"subject": "s", "body": "b"}, _people())
    assert "sent to 1 of 2" in outcome
    assert store.rows(newsletter.ISSUES_TAB)[0]["status"] == newsletter.STATUS_PARTIAL
    assert _decode(gmail.sent[0])["To"] == OPERATOR


def test_a_subscriber_who_has_not_accepted_the_terms_is_skipped(at_afternoon):
    """The terms gate is not one of the things the newsletter waives."""
    cfg = _cfg()
    store = FakeStore(cfg)
    newsletter.add_subscriber(store, "unsigned@example.com", cfg)
    gmail = FakeGmail()

    newsletter.send_issue(cfg, gmail, store, {"subject": "s", "body": "b"},
                          _people())
    assert [_decode(m)["To"] for m in gmail.sent] == [OPERATOR]
    # Held, not removed: they can still accept and get tomorrow's.
    assert "unsigned@example.com" in newsletter.list_subscribers(store, cfg)


def test_a_dry_run_sends_and_records_nothing(at_afternoon):
    cfg = _cfg(dry_run=True)
    store = FakeStore(cfg)
    gmail = FakeGmail()
    outcome = newsletter.send_issue(cfg, gmail, store,
                                    {"subject": "s", "body": "b"}, _people())
    assert "dry run" in outcome
    assert gmail.sent == [] and store.rows(newsletter.ISSUES_TAB) == []


def test_an_issue_with_no_body_is_refused(at_afternoon):
    store = FakeStore()
    gmail = FakeGmail()
    assert newsletter.send_issue(_cfg(), gmail, store,
                                 {"subject": "s", "body": "  "},
                                 _people()).startswith("refused:")
    assert gmail.sent == [] and store.rows(newsletter.ISSUES_TAB) == []


# --------------------------------------------------------------------------
# THE HEARTBEAT
# --------------------------------------------------------------------------

def _heartbeat(client, cfg, gmail, store):
    return outbound.run_heartbeat(client, cfg, gmail, store, "SYSTEM",
                                  {"indi@example.com"}, _people())


def test_the_heartbeat_sends_the_issue_when_one_is_owed(at_afternoon):
    cfg = _cfg()
    store = FakeStore(cfg)
    gmail = FakeGmail()
    client = FakeClaude(newsletter.NEWSLETTER_TOOL,
                        {"subject": "Monday", "body": "an issue"})

    outcome = _heartbeat(client, cfg, gmail, store)
    assert "sent to 1 subscriber(s)" in outcome
    assert len(gmail.sent) == 1
    # The run was told, unambiguously, that this was its job — and given
    # nothing else it could do instead.
    assert client.offered_tools == [newsletter.NEWSLETTER_TOOL]
    assert "ONE JOB" in client.calls[-1]["system"]
    assert outbound.NOTHING_TOOL not in client.calls[-1]["system"]


def test_the_heartbeat_is_ordinary_before_the_target_hour(at_morning):
    cfg = _cfg()
    store = FakeStore(cfg)
    client = FakeClaude(outbound.NOTHING_TOOL, {"reason": "quiet"})

    outcome = _heartbeat(client, cfg, FakeGmail(), store)
    assert "nothing to do" in outcome
    assert store.rows(newsletter.ISSUES_TAB) == []
    assert newsletter.NEWSLETTER_TOOL not in client.offered_tools
    assert outbound.NOTHING_TOOL in client.offered_tools


def test_the_heartbeat_is_ordinary_once_the_issue_is_recorded(at_afternoon):
    cfg = _cfg()
    store = FakeStore(cfg)
    newsletter.record_issue(store, "Monday", "an issue", 1)
    client = FakeClaude(outbound.JOURNAL_TOOL, {"entry": "a thought"})

    outcome = _heartbeat(client, cfg, FakeGmail(), store)
    assert outcome == "journal entry written"
    assert newsletter.NEWSLETTER_TOOL not in client.offered_tools
    assert len(store.rows(newsletter.ISSUES_TAB)) == 1


def test_a_heartbeat_that_writes_no_issue_records_nothing(at_afternoon):
    """If the model answers with prose instead of the tool, the day is
    not spent — the next heartbeat tries again."""
    cfg = _cfg()
    store = FakeStore(cfg)
    gmail = FakeGmail()
    client = FakeClaude(text="I would rather not.")

    outcome = _heartbeat(client, cfg, gmail, store)
    assert "not written" in outcome
    assert gmail.sent == [] and store.rows(newsletter.ISSUES_TAB) == []
    assert newsletter.due(cfg, store, _people())[0]


# --------------------------------------------------------------------------
# BUDGETS
# --------------------------------------------------------------------------

def _exhaust_outbound_budget(store, cfg):
    for i in range(cfg.outbound_max_per_day):
        store.append_outbound(ADA, f"note {i}", "body", "why")


def test_the_newsletter_is_exempt_from_the_outbound_budget(at_afternoon):
    """The budget exists to stop Indi deciding on its own to write to
    people. Everyone on the list asked for this one."""
    cfg = _cfg(journal_max_per_day=0)
    store = FakeStore(cfg)
    _exhaust_outbound_budget(store, cfg)
    gmail = FakeGmail()
    client = FakeClaude(newsletter.NEWSLETTER_TOOL,
                        {"subject": "Monday", "body": "an issue"})

    outcome = _heartbeat(client, cfg, gmail, store)
    assert "sent to 1 subscriber(s)" in outcome
    assert len(gmail.sent) == 1
    # And it did not consume the budget it was exempt from: the Outbound
    # tab is untouched, so tomorrow's count starts where it should.
    assert store.outbound_today() == cfg.outbound_max_per_day


def test_ordinary_heartbeat_mail_is_still_not_exempt(at_afternoon):
    """The same run, the same exhausted budget, with today's issue
    already recorded: the ordinary path is refused exactly as before."""
    cfg = _cfg()
    store = FakeStore(cfg)
    newsletter.record_issue(store, "Monday", "an issue", 1)
    _exhaust_outbound_budget(store, cfg)
    gmail = FakeGmail()
    client = FakeClaude(outbound.SEND_TOOL, {
        "to": ADA, "subject": "hello", "body": "hello there",
        "rationale": "no reason at all",
    })

    outcome = _heartbeat(client, cfg, gmail, store)
    assert "refused" in outcome and "budget" in outcome
    assert gmail.sent == []


def test_exhausted_budgets_still_stop_an_ordinary_heartbeat(at_afternoon):
    cfg = _cfg(journal_max_per_day=0)
    store = FakeStore(cfg)
    newsletter.record_issue(store, "Monday", "an issue", 1)
    _exhaust_outbound_budget(store, cfg)
    client = FakeClaude(outbound.JOURNAL_TOOL, {"entry": "a thought"})

    assert "budgets exhausted" in _heartbeat(client, cfg, FakeGmail(), store)
    assert client.calls == []


def test_the_newsletter_ignores_the_per_recipient_cap(at_afternoon):
    """Indi wrote to this subscriber today on its own initiative. The
    issue they subscribed to still goes."""
    cfg = _cfg(outbound_max_per_recipient_per_day=1)
    store = FakeStore(cfg)
    store.append_outbound(OPERATOR, "a note", "body", "why")
    assert store.outbound_today_to(OPERATOR) >= cfg.outbound_max_per_recipient_per_day

    gmail = FakeGmail()
    newsletter.send_issue(cfg, gmail, store, {"subject": "s", "body": "b"},
                          _people())
    assert [_decode(m)["To"] for m in gmail.sent] == [OPERATOR]


# --------------------------------------------------------------------------
# UNSUBSCRIBING
# --------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    "unsubscribe",
    "Unsubscribe.",
    "UNSUBSCRIBE",
    "please unsubscribe me",
    "Please unsubscribe me from the newsletter.",
    "unsubscribe\n\n--\nSent from my phone",
])
def test_these_read_as_unsubscribe_requests(body):
    assert newsletter.looks_like_unsubscribe(_msg(body=body))


@pytest.mark.parametrize("body", [
    "",
    "thanks for today's issue",
    "I was going to unsubscribe but actually I read the whole thing and "
    "wanted to say that the bit about the queue was interesting — keep going.",
    "how do I unsubscribe a different mailing list? there's no link in it "
    "anywhere and the sender won't answer me, which is infuriating.",
])
def test_these_do_not(body):
    assert not newsletter.looks_like_unsubscribe(_msg(body=body))


def test_unsubscribing_removes_the_address_and_is_acknowledged():
    cfg = _cfg()
    store = FakeStore(cfg)
    gmail = FakeGmail()
    assert OPERATOR in newsletter.list_subscribers(store, cfg)

    outcome = newsletter.check_unsubscribe(cfg, gmail, store, _msg())
    assert outcome and "unsubscribed" in outcome
    assert newsletter.list_subscribers(store, cfg) == []

    assert len(gmail.sent) == 1
    reply = _decode(gmail.sent[0])
    assert reply["To"] == OPERATOR
    assert "removed" in reply.get_payload(decode=True).decode()


def test_a_second_unsubscribe_is_not_treated_as_one():
    """Once they are off the list there is nothing to act on, and the
    message goes to the ordinary reply path like any other."""
    cfg = _cfg()
    store = FakeStore(cfg)
    newsletter.check_unsubscribe(cfg, FakeGmail(), store, _msg())
    assert newsletter.check_unsubscribe(cfg, FakeGmail(), store, _msg()) is None


def test_a_stranger_saying_unsubscribe_is_left_to_the_reply_path():
    """They were never on the list. Answering as though they had been
    would also tell them who is."""
    cfg = _cfg()
    store = FakeStore(cfg)
    gmail = FakeGmail()
    assert newsletter.check_unsubscribe(
        cfg, gmail, store, _msg(sender="stranger@example.com")) is None
    assert gmail.sent == []


def test_an_unsubscribed_address_gets_no_further_issues(at_afternoon):
    cfg = _cfg()
    store = FakeStore(cfg)
    newsletter.add_subscriber(store, ADA, cfg)
    newsletter.check_unsubscribe(cfg, FakeGmail(), store,
                                 _msg(sender=ADA, body="unsubscribe"))

    gmail = FakeGmail()
    newsletter.send_issue(cfg, gmail, store, {"subject": "s", "body": "b"},
                          _people())
    assert [_decode(m)["To"] for m in gmail.sent] == [OPERATOR]


def test_a_dry_run_does_not_unsubscribe_anyone():
    cfg = _cfg(dry_run=True)
    store = FakeStore(cfg)
    gmail = FakeGmail()
    assert "would unsubscribe" in newsletter.check_unsubscribe(
        cfg, gmail, store, _msg())
    assert newsletter.list_subscribers(store, cfg) == [OPERATOR]
    assert gmail.sent == []


def test_the_reply_path_honours_an_unsubscribe_without_asking_a_model():
    """The wiring, end to end through process_message: the message is
    acted on, acknowledged and marked read, and the reply model is never
    reached."""
    import check_and_reply

    cfg = _cfg(max_quoted_chars=6000, max_replies_per_sender_per_day=10,
               allowed_senders=(), max_thread_messages=0, max_thread_chars=0)
    cfg.is_allowed = lambda addr: True
    store = FakeStore(cfg)
    gmail = FakeGmail()

    class NoModel:
        messages = None

        def __getattr__(self, name):
            raise AssertionError("the reply model was called")

    raw = {
        "id": "m1", "threadId": "t1", "internalDate": "0",
        "payload": {
            "headers": [
                {"name": "From", "value": OPERATOR},
                {"name": "Subject", "value": "newsletter"},
                {"name": "Message-ID", "value": "<m1@example.com>"},
            ],
            "mimeType": "text/plain",
            "body": {"data": base64.urlsafe_b64encode(b"unsubscribe").decode()},
        },
    }
    people = correspondents.CorrespondentStore()
    try:
        outcome = check_and_reply.process_message(
            gmail, None, NoModel(), cfg, store, raw, {"indi@example.com"},
            "SYSTEM", people,
        )
    finally:
        people.close()

    assert outcome.startswith("newsletter:") and "unsubscribed" in outcome
    assert newsletter.list_subscribers(store, cfg) == []
    assert gmail.read == ["m1"]
    assert len(gmail.sent) == 1
    # And the message is recorded as handled, so it is not reconsidered.
    queue = store.rows(sheets_queue.QUEUE_TAB)
    assert len(queue) == 1 and queue[0]["status"] == sheets_queue.STATUS_SKIPPED


# --------------------------------------------------------------------------
# RESEARCH: WEB SEARCH AND FETCH IN THE ISSUE RUN
# --------------------------------------------------------------------------

class _BetaClaude(FakeClaude):
    """Records calls made through client.beta.messages too."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.beta = type("Beta", (), {"messages": self})()


def _research_cfg(**over):
    base = dict(enable_web_search=True, newsletter_web_search=True,
                newsletter_max_searches=5, newsletter_web_fetch=False,
                newsletter_max_fetches=3, web_fetch_allowed_domains=(),
                web_search_tool_type="web_search_20250305",
                web_fetch_tool_type="web_fetch_20250910")
    base.update(over)
    cfg = _cfg(**base)
    cfg.betas = lambda want_web_fetch, want_code_execution, want_files: (
        ["web-fetch-2025-09-10"] if want_web_fetch else [])
    return cfg


def _issue(client, cfg):
    store = FakeStore()
    return newsletter.run_issue(client, cfg, FakeGmail(), store, "SYS", "m",
                                context="CTX")


def test_issue_run_offers_search_with_its_own_cap():
    client = _BetaClaude(newsletter.NEWSLETTER_TOOL,
                         {"subject": "s", "body": "b"})
    _issue(client, _research_cfg())
    tools = client.calls[-1]["tools"]
    search = [t for t in tools if t["name"] == "web_search"]
    assert len(search) == 1 and search[0]["max_uses"] == 5
    assert "web_fetch" not in client.offered_tools
    system = client.calls[-1]["system"]
    assert "Sources:" in system and "Never put anything from your correspondence" in system
    assert "betas" not in client.calls[-1]


def test_no_search_when_either_switch_is_off():
    for over in ({"newsletter_web_search": False}, {"enable_web_search": False},
                 {"newsletter_max_searches": 0}):
        client = _BetaClaude(newsletter.NEWSLETTER_TOOL,
                             {"subject": "s", "body": "b"})
        _issue(client, _research_cfg(**over))
        assert client.offered_tools == [newsletter.NEWSLETTER_TOOL]
        assert "RESEARCH FOR THIS ISSUE" not in client.calls[-1]["system"]


def test_old_configs_without_the_settings_get_no_search():
    client = FakeClaude(newsletter.NEWSLETTER_TOOL, {"subject": "s", "body": "b"})
    _issue(client, _cfg())
    assert client.offered_tools == [newsletter.NEWSLETTER_TOOL]


def test_fetch_rides_on_search_and_sends_the_beta():
    client = _BetaClaude(newsletter.NEWSLETTER_TOOL,
                         {"subject": "s", "body": "b"})
    _issue(client, _research_cfg(newsletter_web_fetch=True,
                                 web_fetch_allowed_domains=("example.org",)))
    fetch = [t for t in client.calls[-1]["tools"] if t["name"] == "web_fetch"]
    assert fetch and fetch[0]["max_uses"] == 3
    assert fetch[0]["allowed_domains"] == ["example.org"]
    assert client.calls[-1]["betas"] == ["web-fetch-2025-09-10"]

    # Fetch alone, with search off, offers nothing.
    client = _BetaClaude(newsletter.NEWSLETTER_TOOL,
                         {"subject": "s", "body": "b"})
    _issue(client, _research_cfg(newsletter_web_search=False,
                                 newsletter_web_fetch=True))
    assert client.offered_tools == [newsletter.NEWSLETTER_TOOL]


def test_a_paused_turn_is_resumed_and_searches_are_costed(monkeypatch):
    recorded = []
    monkeypatch.setattr(newsletter.costs, "record",
                        lambda kind, model, tokens, web_searches=0, note="":
                        recorded.append(web_searches))
    search_block = type("B", (), {"type": "server_tool_use", "name": "web_search",
                                  "input": {"query": "q"}})()
    send_block = type("B", (), {"type": "tool_use", "name": newsletter.NEWSLETTER_TOOL,
                                "input": {"subject": "s", "body": "b"}})()
    responses = [
        type("R", (), {"content": [search_block], "usage": None,
                       "stop_reason": "pause_turn"})(),
        type("R", (), {"content": [search_block, send_block], "usage": None,
                       "stop_reason": "tool_use"})(),
    ]
    client = _BetaClaude()

    def stream(**kwargs):
        client.calls.append(kwargs)
        response = responses[len(client.calls) - 1]

        class _S:
            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False

            def get_final_message(self):
                return response
        return _S()
    client.stream = stream
    gmail = FakeGmail()
    outcome = newsletter.run_issue(client, _research_cfg(), gmail, FakeStore(),
                                   "SYS", "m", context="CTX")
    assert len(client.calls) == 2
    # The resumed call carries the paused turn.
    assert client.calls[1]["messages"][-1]["role"] == "assistant"
    assert recorded == [1, 1]
    assert " sent to " in outcome and gmail.sent
