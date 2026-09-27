"""
tests/test_gmail_store.py

Step 5 of MIGRATE-GMAIL.md: `gmail_store.GmailStore`, the store whose
readers ask Gmail while its writers keep writing the sheet.

The contract is the one tests/test_store.py pins for the sheet, restated
here over a fake Gmail, with the two differences the migration accepts:

  * search matches Gmail's way (words), not substrings;
  * `note` is not kept in Gmail — the sheet still has it, and it moves to
    the Journal at step 6.

The fake has two modes. STRICT understands the handful of query terms the
store sends (labels, from:, to:, newer_than:, words) so results can be
checked. PERMISSIVE ignores `q` and `labelIds` and returns the whole
mailbox for every list, like test_gmail_reads.py's fake: a scoping test
that passes against it passes because the Python filter held.

This directory is protected. Indi can read it but not edit it.
"""

import base64
import datetime
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import gmail_store
import memory_file
import sheets_queue
from test_store import ALICE, BOB, MALLORY, NOW, FakeSheets

INDI = "indi@example.org"
HOUR = datetime.timedelta(hours=1)
DAY = datetime.timedelta(days=1)


def _ms(when):
    return str(int(when.timestamp() * 1000))


def _b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


# --------------------------------------------------------------------------
# FAKE GMAIL
# --------------------------------------------------------------------------

class FakeGmail:
    def __init__(self, permissive=False):
        self.permissive = permissive
        self.label_ids = {"INBOX": "INBOX", "SENT": "SENT", "DRAFT": "DRAFT",
                          "UNREAD": "UNREAD"}
        self.msgs = {}
        self.gets = []

    # -- seeding ----------------------------------------------------------

    def add(self, mid, frm, to, subject="hi", body="hello", when=NOW,
            thread=None, labels=(), message_id=None, in_reply_to=""):
        headers = [{"name": "From", "value": frm}, {"name": "To", "value": to},
                   {"name": "Subject", "value": subject},
                   {"name": "Message-ID", "value": message_id or f"<{mid}@mail>"}]
        if in_reply_to:
            headers.append({"name": "In-Reply-To", "value": in_reply_to})
        self.msgs[mid] = {
            "id": mid, "threadId": thread or "t-" + mid, "internalDate": _ms(when),
            "labelIds": [self._id(n) for n in labels],
            "payload": {"mimeType": "text/plain", "headers": headers,
                        "body": {"data": _b64(body)}},
        }

    def _id(self, name):
        if name not in self.label_ids:
            self.label_ids[name] = f"L{len(self.label_ids)}"
        return self.label_ids[name]

    def names(self, mid):
        rev = {v: k for k, v in self.label_ids.items()}
        return {rev[i] for i in self.msgs[mid]["labelIds"]}

    # -- client chain -----------------------------------------------------

    def users(self):
        return self

    def labels(self):
        return SimpleNamespace(list=self._labels_list, create=self._labels_create)

    def messages(self):
        return SimpleNamespace(list=self._list, get=self._get, modify=self._modify)

    def threads(self):
        return SimpleNamespace(get=self._thread)

    @staticmethod
    def _call(fn):
        return SimpleNamespace(execute=fn)

    def _labels_list(self, userId):
        return self._call(lambda: {"labels": [{"id": i, "name": n}
                                              for n, i in self.label_ids.items()]})

    def _labels_create(self, userId, body):
        return self._call(lambda: {"id": self._id(body["name"])})

    def _modify(self, userId, id, body):
        def go():
            m = self.msgs[id]
            m["labelIds"] = [l for l in m["labelIds"] if l not in body.get("removeLabelIds", [])]
            for l in body.get("addLabelIds", []):
                if l not in m["labelIds"]:
                    m["labelIds"].append(l)
            return m
        return self._call(go)

    def _get(self, userId, id, format="full", metadataHeaders=None):
        self.gets.append((id, format))

        def go():
            if id not in self.msgs:
                raise KeyError(id)
            return self.msgs[id]
        return self._call(go)

    def _thread(self, userId, id, format="full", metadataHeaders=None):
        return self._call(lambda: {"messages": [m for m in self.msgs.values()
                                                if m["threadId"] == id]})

    def _matches(self, m, q):
        if not q:
            return True
        hdr = {h["name"].lower(): h["value"].lower() for h in m["payload"]["headers"]}
        text = (hdr.get("subject", "") + " " + base64.urlsafe_b64decode(
            m["payload"]["body"]["data"]).decode().lower())
        names = {n.lower().replace("/", "-") for n in
                 {k for k, v in self.label_ids.items() if v in m["labelIds"]}}
        q = q.replace("(", " ").replace(")", " ")
        if "{" in q:
            group, q = q[q.index("{") + 1:q.index("}")], q[:q.index("{")] + q[q.index("}") + 1:]
            if not any(t[6:] in names for t in group.split() if t.startswith("label:")):
                return False
        for term in q.lower().split():
            if term.startswith("from:"):
                if term[5:] not in hdr.get("from", ""):
                    return False
            elif term.startswith("to:"):
                if term[3:] not in hdr.get("to", ""):
                    return False
            elif term.startswith(("after:", "before:")):
                secs = int(term.split(":", 1)[1]) * 1000
                if (term.startswith("after:") and int(m["internalDate"]) < secs) or \
                        (term.startswith("before:") and int(m["internalDate"]) >= secs):
                    return False
            elif term.startswith("newer_than:"):
                if int(m["internalDate"]) < int(_ms(NOW - int(term[11:-1]) * DAY)):
                    return False
            elif ":" in term:
                return False         # an operator the fake doesn't know
            elif term not in text.split() and term not in hdr.get("subject", "").split():
                return False
        return True

    def _list(self, userId, maxResults=100, labelIds=(), q="", pageToken=None):
        def go():
            hits = [m for m in self.msgs.values()
                    if self.permissive or (all(l in m["labelIds"] for l in labelIds)
                                           and self._matches(m, q))]
            hits.sort(key=lambda m: -int(m["internalDate"]))
            return {"messages": [{"id": m["id"]} for m in hits[:maxResults]]}
        return self._call(go)


# --------------------------------------------------------------------------
# HELPERS
# --------------------------------------------------------------------------

def _gstore(gmail):
    sheets = FakeSheets()
    sheets.tabs[("Q", sheets_queue.QUEUE_TAB)] = [list(sheets_queue.QUEUE_HEADERS)]
    sheets.tabs[("R", sheets_queue.RESPONSES_TAB)] = [list(sheets_queue.RESPONSES_HEADERS)]
    sheets.tabs[("R", sheets_queue.OUTBOUND_TAB)] = [list(sheets_queue.OUTBOUND_HEADERS)]
    cfg = SimpleNamespace(max_body_chars=45000, gmail_status_labels=True)
    store = gmail_store.GmailStore(sheets, None, cfg, gmail)
    store.queue_id, store.responses_id = "Q", "R"
    return store


def _fresh(store):
    again = gmail_store.GmailStore(store.sheets, None, store.cfg, store.gmail)
    again.queue_id, again.responses_id = store.queue_id, store.responses_id
    return again


def _incoming(gmail, mid, addr=ALICE, when=NOW, **kw):
    gmail.add(mid, f"Someone <{addr}>", INDI, when=when, labels=("INBOX",), **kw)
    return {"gmail_msg_id": mid, "thread_id": gmail.msgs[mid]["threadId"],
            "from_raw": f"Someone <{addr}>", "from_addr": addr,
            "subject": kw.get("subject", "hi"), "body": kw.get("body", "hello"),
            "internal_date": int(_ms(when))}


def _handled(store, gmail, mid, addr=ALICE, status="replied", when=NOW, **kw):
    msg = _incoming(gmail, mid, addr, when, **kw)
    row = store.append_queue_row(msg)
    if status != "queued":
        store.set_status(row, status)
    return row


def _reply(store, gmail, to_mid, rid, text="thanks", when=NOW, draft=False, label=True):
    src = gmail.msgs[to_mid]
    rfc = next(h["value"] for h in src["payload"]["headers"] if h["name"] == "Message-ID")
    to = next(h["value"] for h in src["payload"]["headers"] if h["name"] == "From")
    gmail.add(rid, INDI, to, subject="Re: hi", body=text, when=when,
              thread=src["threadId"], labels=("DRAFT",) if draft else ("SENT",),
              in_reply_to=rfc)
    store.append_response(to_mid, to, text, reply_id=rid if label else "")


@pytest.fixture
def frozen_now(monkeypatch):
    monkeypatch.setattr(gmail_store, "_utc_now", lambda: NOW)
    return NOW


# --------------------------------------------------------------------------
# STATUS
# --------------------------------------------------------------------------

def test_status_comes_from_the_label_and_survives_a_new_run():
    g = FakeGmail()
    store = _gstore(g)
    row = store.append_queue_row(_incoming(g, "m1"))
    assert store.find_queue_row("m1")["status"] == "queued"
    store.set_status(row, "failed", "boom")
    assert _fresh(store).find_queue_row("m1")["status"] == "failed"
    store.set_status(row, "replied")
    later = _fresh(store).find_queue_row("m1")
    assert later["status"] == "replied"
    assert g.names("m1") & set(sheets_queue.STATUS_LABELS.values()) == {"Indi/Replied"}
    assert store.find_queue_row("nope") is None


def test_status_writes_still_reach_the_sheet():
    g = FakeGmail()
    store = _gstore(g)
    row = store.append_queue_row(_incoming(g, "m1"))
    store.set_status(store.find_queue_row("m1"), "skipped", "automated")
    sheet = sheets_queue.SheetStore(store.sheets, None, store.cfg)
    sheet.queue_id = "Q"
    assert sheet.find_queue_row("m1")["status"] == "skipped"
    assert sheet.find_queue_row("m1")["note"] == "automated"


def test_a_missing_label_falls_back_to_the_sheet_rather_than_forgetting():
    # A label write failed after the sheet said "replied". Reading Gmail
    # alone would make the message look new and answer the person twice.
    g = FakeGmail()
    store = _gstore(g)
    row = store.append_queue_row(_incoming(g, "m1"))
    store.set_status(row, "replied")
    g.msgs["m1"]["labelIds"] = ["INBOX"]
    assert _fresh(store).find_queue_row("m1")["status"] == "replied"


# --------------------------------------------------------------------------
# messages_for_sender
# --------------------------------------------------------------------------

def test_messages_for_sender_is_one_exact_sender_replied_only_oldest_first():
    g = FakeGmail()
    store = _gstore(g)
    _handled(store, g, "a3", when=NOW - 1 * HOUR)
    _handled(store, g, "b1", BOB, when=NOW - 5 * HOUR)
    _handled(store, g, "a1", when=NOW - 30 * HOUR)
    for mid, status in (("aF", "failed"), ("aS", "skipped"), ("aI", "ignored"), ("aQ", "queued")):
        _handled(store, g, mid, status=status, when=NOW - 18 * HOUR)
    _handled(store, g, "a2", when=NOW - 10 * HOUR)
    # from: is a search, not equality — this address contains Alice's.
    _handled(store, g, "x1", "x" + ALICE, when=NOW - 2 * HOUR)
    assert [r["gmail_msg_id"] for r in store.messages_for_sender(ALICE)] == ["a1", "a2", "a3"]
    assert [r["gmail_msg_id"] for r in store.messages_for_sender(ALICE.upper())] == ["a1", "a2", "a3"]
    assert store.messages_for_sender(MALLORY) == []


def test_messages_for_sender_does_not_fetch_bodies_it_is_told_to_skip():
    g = FakeGmail()
    store = _gstore(g)
    for i in range(5):
        _handled(store, g, f"a{i}", when=NOW - (10 - i) * HOUR, body=f"text {i}")
    g.gets.clear()
    rows = _fresh(store).messages_for_sender(ALICE, exclude_ids={"a0", "a1", "a2"})
    assert [r["gmail_msg_id"] for r in rows] == ["a3", "a4"]
    full = {mid for mid, fmt in g.gets if fmt == "full"}
    assert full == {"a3", "a4"}


def test_memory_pending_rows_under_gmail_store():
    g = FakeGmail()
    store = _gstore(g)
    for i in range(4):
        _handled(store, g, f"a{i}", when=NOW - (10 - i) * HOUR)
    mem = SimpleNamespace(sender_addr=ALICE,
                          watermark=memory_file.Watermark(ids=["a0", "a1"]))
    pending = memory_file.SenderMemory.pending_rows(mem, store)
    assert [r["gmail_msg_id"] for r in pending] == ["a2", "a3"]


# --------------------------------------------------------------------------
# replies
# --------------------------------------------------------------------------

def test_replies_are_found_in_the_thread_and_labelled():
    g = FakeGmail()
    store = _gstore(g)
    _handled(store, g, "m1")
    _handled(store, g, "m2", BOB)
    _reply(store, g, "m1", "r1", "first")
    _reply(store, g, "m1", "r2", "second", when=NOW + HOUR, draft=True)
    _reply(store, g, "m2", "r3", "to bob")
    assert "Indi/Reply" in g.names("r1") and "Indi/Reply" in g.names("r2")
    rows = _fresh(store).recent_messages(10)
    assert _fresh(store).responses_for(rows) == {"m1": ["first", "second"], "m2": ["to bob"]}


def test_a_hand_sent_draft_that_lost_its_label_still_counts_as_the_reply():
    g = FakeGmail()
    store = _gstore(g)
    _handled(store, g, "m1")
    _reply(store, g, "m1", "r1", "sent by hand", label=False)
    rows = store.recent_messages(5)
    assert store.responses_for(rows) == {"m1": ["sent by hand"]}


def test_replies_in_last_day_is_an_exact_rolling_24h_of_indis_replies(frozen_now):
    g = FakeGmail()
    store = _gstore(g)
    tick = datetime.timedelta(seconds=1)
    for i, when in enumerate([NOW - DAY + tick, NOW - DAY, NOW - DAY - tick,
                              NOW - 13 * HOUR, NOW - 2 * DAY]):
        _handled(store, g, f"m{i}", when=when - HOUR)
        _reply(store, g, f"m{i}", f"r{i}", when=when, draft=(i == 3))
    _handled(store, g, "b1", BOB)
    _reply(store, g, "b1", "rb", when=NOW)
    # Heartbeat mail to Alice is not a reply and does not count.
    g.add("o1", INDI, ALICE, when=NOW, labels=("SENT",))
    store.append_outbound(ALICE, "hi", "x", "why", message_id="o1")
    assert store.replies_in_last_day(ALICE) == 3        # drafts included, edge inclusive
    assert store.replies_in_last_day(ALICE.upper()) == 3
    assert store.replies_in_last_day(BOB) == 1
    assert store.replies_in_last_day(MALLORY) == 0


# --------------------------------------------------------------------------
# known correspondents and outbound
# --------------------------------------------------------------------------

def test_known_correspondents_is_replied_ignored_and_outbound_only():
    g = FakeGmail()
    store = _gstore(g)
    _handled(store, g, "1", "Replied@Example.com")
    _handled(store, g, "2", "ignored@example.com", status="ignored")
    _handled(store, g, "3", "skipped@example.com", status="skipped")
    _handled(store, g, "4", "failed@example.com", status="failed")
    _handled(store, g, "5", "queued@example.com", status="queued")
    g.add("o1", INDI, "Written@Example.com", labels=("SENT",))
    store.append_outbound("Written@Example.com", "s", "b", "r", message_id="o1")
    # Pre-Indi mail in Sent is not a correspondent.
    g.add("old", INDI, "stranger@example.com", labels=("SENT",))
    assert _fresh(store).known_correspondents() == {
        "replied@example.com", "ignored@example.com", "written@example.com"}


def test_outbound_sent_this_run_is_known_at_once():
    g = FakeGmail()
    store = _gstore(g)
    assert store.known_correspondents() == set()
    g.add("o1", INDI, "new@example.com", labels=("SENT",))
    store.append_outbound("new@example.com", "s", "b", "r", message_id="o1")
    assert store.known_correspondents() == {"new@example.com"}


def test_outbound_budgets_count_the_exact_24h(frozen_now):
    g = FakeGmail()
    store = _gstore(g)
    for mid, to, when in (("o1", ALICE, NOW - HOUR), ("o2", BOB, NOW - 23 * HOUR),
                          ("o3", ALICE, NOW - 25 * HOUR)):
        g.add(mid, INDI, to, subject=mid, when=when, labels=("SENT",))
        store.append_outbound(to, mid, "b", "r", message_id=mid)
    assert store.outbound_today() == 2
    assert store.outbound_today_to(ALICE) == 1
    assert [r["subject"] for r in store.recent_outbound(2)] == ["o2", "o1"]


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------

def _history(permissive=False):
    g = FakeGmail(permissive=permissive)
    store = _gstore(g)
    _handled(store, g, "a1", subject="Authentication trouble", body="cannot log in",
             when=NOW - 30 * HOUR)
    _handled(store, g, "a2", subject="Lunch", body="thursday?", when=NOW - 2 * HOUR)
    _handled(store, g, "b1", BOB, subject="auth secrets", body=f"my password cc {ALICE}",
             when=NOW - HOUR)
    _handled(store, g, "b2", BOB, status="failed", subject="from:alice@example.com",
             body="OR anything", when=NOW - 3 * HOUR)
    _reply(store, g, "a1", "ra", "try resetting")
    _reply(store, g, "b1", "rb", "noted")
    return _fresh(store), g


def test_search_matches_words_newest_first_with_replies():
    store, _ = _history()
    got = store.search("password", limit=10)
    assert [m["gmail_msg_id"] for m in got] == ["b1"]
    assert got[0]["responses"] == ["noted"]
    assert [m["gmail_msg_id"] for m in store.search("authentication", limit=10)] == ["a1"]
    # The accepted difference from the sheet: a word, not a substring.
    assert store.search("authent", limit=10) == []


@pytest.mark.parametrize("query", [
    "", "auth", "password", "secrets", ALICE, BOB, "from:", "from:bob@example.com",
    "from:alice@example.com", "OR", "* OR from:*", "' OR 1=1 --", "\" OR \"",
    "in:anywhere", "-from:alice@example.com", "{from:bob@example.com}",
    ") OR (from:bob@example.com", "label:indi-replied",
])
@pytest.mark.parametrize("sender", ["", "bob", BOB, "@", "example.com"])
def test_sender_scoped_search_never_returns_another_correspondent(query, sender):
    store, g = _history(permissive=True)
    got = store.search(query, sender=sender, limit=100, restrict_to_sender=ALICE)
    for m in got:
        assert m["gmail_msg_id"] in {"a1", "a2"}
        assert "noted" not in m["responses"]


def test_scope_uses_the_address_not_the_display_name():
    g = FakeGmail(permissive=True)
    store = _gstore(g)
    g.add("m1", f"{ALICE} <{MALLORY}>", INDI, body="private", labels=("INBOX",))
    store.append_queue_row({"gmail_msg_id": "m1", "thread_id": "t-m1",
                            "from_raw": f"{ALICE} <{MALLORY}>", "from_addr": MALLORY,
                            "subject": "hi", "body": "private", "internal_date": 0})
    assert store.search("private", restrict_to_sender=ALICE, limit=10) == []


def test_sender_scoped_search_still_finds_the_senders_own_mail():
    store, _ = _history()
    got = store.search("", limit=100, restrict_to_sender=ALICE.upper())
    assert [m["gmail_msg_id"] for m in got] == ["a2", "a1"]


# --------------------------------------------------------------------------
# the switch
# --------------------------------------------------------------------------

def test_gmail_reads_are_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("INDI_GMAIL_READS", raising=False)
    assert gmail_store.gmail_reads_enabled(SimpleNamespace()) is False
    monkeypatch.setenv("INDI_GMAIL_READS", "true")
    assert gmail_store.gmail_reads_enabled(SimpleNamespace()) is True


def test_the_search_tool_describes_whichever_store_answers():
    import claude_api
    sheet = claude_api.history_search_schema(True, gmail_search=False)["description"]
    gmail = claude_api.history_search_schema(True, gmail_search=True)["description"]
    assert "substring" in sheet and "substring" not in gmail
    assert "whole words" in gmail


# --------------------------------------------------------------------------
# cutover_prep.py
# --------------------------------------------------------------------------

def test_prep_labels_past_replies_and_outbound():
    import cutover_prep
    g = FakeGmail()
    _incoming(g, "m1")
    rfc = "<m1@mail>"
    g.add("r1", INDI, ALICE, thread="t-m1", labels=("SENT",), in_reply_to=rfc)
    g.add("x1", INDI, ALICE, thread="t-m1", labels=("SENT",))       # not a reply to m1
    g.add("o1", INDI, BOB, subject="thinking of you", labels=("SENT",))
    queue = [{"gmail_msg_id": "m1", "thread_id": "t-m1"}]
    responses = [{"gmail_msg_id": "m1"}]
    outbound = [{"recipient": BOB, "subject": "thinking of you", "created_at": NOW.isoformat()}]
    quiet = lambda *_: None

    dry = cutover_prep.label_replies(g, queue, responses, apply=False, out=quiet)
    assert dry["would_label"] == 1 and "Indi/Reply" not in g.names("r1")
    cutover_prep.label_replies(g, queue, responses, apply=True, out=quiet)
    assert "Indi/Reply" in g.names("r1") and "Indi/Reply" not in g.names("x1")
    again = cutover_prep.label_replies(g, queue, responses, apply=True, out=quiet)
    assert again["already"] == 1 and again["labelled"] == 0

    cutover_prep.label_outbound(g, outbound, apply=True, out=quiet)
    assert "Indi/Outbound" in g.names("o1")


def test_prep_converts_v1_memory_against_the_sheets_order():
    import cutover_prep
    from test_store import _store
    t = lambda h: (NOW - datetime.timedelta(hours=h)).isoformat()
    store = _store(messages=[
        {"gmail_msg_id": "a3", "sender_addr": ALICE, "received_at": t(1)},
        {"gmail_msg_id": "a1", "sender_addr": ALICE, "received_at": t(30)},
        {"gmail_msg_id": "a2", "sender_addr": ALICE, "received_at": t(10)},
    ])
    files = {memory_file.memory_file_name(ALICE):
             "# indi-memory v1 | sender: alice@example.com | folded: 2 | updated: x\n\nlikes tea\n"}
    backups = []
    io = SimpleNamespace(
        memory_file_name=memory_file.memory_file_name,
        find_file_id=lambda _d, name: name if name in files else None,
        read_file=lambda _d, fid: files.get(fid, "") if fid else "",
        parse_memory=memory_file.parse_memory,
        serialise_memory=memory_file.serialise_memory,
        save_backup=lambda _d, addr, raw, keep: backups.append(raw),
        write_file=lambda _d, name, text, fid=None: files.__setitem__(name, text),
    )
    quiet = lambda *_: None
    cutover_prep.convert_memory(None, store, apply=False, keep=3, out=quiet, io=io)
    assert "v1" in files[memory_file.memory_file_name(ALICE)]
    counts = cutover_prep.convert_memory(None, store, apply=True, keep=3, out=quiet, io=io)
    assert counts["converted"] == 1 and len(backups) == 1
    watermark, body = memory_file.parse_memory(files[memory_file.memory_file_name(ALICE)])
    # The first two in the sheet's order — oldest first — not in row order.
    assert watermark.ids == frozenset({"a1", "a2"}) and body.strip() == "likes tea"
    again = cutover_prep.convert_memory(None, store, apply=True, keep=3, out=quiet, io=io)
    assert again["already_v2"] == 1 and len(backups) == 1
