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

Repeats add three more, and they are the ones worth being careful about,
because a repeating reminder is an automated mail sender:

  * the date arithmetic is right, including short months and leap years
  * a series that was missed resumes rather than either ending silently
    or delivering its backlog in a burst
  * a correspondent can stop their own reminders and cannot touch
    anyone else's — the same scoping rule as scheduling, tested the same
    way, by trying to reach past it

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


# --------------------------------------------------------------------------
# RECURRENCE ARITHMETIC
#
# Hand-written date maths, so it is tested at the level it is written at.
# The two things that go wrong in every implementation of this are short
# months and leap years, and the third — the one you only notice months
# later — is a series that clamps once and then never comes back.
# --------------------------------------------------------------------------

def test_fixed_intervals_are_plain_addition():
    anchor = datetime.datetime(2026, 9, 21, 8, 0, tzinfo=UTC)
    assert outbound._occurrence(anchor, 3, "hours", 2) == \
        datetime.datetime(2026, 9, 21, 14, 0, tzinfo=UTC)
    assert outbound._occurrence(anchor, 1, "days", 10) == \
        datetime.datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    assert outbound._occurrence(anchor, 2, "weeks", 3) == \
        datetime.datetime(2026, 11, 2, 8, 0, tzinfo=UTC)


def test_month_arithmetic_clamps_the_day():
    jan31 = datetime.datetime(2026, 1, 31, 9, 0, tzinfo=UTC)
    # 2026 is not a leap year, so the 31st of January plus a month is the
    # 28th of February — there is nowhere else for it to go.
    assert outbound._occurrence(jan31, 1, "months", 1) == \
        datetime.datetime(2026, 2, 28, 9, 0, tzinfo=UTC)
    assert outbound._occurrence(jan31, 1, "months", 3) == \
        datetime.datetime(2026, 4, 30, 9, 0, tzinfo=UTC)


def test_a_clamped_month_does_not_drag_the_series_down_with_it():
    """The bug this design exists to avoid: stepping from the previous
    occurrence would put February's 28th into March and leave a monthly
    reminder stuck on the 28th forever. Counting from the anchor gets
    the 31st back."""
    jan31 = datetime.datetime(2026, 1, 31, 9, 0, tzinfo=UTC)
    march = outbound._occurrence(jan31, 1, "months", 2)
    assert march == datetime.datetime(2026, 3, 31, 9, 0, tzinfo=UTC)


def test_leap_years():
    jan31 = datetime.datetime(2024, 1, 31, 9, 0, tzinfo=UTC)
    assert outbound._occurrence(jan31, 1, "months", 1) == \
        datetime.datetime(2024, 2, 29, 9, 0, tzinfo=UTC)
    # And a yearly reminder booked on the 29th of February clamps in the
    # three years that don't have one, then comes back.
    feb29 = datetime.datetime(2024, 2, 29, 9, 0, tzinfo=UTC)
    assert outbound._occurrence(feb29, 1, "years", 1) == \
        datetime.datetime(2025, 2, 28, 9, 0, tzinfo=UTC)
    assert outbound._occurrence(feb29, 1, "years", 4) == \
        datetime.datetime(2028, 2, 29, 9, 0, tzinfo=UTC)


def test_next_occurrence_skips_everything_already_past():
    """Catch-up: three days off the air must not queue three messages."""
    anchor = datetime.datetime(2026, 9, 1, 7, 0, tzinfo=UTC)
    after = datetime.datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    assert outbound.next_occurrence(anchor, 1, "days", after) == \
        datetime.datetime(2026, 9, 21, 7, 0, tzinfo=UTC)
    # Monthly, a year late, lands on the next one and not on any of the
    # twelve in between.
    assert outbound.next_occurrence(anchor, 1, "months", after) == \
        datetime.datetime(2026, 10, 1, 7, 0, tzinfo=UTC)


def test_next_occurrence_is_strictly_after_its_reference():
    anchor = datetime.datetime(2026, 9, 21, 8, 0, tzinfo=UTC)
    exactly = datetime.datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
    assert outbound.next_occurrence(anchor, 1, "days", exactly) == \
        datetime.datetime(2026, 9, 23, 8, 0, tzinfo=UTC)


def test_next_occurrence_refuses_nonsense_units_and_intervals():
    anchor = datetime.datetime(2026, 9, 21, 8, 0, tzinfo=UTC)
    assert outbound.next_occurrence(anchor, 1, "fortnights", anchor) is None
    assert outbound.next_occurrence(anchor, 0, "days", anchor) is None


# --------------------------------------------------------------------------
# SCHEDULING A REPEAT
# --------------------------------------------------------------------------

def _repeat_row(due_at, every=1, unit="days", count_left=3, until="",
                anchor=None, recipient="steve@example.com", series="s-test",
                reminder_id="r-test", subject="Reminder", body="take your injection"):
    row = _row(due_at, recipient=recipient, body=body, subject=subject)
    row.update({
        "reminder_id": reminder_id,
        "series_id": series,
        "repeat_every": str(every),
        "repeat_unit": unit,
        "repeat_count_left": str(count_left),
        "repeat_until": until,
        "repeat_anchor": (anchor or due_at).isoformat()
        if hasattr(anchor or due_at, "isoformat") else (anchor or due_at),
    })
    return row


def _schedule_repeat(store, cfg=None, **over):
    payload = {
        "due_at": (_now() + datetime.timedelta(days=1)).isoformat(),
        "subject": "s", "body": "b",
        "repeat_every": 3, "repeat_unit": "days",
    }
    payload.update(over)
    return outbound.handle_reply_reminder(
        cfg or _cfg(), store, "steve@example.com", payload)


def test_a_repeat_is_stored_on_the_row():
    store = FakeStore()
    text, is_error = _schedule_repeat(store)
    assert not is_error, text
    row = store.rows[0]
    assert row["repeat_every"] == "3"
    assert row["repeat_unit"] == "days"
    # The row booked now is the first of the series, so what is left is
    # one fewer than the ceiling.
    assert row["repeat_count_left"] == str(outbound.MAX_OCCURRENCES_PER_SERIES - 1)
    assert row["repeat_anchor"] == row["due_at"]
    assert row["reminder_id"] and row["series_id"]
    assert "every 3 days" in text


def test_a_repeat_unit_is_accepted_in_the_singular():
    store = FakeStore()
    text, is_error = _schedule_repeat(store, repeat_every=1, repeat_unit="month")
    assert not is_error, text
    assert store.rows[0]["repeat_unit"] == "months"


def test_an_uncapped_series_still_gets_a_cap():
    store = FakeStore()
    _schedule_repeat(store, repeat_every=1, repeat_unit="days")
    total = int(store.rows[0]["repeat_count_left"]) + 1
    assert total == outbound.MAX_OCCURRENCES_PER_SERIES


def test_repeat_count_is_honoured():
    store = FakeStore()
    _schedule_repeat(store, repeat_count=4)
    assert store.rows[0]["repeat_count_left"] == "3"


def test_repeat_until_is_stored():
    store = FakeStore()
    until = _now() + datetime.timedelta(days=30)
    _schedule_repeat(store, repeat_until=until.isoformat())
    assert store.rows[0]["repeat_until"] == until.isoformat()


# -- the refusals ----------------------------------------------------------

@pytest.mark.parametrize("over, fragment", [
    ({"repeat_every": 3, "repeat_unit": ""}, "repeat_unit"),
    ({"repeat_every": "", "repeat_unit": "days"}, "repeat_every"),
    ({"repeat_every": 3, "repeat_unit": "fortnights"}, "repeat_unit"),
    ({"repeat_every": 0, "repeat_unit": "days"}, "at least 1"),
    ({"repeat_every": -2, "repeat_unit": "hours"}, "at least 1"),
    ({"repeat_every": "often", "repeat_unit": "days"}, "repeat_every"),
    ({"repeat_count": 1}, "at least 2"),
    ({"repeat_count": outbound.MAX_OCCURRENCES_PER_SERIES + 1}, "more than the"),
    ({"repeat_until": "next spring"}, "could not read"),
    ({"repeat_until": "1999-01-01T00:00:00+00:00"}, "not in the future"),
])
def test_a_bad_repeat_is_refused_out_loud(over, fragment):
    """Every one of these has to come back as a refusal the model can
    repeat to a person, not as a reminder silently downgraded to a
    single message."""
    store = FakeStore()
    text, is_error = _schedule_repeat(store, **over)
    assert is_error, text
    assert text.startswith("refused:") and fragment in text
    assert store.rows == []


def test_a_repeat_cannot_run_past_the_horizon():
    store = FakeStore()
    far = _now() + datetime.timedelta(days=outbound.MAX_DAYS_AHEAD + 10)
    text, is_error = _schedule_repeat(store, repeat_until=far.isoformat())
    assert is_error and str(outbound.MAX_DAYS_AHEAD) in text
    assert store.rows == []


def test_a_repeat_ending_before_it_starts_is_refused():
    store = FakeStore()
    due = _now() + datetime.timedelta(days=10)
    text, is_error = _schedule_repeat(
        store, due_at=due.isoformat(),
        repeat_until=(_now() + datetime.timedelta(days=2)).isoformat())
    assert is_error and "before the first reminder" in text
    assert store.rows == []


def test_an_ordinary_reminder_is_still_a_one_shot():
    store = FakeStore()
    outbound.handle_reply_reminder(
        _cfg(), store, "steve@example.com",
        {"due_at": (_now() + datetime.timedelta(days=1)).isoformat(),
         "subject": "s", "body": "b"})
    assert store.rows[0]["repeat_every"] == ""
    assert outbound.describe_repeat(store.rows[0]) == "one-off"


# --------------------------------------------------------------------------
# DELIVERING A SERIES
# --------------------------------------------------------------------------

def test_a_sent_occurrence_books_the_next_one():
    due = _now() - datetime.timedelta(minutes=5)
    store = FakeStore([_repeat_row(due, every=1, unit="days", count_left=3)])
    gmail = FakeGmail()
    outbound.deliver_due_reminders(_cfg(), gmail, store)

    assert len(gmail.sent) == 1
    assert store.rows[0]["status"] == outbound.STATUS_SENT
    assert len(store.rows) == 2
    nxt = store.rows[1]
    assert nxt["status"] == outbound.STATUS_SCHEDULED
    assert nxt["series_id"] == "s-test"
    assert nxt["repeat_count_left"] == "2"
    assert nxt["repeat_anchor"] == store.rows[0]["repeat_anchor"]
    assert outbound.parse_when(nxt["due_at"]) == \
        outbound.parse_when(due.isoformat()) + datetime.timedelta(days=1)
    # And the freshly booked row is not itself due, so a second run in
    # the same minute sends nothing more.
    outbound.deliver_due_reminders(_cfg(), gmail, store)
    assert len(gmail.sent) == 1


def test_the_last_occurrence_ends_the_series():
    store = FakeStore([_repeat_row(_now() - datetime.timedelta(minutes=5),
                                   count_left=0)])
    gmail = FakeGmail()
    outcomes = outbound.deliver_due_reminders(_cfg(), gmail, store)
    assert len(gmail.sent) == 1
    assert len(store.rows) == 1
    assert any("finished its run" in o for o in outcomes)


def test_a_series_past_its_end_date_is_not_rebooked():
    due = _now() - datetime.timedelta(minutes=5)
    store = FakeStore([_repeat_row(
        due, every=1, unit="days", count_left=5,
        until=(_now() + datetime.timedelta(hours=2)).isoformat())])
    gmail = FakeGmail()
    outcomes = outbound.deliver_due_reminders(_cfg(), gmail, store)
    assert len(gmail.sent) == 1
    assert len(store.rows) == 1
    assert any("end date" in o for o in outcomes)


def test_missed_occurrences_are_skipped_not_sent_in_a_burst():
    """The machine was off for three days. A daily series owes the person
    one message at the next occurrence, not three at once — and the one
    it missed this morning is too old to be worth sending."""
    anchor = _now() - datetime.timedelta(days=3, hours=2)
    due = anchor + datetime.timedelta(days=3)  # this morning's, hours late
    store = FakeStore([_repeat_row(due, every=1, unit="days", count_left=10,
                                   anchor=anchor)])
    gmail = FakeGmail()
    outbound.deliver_due_reminders(_cfg(), gmail, store)

    # Within the grace window, so today's does go out — exactly one.
    assert len(gmail.sent) == 1
    assert len(store.rows) == 2
    nxt = outbound.parse_when(store.rows[1]["due_at"])
    assert nxt > _now()
    assert nxt - _now() < datetime.timedelta(days=1)


def test_an_expired_occurrence_still_rolls_the_series_forward():
    """The important half of catch-up: two days off the air must not
    silently end a daily series."""
    anchor = _now() - datetime.timedelta(days=4)
    due = anchor + datetime.timedelta(days=2)  # two days late: expired
    store = FakeStore([_repeat_row(due, every=1, unit="days", count_left=10,
                                   anchor=anchor)])
    gmail = FakeGmail()
    outcomes = outbound.deliver_due_reminders(_cfg(), gmail, store)

    assert gmail.sent == []
    assert store.rows[0]["status"] == outbound.STATUS_EXPIRED
    assert len(store.rows) == 2
    nxt = store.rows[1]
    assert nxt["status"] == outbound.STATUS_SCHEDULED
    assert nxt["repeat_count_left"] == "9"
    assert outbound.parse_when(nxt["due_at"]) > _now()
    assert any("booked for" in o for o in outcomes)


def test_a_failed_send_does_not_roll_the_series():
    """It is still pending and will be retried; booking the next one now
    would leave the series with two rows and send twice."""
    store = FakeStore([_repeat_row(_now() - datetime.timedelta(minutes=5))])
    outbound.deliver_due_reminders(_cfg(), FakeGmail(fail=True), store)
    assert len(store.rows) == 1
    assert store.rows[0]["status"] == outbound.STATUS_SCHEDULED


def test_a_full_queue_does_not_kill_a_running_series():
    """The roll-forward bypasses the pending quotas deliberately: the new
    row replaces the one finishing, so a queue filled by other people
    must not be able to end someone's medication reminder."""
    rows = [_row(_now() + datetime.timedelta(days=2),
                 recipient=f"other{i}@example.com", body=f"b{i}")
            for i in range(outbound.MAX_PENDING_REMINDERS)]
    rows.append(_repeat_row(_now() - datetime.timedelta(minutes=5)))
    store = FakeStore(rows)

    # Scheduling by hand in this state is refused, which is the contrast.
    text, is_error = _schedule_repeat(store)
    assert is_error and "already pending" in text

    outbound.deliver_due_reminders(_cfg(), FakeGmail(), store)
    booked = [r for r in store.rows if r.get("series_id") == "s-test"
              and r["status"] == outbound.STATUS_SCHEDULED]
    assert len(booked) == 1


def test_a_legacy_row_is_delivered_as_a_one_shot():
    """Rows written before repeats existed have every new column blank,
    and must behave exactly as they did: sent once, nothing rebooked."""
    store = FakeStore([_row(_now() - datetime.timedelta(minutes=5))])
    assert "repeat_every" not in store.rows[0]
    gmail = FakeGmail()
    outbound.deliver_due_reminders(_cfg(), gmail, store)
    assert len(gmail.sent) == 1
    assert len(store.rows) == 1


def test_a_legacy_row_expires_without_rebooking():
    late = _now() - datetime.timedelta(hours=outbound.REMINDER_GRACE_HOURS + 2)
    store = FakeStore([_row(late)])
    outbound.deliver_due_reminders(_cfg(), FakeGmail(), store)
    assert len(store.rows) == 1
    assert store.rows[0]["status"] == outbound.STATUS_EXPIRED


# --------------------------------------------------------------------------
# LISTING AND CANCELLING
#
# The scope rule is the same one that fixes the recipient when scheduling,
# and it is tested the same way: by trying to reach past it.
# --------------------------------------------------------------------------

def test_listing_shows_only_the_senders_own_reminders():
    store = FakeStore([
        _row(_now() + datetime.timedelta(days=1), subject="steve's"),
        _row(_now() + datetime.timedelta(days=1), recipient="ada@example.com",
             subject="ada's"),
    ])
    text, is_error = outbound.handle_reply_list_reminders(
        _cfg(), store, "steve@example.com")
    assert not is_error
    assert "steve's" in text
    assert "ada's" not in text and "ada@example.com" not in text


def test_listing_describes_the_repeat():
    store = FakeStore([_repeat_row(_now() + datetime.timedelta(days=1),
                                   every=3, unit="days", count_left=4)])
    text, _ = outbound.handle_reply_list_reminders(
        _cfg(), store, "steve@example.com")
    assert "r-test" in text and "every 3 days" in text and "4 more" in text


def test_listing_says_so_when_there_is_nothing():
    text, is_error = outbound.handle_reply_list_reminders(
        _cfg(), FakeStore(), "steve@example.com")
    assert not is_error and "Nothing is scheduled" in text


def test_cancelling_stops_the_whole_series():
    store = FakeStore([_repeat_row(_now() + datetime.timedelta(days=1))])
    text, is_error = outbound.handle_reply_cancel_reminder(
        _cfg(), store, "steve@example.com", {"reminder_id": "r-test"})
    assert not is_error, text
    assert store.rows[0]["status"] == outbound.STATUS_CANCELLED
    assert "series" in text
    # And it is no longer pending, so nothing is ever delivered.
    gmail = FakeGmail()
    outbound.deliver_due_reminders(_cfg(), gmail, store)
    assert gmail.sent == []


def test_cancelling_a_series_takes_every_pending_row_of_it():
    store = FakeStore([
        _repeat_row(_now() + datetime.timedelta(days=1), reminder_id="r-a"),
        _repeat_row(_now() + datetime.timedelta(days=2), reminder_id="r-b"),
    ])
    outbound.handle_reply_cancel_reminder(
        _cfg(), store, "steve@example.com", {"reminder_id": "r-a"})
    assert all(r["status"] == outbound.STATUS_CANCELLED for r in store.rows)


def test_a_correspondent_cannot_cancel_someone_elses_reminder():
    """The attack this scoping exists for: an email that names another
    person's reminder id and asks for it to be cancelled."""
    store = FakeStore([_repeat_row(_now() + datetime.timedelta(days=1),
                                   recipient="ada@example.com",
                                   reminder_id="r-ada")])
    text, is_error = outbound.handle_reply_cancel_reminder(
        _cfg(), store, "steve@example.com", {"reminder_id": "r-ada"})
    assert is_error
    assert store.rows[0]["status"] == outbound.STATUS_SCHEDULED
    # And the refusal gives nothing away about whose it is.
    assert "ada" not in text.replace("r-ada", "")


def test_cancelling_needs_an_id_that_exists():
    store = FakeStore([_repeat_row(_now() + datetime.timedelta(days=1))])
    empty, is_error = outbound.handle_reply_cancel_reminder(
        _cfg(), store, "steve@example.com", {"reminder_id": "  "})
    assert is_error and "id" in empty
    missing, is_error = outbound.handle_reply_cancel_reminder(
        _cfg(), store, "steve@example.com", {"reminder_id": "r-nope"})
    assert is_error
    assert store.rows[0]["status"] == outbound.STATUS_SCHEDULED


def test_a_legacy_row_can_be_listed_and_cancelled_by_its_row_number():
    """Reminders booked before ids existed still have to be stoppable;
    the sheet's own row number stands in."""
    store = FakeStore([_row(_now() + datetime.timedelta(days=1))])
    listed, _ = outbound.handle_reply_list_reminders(
        _cfg(), store, "steve@example.com")
    ref = outbound.reminder_ref(store.rows[0])
    assert ref == "row2" and ref in listed

    text, is_error = outbound.handle_reply_cancel_reminder(
        _cfg(), store, "steve@example.com", {"reminder_id": "2"})
    assert not is_error, text
    assert store.rows[0]["status"] == outbound.STATUS_CANCELLED


def test_cancelling_a_legacy_row_leaves_other_rows_alone():
    """A blank series_id is not a series everything else belongs to."""
    store = FakeStore([
        _row(_now() + datetime.timedelta(days=1), body="first"),
        _row(_now() + datetime.timedelta(days=2), body="second"),
    ])
    outbound.handle_reply_cancel_reminder(
        _cfg(), store, "steve@example.com", {"reminder_id": "row2"})
    assert store.rows[0]["status"] == outbound.STATUS_CANCELLED
    assert store.rows[1]["status"] == outbound.STATUS_SCHEDULED


def test_dry_run_cancels_nothing():
    store = FakeStore([_repeat_row(_now() + datetime.timedelta(days=1))])
    text, is_error = outbound.handle_reply_cancel_reminder(
        _cfg(dry_run=True), store, "steve@example.com", {"reminder_id": "r-test"})
    assert not is_error
    assert store.rows[0]["status"] == outbound.STATUS_SCHEDULED


def test_the_cancel_tools_take_no_recipient():
    """Same rule as scheduling: there is no address to point anywhere."""
    for schema in (outbound.reply_list_reminders_schema("steve@example.com"),
                   outbound.reply_cancel_reminder_schema("steve@example.com")):
        props = schema["input_schema"]["properties"]
        assert "to" not in props and "recipient" not in props
    assert outbound.reply_cancel_reminder_schema("steve@example.com")[
        "input_schema"]["required"] == ["reminder_id"]


def test_the_reply_schema_offers_the_repeat_fields_and_no_longer_lies():
    schema = outbound.reply_reminder_schema("steve@example.com")
    props = schema["input_schema"]["properties"]
    for field in ("repeat_every", "repeat_unit", "repeat_count", "repeat_until"):
        assert field in props
    assert "cannot be cancelled" not in schema["description"]
    # The repeat fields stay optional: an ordinary reminder is still the
    # common case and must not need them.
    assert set(schema["input_schema"]["required"]) == {"due_at", "subject", "body"}


def test_reply_path_offers_listing_and_cancelling():
    names = _tool_names()
    assert outbound.LIST_REMINDERS_TOOL in names
    assert outbound.CANCEL_REMINDER_TOOL in names


def test_listing_and_cancelling_go_with_scheduling():
    """All three appear together or not at all — the ability to book a
    repeating reminder without the ability to stop one is the wrong half
    of this feature."""
    for over in ({"have_store": False}, {"reminder_sender": ""},
                 {"cfg": _tools_cfg(enable_outbound=False)}):
        names = _tool_names(**over)
        assert outbound.LIST_REMINDERS_TOOL not in names
        assert outbound.CANCEL_REMINDER_TOOL not in names
