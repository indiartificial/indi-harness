"""
tests/test_gmail_reads.py

Step 3 of MIGRATE-GMAIL.md: Gmail-backed shadows of three store readers
(`gmail_reads.py`). The sheet still answers every call; the Gmail reader
runs beside it and only logs where the two disagree.

What matters here, in order:

  * **Sender scope holds in Python.** The fake Gmail client below is
    deliberately PERMISSIVE: it ignores `q` and `labelIds` and returns the
    whole mailbox for every list call. Whatever the model writes into the
    query, a sender-scoped search must still return nothing belonging to
    anyone else — which, against this fake, can only be because the Python
    filter is doing it. The query/sender grid mirrors the 80 combinations
    in test_store.py, plus the operators Gmail has and the sheet didn't.
  * **The 24h rate-limit window is exact.** 23h59m counts, 24h01m does not,
    whatever Gmail's `newer_than:` would have said.
  * **known_correspondents is not Sent wholesale**: labelled senders, plus
    recipients of mail sent at or after the start date, and nothing
    before it.
  * **A failing Gmail client changes nothing.** Return values and sheet
    writes are identical to running with the shadow off.
  * **Log lines are bounded** and carry ids, not bodies.

This directory is protected. Indi can read it but not edit it.
"""

import datetime
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import gmail_reads
import sheets_queue
from test_store import ALICE, BOB, MALLORY, NOW, _iso, _store


INDI = "indi@example.org"
DAY = datetime.timedelta(days=1)


def _ms(when: datetime.datetime) -> str:
    return str(int(when.timestamp() * 1000))


# --------------------------------------------------------------------------
# FAKES
# --------------------------------------------------------------------------

class PermissiveGmail:
    """A Gmail client that answers every `messages.list` with the WHOLE
    mailbox, ignoring `q`, `labelIds` and everything else.

    This is the point of the fake. Real Gmail would narrow by `from:` and
    labels; this one never does, so any test that passes against it passes
    because gmail_reads filtered in Python, not because the query string
    happened to be well-formed. Every call is recorded so tests can also
    assert what was asked for and how often.
    """

    PERMISSIVE = True

    def __init__(self, messages=(), labels=sheets_queue.STATUS_LABELS.values()):
        self.label_ids = {name: f"L{i}" for i, name in enumerate(labels)}
        self.messages_by_id = {m["id"]: m for m in messages}
        self.calls = []

    def users(self):
        return self

    def labels(self):
        return SimpleNamespace(list=self._labels_list)

    def messages(self):
        return SimpleNamespace(list=self._list, get=self._get)

    def _call(self, name, kw, result):
        self.calls.append((name, kw))
        return SimpleNamespace(execute=result)

    def _labels_list(self, **kw):
        return self._call("labels.list", kw, lambda: {"labels": [
            {"id": i, "name": n} for n, i in self.label_ids.items()]})

    def _list(self, **kw):
        # Newest first, as Gmail lists. Everything, every time.
        ordered = sorted(self.messages_by_id.values(),
                         key=lambda m: int(m["internalDate"]), reverse=True)
        return self._call("messages.list", kw, lambda: {
            "messages": [{"id": m["id"], "threadId": "t" + m["id"]} for m in ordered]})

    def _get(self, **kw):
        m = self.messages_by_id[kw["id"]]
        wanted = {h.lower() for h in kw.get("metadataHeaders", [])}
        headers = [{"name": name, "value": m[name.lower()]}
                   for name in ("From", "To", "Cc")
                   if m.get(name.lower()) and name.lower() in wanted]
        labels = [self.label_ids.get(n, n) for n in m.get("labels", ())]
        return self._call("messages.get", kw, lambda: {
            "id": m["id"], "threadId": "t" + m["id"], "labelIds": labels,
            "internalDate": m["internalDate"],
            "payload": {"headers": headers},
            # A body the comparison must never ask for.
            "snippet": "SECRET BODY TEXT" if kw.get("format") != "metadata" else "",
        })

    def count(self, name):
        return sum(1 for c, _ in self.calls if c == name)


class ExplodingGmail:
    """Raises on every call, at every step of the builder chain."""

    def __init__(self):
        self.calls = 0

    def __getattr__(self, name):
        self.calls += 1
        raise RuntimeError(f"gmail is down ({name})")


def _m(msg_id, frm, hours_ago, labels=("Indi/Replied",), to=INDI, cc=""):
    return {"id": msg_id, "from": frm, "to": to, "cc": cc, "labels": labels,
            "internalDate": _ms(NOW - datetime.timedelta(hours=hours_ago))}


def _mailbox():
    """Alice's two handled messages among everything that might leak."""
    return [
        _m("a1", "Alice <alice@example.com>", 30),
        _m("a2", "Alice <Alice@Example.COM>", 2, labels=("Indi/Ignored",)),
        _m("b1", "Bob <bob@example.com>", 1),
        _m("b2", "Bob <bob@example.com>", 3, labels=("Indi/Failed",)),
        # Display names are free text; the addr-spec is mallory's.
        _m("s1", "alice@example.com <mallory@evil.test>", 0.5),
        _m("s2", "\"Alice\" <alice@example.com.evil.test>", 0.6),
        _m("s3", "alice@example.com, mallory@evil.test", 0.7),
        _m("s4", "mallory@evil.test", 0.8, to=ALICE, cc=ALICE),
        # Alice's own mail, but never handled by Indi (pre-Indi, or spam).
        _m("u1", "Alice <alice@example.com>", 4, labels=("INBOX",)),
        # Indi's reply to Alice: from Indi, to Alice.
        _m("x1", f"Indi <{INDI}>", 1.5, labels=("SENT",), to=ALICE),
    ]


def _reads(messages=None, **kw):
    fake = PermissiveGmail(_mailbox() if messages is None else messages, **kw)
    return fake, gmail_reads.GmailReads(fake)


# --------------------------------------------------------------------------
# search: the sender scope
# --------------------------------------------------------------------------

HOSTILE_QUERIES = [
    # The same sixteen test_store.py uses against the sheet...
    "", "auth", "password", "secrets", ALICE, BOB, "from:", "from:bob@example.com",
    "from:alice@example.com", "OR", "* OR from:*", "' OR 1=1 --", "\" OR \"",
    "in:anywhere", "-from:alice@example.com", "{from:bob@example.com}",
    # ...and Gmail's own operators, which the sheet never had.
    "OR from:bob@example.com", ") OR (from:bob@example.com", "}",
    "label:indi-replied", "label:inbox OR in:anywhere", "(from:mallory@evil.test)",
    "-label:indi-replied", "\"alice@example.com\"", "in:sent OR in:trash",
]
SENDERS = ["", "bob", BOB, "@", "example.com"]


@pytest.mark.parametrize("query", HOSTILE_QUERIES)
@pytest.mark.parametrize("sender", SENDERS)
def test_sender_scoped_gmail_search_never_returns_another_correspondent(query, sender):
    fake, reads = _reads()
    assert fake.PERMISSIVE   # the Python filter is the only thing in the way
    got = reads.search_ids(query, sender=sender, limit=100, restrict_to_sender=ALICE)
    assert fake.count("messages.list") == 1
    for msg_id in got:
        assert gmail_reads.normalise_addr(fake.messages_by_id[msg_id]["from"]) == ALICE
    assert set(got) <= {"a1", "a2"}


@pytest.mark.parametrize("restrict", [ALICE, ALICE.upper(), "Alice <alice@example.com>"])
def test_sender_scoped_gmail_search_still_finds_the_senders_own_handled_mail(restrict):
    # Newest first; u1 is Alice's but carries no Indi label, and the sheet
    # would never have held it either.
    fake, reads = _reads()
    assert reads.search_ids("", limit=100, restrict_to_sender=restrict) == ["a2", "a1"]


def test_sender_scope_for_a_stranger_is_empty():
    fake, reads = _reads()
    assert reads.search_ids("", limit=100, restrict_to_sender=MALLORY.replace(
        "mallory", "nobody")) == []
    assert reads.search_ids("auth", sender="bob", limit=100,
                            restrict_to_sender="carol@example.com") == []


def test_scoped_query_sends_from_as_an_optimisation_only():
    fake, reads = _reads()
    reads.search_ids("OR from:bob@example.com", restrict_to_sender=ALICE)
    q = [kw for c, kw in fake.calls if c == "messages.list"][0]["q"]
    assert q.startswith(f"from:{ALICE} ")
    assert "(OR from:bob@example.com)" in q


def test_an_address_unsafe_for_a_query_is_left_out_of_it_and_still_enforced():
    odd = '"a b"@example.com'
    fake, reads = _reads(_mailbox() + [_m("o1", f"Odd <{odd}>", 5)])
    got = reads.search_ids("", limit=100, restrict_to_sender=odd)
    q = [kw for c, kw in fake.calls if c == "messages.list"][0]["q"]
    assert "from:" not in q
    assert got == ["o1"]


def test_unscoped_search_is_every_handled_message_newest_first_to_the_limit():
    fake, reads = _reads()
    assert reads.search_ids("", limit=3) == ["s1", "s2", "s3"]
    assert reads.search_ids("", sender="bob", limit=10) == ["b1", "b2"]


def test_search_fetches_metadata_only_and_is_bounded():
    many = [_m(f"b{i}", BOB, i) for i in range(200)]
    fake, reads = _reads(many)
    assert reads.search_ids("", limit=5, restrict_to_sender=ALICE) == []
    assert fake.count("labels.list") == 1
    assert fake.count("messages.list") == 1
    assert fake.count("messages.get") == gmail_reads.SEARCH_MAX_CANDIDATES
    for name, kw in fake.calls:
        if name == "messages.get":
            assert kw["format"] == "metadata"
            assert kw["metadataHeaders"] == ["From"]


def test_search_stops_fetching_once_it_has_enough():
    fake, reads = _reads()
    reads.search_ids("", limit=2, restrict_to_sender=ALICE)
    # a2 and a1 are the 8th and 10th newest; nothing past a1 is fetched.
    assert fake.count("messages.get") <= 10


def test_search_with_no_indi_labels_in_the_mailbox_makes_no_list_call():
    fake, reads = _reads(labels=())
    assert reads.search_ids("", restrict_to_sender=ALICE) == []
    assert fake.count("messages.list") == 0


# --------------------------------------------------------------------------
# replies_in_last_day: the exact 24h edge
# --------------------------------------------------------------------------

def _sent(msg_id, to, ago, labels=("SENT",)):
    return {"id": msg_id, "from": INDI, "to": to, "cc": "", "labels": labels,
            "internalDate": _ms(NOW - ago)}


def test_replies_in_last_day_cuts_at_exactly_24_hours_in_python():
    minute = datetime.timedelta(minutes=1)
    fake, reads = _reads([
        _sent("in", f"Alice <{ALICE}>", DAY - minute),          # 23h59m: counts
        _sent("edge", ALICE, DAY),                               # 24h00m: counts, like the sheet
        _sent("out", ALICE, DAY + minute),                       # 24h01m: does not
        _sent("old", ALICE, 2 * DAY - minute),                   # inside newer_than:2d, still out
        _sent("bob", BOB, minute),                               # someone else
        _sent("inbound", ALICE, minute, labels=("INBOX",)),      # not sent mail
        _sent("both", f"{BOB}, {ALICE.upper()}", 2 * minute),    # addressed to her too
    ])
    assert reads.replies_in_last_day(ALICE, NOW) == 3
    assert reads.replies_in_last_day(MALLORY, NOW) == 0
    q = [kw for c, kw in fake.calls if c == "messages.list"][0]["q"]
    assert q == f"in:sent newer_than:2d to:{ALICE}"
    assert all(kw["format"] == "metadata" for c, kw in fake.calls if c == "messages.get")


def test_replies_in_last_day_is_bounded():
    fake, reads = _reads([_sent(f"m{i}", ALICE, datetime.timedelta(minutes=i))
                          for i in range(200)])
    assert reads.replies_in_last_day(ALICE, NOW) == gmail_reads.REPLIES_MAX_FETCH
    assert fake.count("messages.list") == 1
    assert fake.count("messages.get") == gmail_reads.REPLIES_MAX_FETCH


# --------------------------------------------------------------------------
# known_correspondents
# --------------------------------------------------------------------------

EPOCH = NOW - 30 * DAY


def _known_mailbox():
    return [
        _m("r", "Carol <carol@example.com>", 100),
        _m("i", "dave@example.com", 100, labels=("Indi/Ignored",)),
        _m("k", "noreply@shop.test", 100, labels=("Indi/Skipped",)),
        _m("f", "eve@example.com", 100, labels=("Indi/Failed",)),
        _m("q", "frank@example.com", 100, labels=("Indi/Queued",)),
        _m("p", "stranger@example.com", 100, labels=("INBOX",)),
        _sent("before", "Old Friend <old@example.com>", 31 * DAY),
        _sent("at", "atepoch@example.com", 30 * DAY),
        _sent("after", "New <new@example.com>", DAY),
        {**_sent("cc", "to@example.com", DAY), "cc": "Copied <CC@example.com>"},
        {**_sent("draft", "draft@example.com", DAY), "labels": ("DRAFT",)},
    ]


def test_known_correspondents_is_labelled_senders_plus_sent_since_the_start():
    fake = PermissiveGmail(_known_mailbox())
    got = gmail_reads.GmailReads(fake, start=EPOCH).known_correspondents()
    assert got == {"carol@example.com", "dave@example.com", "atepoch@example.com",
                   "new@example.com", "to@example.com", "cc@example.com"}
    assert all(kw["format"] == "metadata" for c, kw in fake.calls if c == "messages.get")


def test_known_correspondents_leaves_sent_out_without_a_start_date():
    fake = PermissiveGmail(_known_mailbox())
    got = gmail_reads.GmailReads(fake).known_correspondents()
    assert got == {"carol@example.com", "dave@example.com"}
    assert not any("in:sent" in kw.get("q", "") for c, kw in fake.calls
                   if c == "messages.list")


def test_known_correspondents_stops_at_its_fetch_bound(monkeypatch):
    monkeypatch.setattr(gmail_reads, "KNOWN_MAX_FETCH", 3)
    fake = PermissiveGmail(_known_mailbox())
    reads = gmail_reads.GmailReads(fake, start=EPOCH)
    reads.known_correspondents()
    assert fake.count("messages.get") == 3
    assert reads.truncated


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def _clear(monkeypatch, name):
    for suffix in ("", "_LINUX", "_WINDOWS", "_MACOS"):
        monkeypatch.delenv(name + suffix, raising=False)


def test_shadow_reads_default_off(monkeypatch):
    _clear(monkeypatch, "INDI_GMAIL_SHADOW_READS")
    assert gmail_reads.shadow_reads_enabled(SimpleNamespace()) is False
    monkeypatch.setenv("INDI_GMAIL_SHADOW_READS", "true")
    assert gmail_reads.shadow_reads_enabled(SimpleNamespace()) is True
    monkeypatch.setenv("INDI_GMAIL_SHADOW_READS", "nope")
    assert gmail_reads.shadow_reads_enabled(SimpleNamespace()) is False


def test_start_date_parses_dates_and_ignores_rubbish(monkeypatch):
    _clear(monkeypatch, "INDI_START_DATE")
    assert gmail_reads.start_date() is None
    monkeypatch.setenv("INDI_START_DATE", "2026-06-01")
    assert gmail_reads.start_date() == datetime.datetime(
        2026, 6, 1, tzinfo=datetime.timezone.utc)
    monkeypatch.setenv("INDI_START_DATE", "last tuesday")
    assert gmail_reads.start_date() is None


# --------------------------------------------------------------------------
# Through the store: the sheet decides, Gmail only logs
# --------------------------------------------------------------------------

def _history_rows():
    t = lambda h: _iso(NOW - datetime.timedelta(hours=h))
    return dict(
        messages=[
            {"gmail_msg_id": "a1", "sender_addr": ALICE, "subject": "Authentication",
             "body": "cannot log in", "received_at": t(30)},
            {"gmail_msg_id": "a2", "sender_addr": ALICE, "subject": "Lunch",
             "body": "thursday?", "received_at": t(2), "status": "ignored"},
            {"gmail_msg_id": "b1", "sender_addr": BOB, "subject": "auth secrets",
             "body": "password", "received_at": t(1)},
        ],
        replies=[("a1", ALICE, "try resetting", _iso(NOW)),
                 ("b1", BOB, "noted", _iso(NOW - datetime.timedelta(hours=3)))],
        outbound=["written@example.com"],
    )


def _with_shadow(store, gmail):
    # start_date="" pins "no start date" whatever the machine's .env says.
    cfg = SimpleNamespace(max_body_chars=store.cfg.max_body_chars, start_date="")
    again = sheets_queue.SheetStore(store.sheets, None, cfg,
                                    shadow_gmail_service=gmail)
    again.queue_id, again.responses_id = store.queue_id, store.responses_id
    return again


def _exercise(store):
    return (
        store.search("auth", limit=10),
        store.search("", limit=10, restrict_to_sender=ALICE),
        store.search("from:bob@example.com OR", sender="bob", limit=10,
                     restrict_to_sender=ALICE),
        store.replies_in_last_day(ALICE),
        store.replies_in_last_day(BOB),
        store.known_correspondents(),
    )


def test_a_gmail_client_that_always_raises_changes_nothing(caplog):
    plain = _store(**_history_rows())
    shadowed = _with_shadow(_store(**_history_rows()), ExplodingGmail())

    with caplog.at_level(logging.WARNING, logger="indi.gmail_reads"):
        got = _exercise(shadowed)
    assert got == _exercise(plain)
    # The readers write nothing to the sheet, with or without the shadow.
    assert shadowed.sheets.tabs == plain.sheets.tabs
    assert shadowed.sheets.updates == plain.sheets.updates == []
    assert shadowed._shadow.reads.gmail.calls > 0
    failures = [r for r in caplog.records if "Shadow read failed" in r.getMessage()]
    assert len(failures) == 6


def test_without_a_shadow_client_the_store_never_touches_gmail():
    store = _store(**_history_rows())
    assert store._shadow is None


def test_shadow_disagreement_is_logged_with_inputs_and_ids(caplog):
    gmail = PermissiveGmail(_mailbox())
    store = _with_shadow(_store(**_history_rows()), gmail)
    with caplog.at_level(logging.WARNING, logger="indi.gmail_reads"):
        got = store.search("", limit=10, restrict_to_sender=ALICE)
    assert [m["gmail_msg_id"] for m in got] == ["a2", "a1"]   # the sheet's answer
    assert not [r for r in caplog.records if "disagreement" in r.getMessage()]

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="indi.gmail_reads"):
        got = store.search("auth", limit=10)
    assert [m["gmail_msg_id"] for m in got] == ["b1", "a1"]
    [line] = [r.getMessage() for r in caplog.records]
    assert "[search]" in line and "query='auth'" in line
    assert "gmail only" in line and "s1" in line
    assert "password" not in line and "SECRET" not in line


def test_known_correspondents_difference_is_logged_both_ways(caplog):
    gmail = PermissiveGmail(_known_mailbox())
    store = _with_shadow(_store(**_history_rows()), gmail)
    with caplog.at_level(logging.WARNING, logger="indi.gmail_reads"):
        got = store.known_correspondents()
    assert got == {ALICE, BOB, "written@example.com"}
    [record] = caplog.records
    line = record.getMessage()
    assert record.levelno == logging.WARNING
    assert "[known_correspondents]" in line and "start_date=unset" in line
    assert ("sheet only [alice@example.com, bob@example.com, written@example.com] (3)"
            in line)
    assert "gmail only [carol@example.com, dave@example.com] (2)" in line


def test_disagreement_lines_are_bounded(caplog):
    shadow = gmail_reads.ShadowReader(PermissiveGmail([]))
    huge = {f"person{i:04d}@example.com" for i in range(2000)}
    with caplog.at_level(logging.WARNING, logger="indi.gmail_reads"):
        shadow.compare_known_correspondents(huge)
        shadow.compare_search([f"id{i}" for i in range(500)], "x" * 10_000,
                              "", 500, "")
    for record in caplog.records:
        assert len(record.getMessage()) < 2000
    assert "+1990 more" in caplog.records[0].getMessage()
    assert "+490 more" in caplog.records[1].getMessage()


def test_replies_in_last_day_shadow_uses_the_same_now_as_the_sheet(caplog):
    minute = datetime.timedelta(minutes=1)
    gmail = PermissiveGmail([_sent("in", ALICE, DAY - minute),
                             _sent("out", ALICE, DAY + minute)])
    store = _with_shadow(_store(replies=[
        ("m1", ALICE, "x", _iso(NOW - DAY + minute)),
        ("m2", ALICE, "y", _iso(NOW - DAY - minute)),
    ]), gmail)

    class Frozen(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz else NOW.replace(tzinfo=None)

    import unittest.mock
    with unittest.mock.patch.object(sheets_queue, "datetime", SimpleNamespace(
            datetime=Frozen, timezone=datetime.timezone, timedelta=datetime.timedelta)), \
            caplog.at_level(logging.WARNING, logger="indi.gmail_reads"):
        assert store.replies_in_last_day(ALICE) == 1
    assert not caplog.records
