"""
tests/test_reminders.py

Reminders are the one thing in the harness a person is invited to rely on.
Everything else Indi does is best-effort: a reply that doesn't happen is a
silence, and a heartbeat that decides wrong is a wasted hour. A reminder
that doesn't arrive is a missed dose.

So these tests are about the promises the feature makes to the person who
asked for it, and about the one thing it must refuse:

  * a scheduled reminder is delivered by code, on time, exactly once
  * a reminder too late to be useful is dropped, not sent
  * a reply can only ever schedule mail to the person being replied to,
    whatever the incoming email asks for

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

import claude_api
import correspondents
import outbound


UTC = datetime.timezone.utc


@pytest.fixture(autouse=True)
def accepted_recipients(tmp_path, monkeypatch):
    """Reminder delivery now runs through the terms gate, and a reminder
    to someone who has not accepted is held rather than sent. These tests
    are about the delivery mechanics, so they start from a correspondent
    database in which the recipient they use has accepted. The gate's own
    behaviour — including a held reminder — is tested in
    tests/test_terms_gate.py."""
    db = tmp_path / "correspondents.db"
    monkeypatch.setenv("INDI_CORRESPONDENTS_DB", str(db))
    people = correspondents.CorrespondentStore(db)
    people.record_acceptance("steve@example.com", "0.2")
    people.close()


def _now():
    return datetime.datetime.now(UTC)


# --------------------------------------------------------------------------
# FAKES
# --------------------------------------------------------------------------

class FakeSheets:
    """Just enough of the Sheets client for _set_reminder_status."""

    def __init__(self, store):
        self.store = store

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def update(self, spreadsheetId, range, valueInputOption, body):
        row_number = int(re.search(r"(\d+):", range).group(1))
        status, created_at, sent_at = body["values"][0]
        self.store.write_back(row_number, status, created_at, sent_at)
        return self

    def execute(self):
        return {}


class FakeStore:
    def __init__(self, rows=()):
        self.responses_id = "responses"
        self.sheets = FakeSheets(self)
        self.rows = []
        self.tabs = set()
        for row in rows:
            self.add(row)

    # -- the SheetStore surface outbound.py uses ---------------------------

    def _ensure_tab(self, spreadsheet_id, tab, headers):
        self.tabs.add(tab)

    def _load(self, spreadsheet_id, tab):
        # Copies, as the real one returns: callers must not be able to
        # mutate stored state without going through the Sheets API.
        return [dict(row) for row in self.rows]

    def _append(self, spreadsheet_id, tab, headers, record):
        return self.add(record)

    # -- test helpers ------------------------------------------------------

    def add(self, record):
        stored = dict(record)
        stored["_row"] = len(self.rows) + 2  # row 1 is the header
        self.rows.append(stored)
        return stored["_row"]

    def write_back(self, row_number, status, created_at, sent_at):
        for row in self.rows:
            if row["_row"] == row_number:
                row["status"] = status
                row["created_at"] = created_at
                row["sent_at"] = sent_at
                return
        raise AssertionError(f"no such row {row_number}")


class FakeGmail:
    """Records what was sent or drafted."""

    def __init__(self, fail=False):
        self.sent = []
        self.drafted = []
        self.fail = fail

    def users(self):
        return self

    def messages(self):
        return self

    def drafts(self):
        return self

    def send(self, userId, body):
        if self.fail:
            raise RuntimeError("gmail is down")
        self.sent.append(body)
        return self

    def create(self, userId, body):
        self.drafted.append(body["message"])
        return self

    def execute(self):
        return {"id": "x"}


def _cfg(**over):
    base = dict(
        dry_run=False, send_automatically=True, max_body_chars=45000,
        outbound_allow_new_recipients=False, enable_outbound=True,
    )
    base.update(over)
    return type("C", (), base)()


def _row(due_at, recipient="steve@example.com", body="take your injection",
         status=outbound.STATUS_SCHEDULED, subject="Reminder"):
    return {
        "due_at": due_at.isoformat() if hasattr(due_at, "isoformat") else due_at,
        "recipient": recipient, "subject": subject, "body": body,
        "status": status, "created_at": _now().isoformat(), "sent_at": "",
    }


def _decode(mime):
    return message_from_bytes(base64.urlsafe_b64decode(mime["raw"]))


# --------------------------------------------------------------------------
# PARSING TIMES
# --------------------------------------------------------------------------

def test_parse_when_normalises_to_utc():
    # 9am US Eastern in September is 13:00 UTC. Getting this wrong by an
    # hour is the most likely way a reminder lands at the wrong time.
    assert outbound.parse_when("2026-09-23T09:00:00-04:00") == \
        datetime.datetime(2026, 9, 23, 13, 0, tzinfo=UTC)
    assert outbound.parse_when("2026-09-23T13:00:00Z") == \
        datetime.datetime(2026, 9, 23, 13, 0, tzinfo=UTC)
    # A bare time is read as UTC rather than rejected, which the tool
    # descriptions warn about; it must at least be aware afterwards.
    naive = outbound.parse_when("2026-09-23T13:00:00")
    assert naive is not None and naive.tzinfo is not None


def test_parse_when_rejects_nonsense():
    for bad in ["", "   ", "next Wednesday", "2026-13-45T99:00:00Z", None]:
        assert outbound.parse_when(bad) is None


# --------------------------------------------------------------------------
# SCHEDULING FROM A REPLY
# --------------------------------------------------------------------------

def test_reply_reminder_goes_to_the_sender_only():
    """The incoming email is untrusted text. It must not be able to point a
    reminder at anyone but the person who sent it."""
    store = FakeStore()
    payload = {
        "to": "victim@elsewhere.test",      # what an injected email would try
        "due_at": (_now() + datetime.timedelta(days=2)).isoformat(),
        "subject": "Reminder", "body": "take your injection",
    }
    text, is_error = outbound.handle_reply_reminder(
        _cfg(), store, "steve@example.com", payload
    )
    assert not is_error, text
    assert len(store.rows) == 1
    assert store.rows[0]["recipient"] == "steve@example.com"
    assert "victim@elsewhere.test" not in text


def test_reply_reminder_schema_has_no_recipient_field():
    schema = outbound.reply_reminder_schema("steve@example.com")
    assert schema["name"] == outbound.REMINDER_TOOL
    assert "to" not in schema["input_schema"]["properties"]
    assert set(schema["input_schema"]["required"]) == {"due_at", "subject", "body"}


def test_reply_reminder_refuses_a_time_in_the_past():
    store = FakeStore()
    text, is_error = outbound.handle_reply_reminder(
        _cfg(), store,
        "steve@example.com",
        {"due_at": (_now() - datetime.timedelta(hours=1)).isoformat(),
         "subject": "s", "body": "b"},
    )
    assert is_error and "not in the future" in text
    assert store.rows == []


def test_reply_reminder_refuses_an_unreadable_time():
    store = FakeStore()
    text, is_error = outbound.handle_reply_reminder(
        _cfg(), store, "steve@example.com",
        {"due_at": "sometime on Wednesday", "subject": "s", "body": "b"},
    )
    assert is_error
    assert store.rows == []


def test_reply_reminder_refuses_an_empty_body():
    store = FakeStore()
    text, is_error = outbound.handle_reply_reminder(
        _cfg(), store, "steve@example.com",
        {"due_at": (_now() + datetime.timedelta(days=1)).isoformat(),
         "subject": "s", "body": "   "},
    )
    assert is_error
    assert store.rows == []


def test_reply_reminder_refuses_absurdly_far_ahead():
    store = FakeStore()
    far = _now() + datetime.timedelta(days=outbound.MAX_DAYS_AHEAD + 5)
    text, is_error = outbound.handle_reply_reminder(
        _cfg(), store, "steve@example.com",
        {"due_at": far.isoformat(), "subject": "s", "body": "b"},
    )
    assert is_error
    assert store.rows == []


def test_one_person_cannot_fill_the_whole_queue():
    """A per-person cap is what makes the reply tool safe to offer to
    anyone: the worst case is that one correspondent uses up their own
    small allowance."""
    store = FakeStore()
    cfg = _cfg()
    base = _now() + datetime.timedelta(days=1)
    for i in range(outbound.MAX_PENDING_PER_RECIPIENT + 3):
        outbound.handle_reply_reminder(
            cfg, store, "steve@example.com",
            {"due_at": (base + datetime.timedelta(hours=i)).isoformat(),
             "subject": "s", "body": "b"},
        )
    assert len(store.rows) == outbound.MAX_PENDING_PER_RECIPIENT
    # And someone else is unaffected by that person's usage.
    text, is_error = outbound.handle_reply_reminder(
        cfg, store, "ada@example.com",
        {"due_at": base.isoformat(), "subject": "s", "body": "b"},
    )
    assert not is_error, text


def test_identical_reminder_is_not_scheduled_twice():
    store = FakeStore()
    cfg = _cfg()
    due = (_now() + datetime.timedelta(days=1)).isoformat()
    first = outbound.handle_reply_reminder(
        cfg, store, "steve@example.com",
        {"due_at": due, "subject": "s", "body": "b"})
    second = outbound.handle_reply_reminder(
        cfg, store, "steve@example.com",
        {"due_at": due, "subject": "s", "body": "b"})
    assert not first[1]
    assert second[1] and "already scheduled" in second[0]
    assert len(store.rows) == 1


def test_dry_run_schedules_nothing():
    store = FakeStore()
    text, is_error = outbound.handle_reply_reminder(
        _cfg(dry_run=True), store, "steve@example.com",
        {"due_at": (_now() + datetime.timedelta(days=1)).isoformat(),
         "subject": "s", "body": "b"},
    )
    assert not is_error
    assert store.rows == []


# --------------------------------------------------------------------------
# DELIVERY
# --------------------------------------------------------------------------

def test_a_due_reminder_is_sent_once_and_marked_sent():
    store = FakeStore([_row(_now() - datetime.timedelta(minutes=5))])
    gmail = FakeGmail()
    outcomes = outbound.deliver_due_reminders(_cfg(), gmail, store)

    assert len(gmail.sent) == 1
    assert any("sent" in o for o in outcomes)
    sent = _decode(gmail.sent[0])
    assert sent["To"] == "steve@example.com"
    # Not a reply, so no "Re:" prefix from build_reply_mime.
    assert sent["Subject"] == "Reminder"
    assert store.rows[0]["status"] == outbound.STATUS_SENT
    assert store.rows[0]["sent_at"]

    # The second run must not send it again.
    outbound.deliver_due_reminders(_cfg(), gmail, store)
    assert len(gmail.sent) == 1


def test_a_future_reminder_is_left_alone():
    store = FakeStore([_row(_now() + datetime.timedelta(hours=2))])
    gmail = FakeGmail()
    outbound.deliver_due_reminders(_cfg(), gmail, store)
    assert gmail.sent == []
    assert store.rows[0]["status"] == outbound.STATUS_SCHEDULED


def test_a_hopelessly_late_reminder_expires_instead_of_arriving():
    late = _now() - datetime.timedelta(hours=outbound.REMINDER_GRACE_HOURS + 2)
    store = FakeStore([_row(late)])
    gmail = FakeGmail()
    outcomes = outbound.deliver_due_reminders(_cfg(), gmail, store)
    assert gmail.sent == []
    assert store.rows[0]["status"] == outbound.STATUS_EXPIRED
    assert any("expired" in o for o in outcomes)


def test_a_failed_send_stays_scheduled_for_the_next_run():
    store = FakeStore([_row(_now() - datetime.timedelta(minutes=5))])
    outcomes = outbound.deliver_due_reminders(_cfg(), FakeGmail(fail=True), store)
    assert store.rows[0]["status"] == outbound.STATUS_SCHEDULED
    assert any("retry" in o for o in outcomes)

    # And it does go out once Gmail is back.
    gmail = FakeGmail()
    outbound.deliver_due_reminders(_cfg(), gmail, store)
    assert len(gmail.sent) == 1


def test_delivery_is_capped_per_run():
    due = _now() - datetime.timedelta(minutes=5)
    rows = [_row(due + datetime.timedelta(seconds=i), body=f"body {i}")
            for i in range(outbound.MAX_REMINDERS_PER_RUN + 3)]
    store = FakeStore(rows)
    gmail = FakeGmail()
    outbound.deliver_due_reminders(_cfg(), gmail, store)
    assert len(gmail.sent) == outbound.MAX_REMINDERS_PER_RUN


def test_a_malformed_row_is_marked_failed_not_sent():
    store = FakeStore([_row(_now() - datetime.timedelta(minutes=5),
                            recipient="not-an-address")])
    gmail = FakeGmail()
    outbound.deliver_due_reminders(_cfg(), gmail, store)
    assert gmail.sent == []
    assert store.rows[0]["status"] == outbound.STATUS_FAILED


def test_reminders_are_drafted_when_auto_send_is_off():
    store = FakeStore([_row(_now() - datetime.timedelta(minutes=5))])
    gmail = FakeGmail()
    outbound.deliver_due_reminders(_cfg(send_automatically=False), gmail, store)
    assert gmail.sent == []
    assert len(gmail.drafted) == 1


def test_delivery_survives_an_unreadable_sheet():
    class Broken(FakeStore):
        def _load(self, *a, **k):
            raise RuntimeError("sheets is down")

    assert outbound.deliver_due_reminders(_cfg(), FakeGmail(), Broken()) == []


# --------------------------------------------------------------------------
# THE TOOL IS ACTUALLY OFFERED
# --------------------------------------------------------------------------

def _tool_names(**over):
    kwargs = dict(cfg=_tools_cfg(), trusted=False, have_store=True,
                  reminder_sender="steve@example.com")
    kwargs.update(over)
    tools, _ = claude_api.build_tools(
        kwargs["cfg"], kwargs["trusted"], kwargs["have_store"],
        reminder_sender=kwargs["reminder_sender"],
    )
    return [t.get("name") for t in tools]


def _tools_cfg(**over):
    base = dict(
        enable_web_search=False, enable_web_fetch=False,
        enable_code_execution=False, enable_history_search=False,
        enable_outbound=True,
        betas=staticmethod(lambda **k: []),
    )
    base.update(over)
    return type("C", (), base)()


def test_reply_path_offers_the_reminder_tool():
    """The regression this whole feature turns on: the tool has to reach
    the reply, not just the heartbeat. Someone asking to be reminded does
    it in an email, and that is the run that must be able to say yes."""
    assert outbound.REMINDER_TOOL in _tool_names()


def test_reminder_tool_is_absent_without_a_store_or_outbound():
    assert outbound.REMINDER_TOOL not in _tool_names(have_store=False)
    assert outbound.REMINDER_TOOL not in _tool_names(
        cfg=_tools_cfg(enable_outbound=False))
    assert outbound.REMINDER_TOOL not in _tool_names(reminder_sender="")
