"""
tests/test_consult.py

Phase 3 (consult.py): the pre-send check, the broker, consultation
emails, answers, grants and follow-ups, and their place in
process_message.

Model calls are scripted. What is tested is what code does with each
possible answer — and that nothing is widened except by a grant made from
the owner's own authenticated yes.

This directory is protected. Indi can read it but not edit it.
"""

import datetime
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import audience
import check_and_reply
import claude_api
import consult
import gmail_io
import terms_gate
from test_audience import ANN, BOB, CAROL, INDI, world  # noqa: F401
from test_reply_all import Store as ReplyStore, _cfg as _unused  # noqa: F401
from test_reply_send_path import (STATS, FakeClaude, FakeGmail, _cfg, people,  # noqa: F401
                                  reply, terms_dir)

CFG = SimpleNamespace(max_body_chars=45000, dry_run=False, send_automatically=True,
                      max_tokens=1000, heartbeat_journal_entries=5)


def _tool(name, **inp):
    return SimpleNamespace(type="tool_use", name=name, input=inp)


def _text(t):
    return SimpleNamespace(type="text", text=t)


class Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.seen = []
        self.messages = self

    def create(self, **kw):
        self.seen.append(kw)
        return SimpleNamespace(content=self.responses.pop(0), usage=None)


class Mail:
    """Sends are recorded; reads go to the world's fake Gmail."""

    def __init__(self, g):
        self.g, self.sent = g, []

    def users(self):
        return self

    def messages(self):
        return SimpleNamespace(get=self.g._get, send=self._send)

    def drafts(self):
        return SimpleNamespace(create=lambda userId, body: SimpleNamespace(
            execute=lambda **_: {"message": {"id": "d1", "threadId": "td"}}))

    def _send(self, userId, body):
        self.sent.append(body)
        n = len(self.sent)
        return SimpleNamespace(execute=lambda **_: {"id": f"s{n}", "threadId": f"tc{n}"})


@pytest.fixture
def setup(world, monkeypatch):
    g, store = world
    for tab, headers in ((consult.TAB, consult.HEADERS),
                         (audience.GRANTS_TAB, audience.GRANTS_HEADERS)):
        store.sheets.tabs[("R", tab)] = [list(headers)]
    monkeypatch.setattr(consult, "model_for", lambda cfg: "m")
    monkeypatch.setattr(terms_gate, "may_send_unsolicited", lambda a, p=None: True)
    return g, store, Mail(g)


# --------------------------------------------------------------------------
# candidates and the check
# --------------------------------------------------------------------------

def test_candidates_are_what_the_recipients_may_not_see(setup):
    _, store, _ = setup
    ids = {r["gmail_msg_id"] for r in consult.candidates(store, {ANN})}
    assert "b-private" in ids
    assert not ids & {"b-shared", "a-own", "b-later", "r-private"}


def test_no_private_items_means_no_call():
    assert consult.check(Client(), CFG, "hi", {ANN}, []) == (consult.PASS,
                                                          "no private items in view")


def test_revise_rewrites_once_and_checks_again(setup):
    _, store, _ = setup
    items = consult.candidates(store, {ANN})
    client = Client([_tool("verdict", verdict="revise", reason="'not Wednesday' hints")],
                    [_text("How about Thursday?\n— Indi")],
                    [_tool("verdict", verdict="pass", reason="")])
    text, note = consult.vet(client, CFG, "Not Wednesday, Thursday?\n— Indi", {ANN}, items)
    assert text == "How about Thursday?\n— Indi" and note == "revised"
    # The rewrite saw only the draft and the reason, never the private items.
    assert "wednesday impossible" not in str(client.seen[1]["messages"])


def test_a_second_failure_blocks(setup):
    _, store, _ = setup
    items = consult.candidates(store, {ANN})
    client = Client([_tool("verdict", verdict="revise", reason="x")], [_text("still bad")],
                    [_tool("verdict", verdict="revise", reason="y")])
    assert consult.vet(client, CFG, "d", {ANN}, items)[0] is None


def test_a_check_that_cannot_run_is_reported_not_passed(setup):
    _, store, _ = setup

    class Broken:
        messages = SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(consult.CheckUnavailable):
        consult.check(Broken(), CFG, "d", {ANN}, consult.candidates(store, {ANN}))


def test_a_model_that_refuses_a_forced_tool_is_asked_instead(setup):
    _, store, _ = setup
    calls = []

    def create(**kw):
        calls.append(kw)
        if kw["tool_choice"]["type"] == "tool":
            raise RuntimeError("Error code: 400 - tool_choice: type \"tool\" and \"any\" "
                               "are not supported for this model.")
        return SimpleNamespace(content=[_tool("verdict", verdict="pass", reason="")],
                               usage=None)
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    verdict, _ = consult.check(client, CFG, "d", {ANN}, consult.candidates(store, {ANN}))
    assert verdict == consult.PASS
    assert [c["tool_choice"]["type"] for c in calls] == ["tool", "auto"]
    assert "calling the verdict tool" in calls[1]["system"]


def test_an_unrelated_api_error_is_not_retried_that_way(setup):
    _, store, _ = setup
    calls = []

    def create(**kw):
        calls.append(kw)
        raise RuntimeError("Error code: 400 - usage limits reached")
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    with pytest.raises(consult.CheckUnavailable):
        consult.check(client, CFG, "d", {ANN}, consult.candidates(store, {ANN}))
    assert len(calls) == 1


# --------------------------------------------------------------------------
# the broker and the request
# --------------------------------------------------------------------------

def test_the_broker_asks_the_owner_and_records_it(setup):
    g, store, mail = setup
    client = Client(
        [_tool("consultations", requests=[
            {"item_id": "b-private", "question": "May I tell Ann you can't do Wednesday?"},
            {"item_id": "made-up", "question": "?"}])],
        [_tool("verdict", verdict="pass", reason="")])
    msg = {"gmail_msg_id": "a-own", "from_addr": ANN, "subject": "plans", "body": "when?"}
    out = consult.after_reply(client, CFG, mail, store, None, msg, {ANN})
    assert len(out) == 1 and out[0].startswith(f"asked {BOB}")
    [row] = consult.open_rows(store)
    assert row["owner"] == BOB and row["item_id"] == "b-private" and row["to"] == ANN
    assert b"[consult c-" in __import__("base64").urlsafe_b64decode(mail.sent[0]["raw"])
    # The same question is not asked twice.
    again = consult.request(client, CFG, mail, store, None,
                            {"gmail_msg_id": "b-private", "owner": BOB}, "q", {ANN}, "x")
    assert "already asked" in again


def test_owners_who_cannot_be_written_to_are_not_offered(setup, monkeypatch):
    _, store, _ = setup
    monkeypatch.setattr(terms_gate, "may_send_unsolicited", lambda a, p=None: False)
    client = Client()
    msg = {"from_addr": ANN, "subject": "s", "body": "b"}
    assert consult.broker(client, CFG, msg, {ANN}, consult.candidates(store, {ANN}), None) == []
    assert client.seen == []


# --------------------------------------------------------------------------
# the answer
# --------------------------------------------------------------------------

def _open(store, created=None):
    store._append(store.responses_id, consult.TAB, consult.HEADERS, {
        "consult_id": "c-1", "owner": BOB, "item_id": "b-private", "to": ANN,
        "asker_msg_id": "a-own", "thread_id": "tc1", "request_id": "s1",
        "created_at": (created or datetime.datetime.now(datetime.timezone.utc)).isoformat(),
        "status": consult.OPEN, "answered_at": "", "answer_id": "", "asked_again": "",
        "followup": "", "note": ""})
    return consult.open_rows(store)[0]


def _answer(body="Yes, that's fine.", frm=BOB):
    return {"gmail_msg_id": "ans1", "thread_id": "tc1", "from_addr": frm,
            "subject": "Re: consult", "body": body, "message_id": "<ans1@x>",
            "references": ""}


def test_yes_grants_and_follows_up(setup, monkeypatch):
    g, store, mail = setup
    monkeypatch.setattr(gmail_io, "sender_is_authenticated", lambda m: (True, "dmarc=pass"))
    row = _open(store)
    assert "b-private" in {r["gmail_msg_id"] for r in consult.candidates(store, {ANN})}
    client = Client([_text("YES")], [_text("Bob can't make Wednesday, so Thursday?\n— Indi")],
                    [_tool("verdict", verdict="pass", reason="")])
    out = consult.handle_answer(client, CFG, mail, store, None, _answer(), row, {INDI})
    assert "granted" in out and "follow-up sent" in out
    grants = store.grants().active()
    assert [(x.owner, x.to, x.scope, x.evidence) for x in grants] == [
        (BOB, ANN, "b-private", "ans1")]
    # Ann may see it now, and the follow-up was written from that.
    assert "b-private" not in {r["gmail_msg_id"] for r in consult.candidates(store, {ANN})}
    assert "wednesday impossible" in str(client.seen[1]["messages"])
    assert row["status"] == consult.GRANTED


def test_no_is_recorded_and_widens_nothing(setup, monkeypatch):
    _, store, mail = setup
    monkeypatch.setattr(gmail_io, "sender_is_authenticated", lambda m: (True, ""))
    row = _open(store)
    consult.handle_answer(Client([_text("NO")]), CFG, mail, store, None,
                          _answer("No thanks"), row, {INDI})
    assert row["status"] == consult.REFUSED and store.grants().active() == []


def test_unclear_asks_once_then_counts_as_no(setup, monkeypatch):
    _, store, mail = setup
    monkeypatch.setattr(gmail_io, "sender_is_authenticated", lambda m: (True, ""))
    row = _open(store)
    consult.handle_answer(Client([_text("UNCLEAR")]), CFG, mail, store, None,
                          _answer("Why?"), row, {INDI})
    assert row["status"] == consult.OPEN and len(mail.sent) == 1
    consult.handle_answer(Client([_text("UNCLEAR")]), CFG, mail, store, None,
                          _answer("Hmm"), row, {INDI})
    assert row["status"] == consult.REFUSED and store.grants().active() == []


def test_a_forged_yes_is_ignored(setup, monkeypatch):                     # scenario 5
    _, store, mail = setup
    monkeypatch.setattr(gmail_io, "sender_is_authenticated", lambda m: (False, "dmarc=fail"))
    row = _open(store)
    out = consult.handle_answer(Client(), CFG, mail, store, None, _answer(), row, {INDI})
    assert "failed authentication" in out and row["status"] == consult.OPEN
    assert store.grants().active() == []


def test_a_yes_from_someone_else_is_ignored(setup, monkeypatch):
    _, store, mail = setup
    monkeypatch.setattr(gmail_io, "sender_is_authenticated", lambda m: (True, ""))
    row = _open(store)
    out = consult.handle_answer(Client(), CFG, mail, store, None, _answer(frm=CAROL),
                                row, {INDI})
    assert "not from its owner" in out and store.grants().active() == []


def test_silence_expires_after_72_hours(setup):                           # scenario 6
    _, store, _ = setup
    _open(store, created=datetime.datetime.now(datetime.timezone.utc)
          - datetime.timedelta(hours=73))
    assert consult.expire(store) == 1 and consult.open_rows(store) == []


# --------------------------------------------------------------------------
# in process_message
# --------------------------------------------------------------------------

def _raw(body="lunch?"):
    import base64
    return {"id": "m1", "threadId": "t1", "internalDate": "1700000000000",
            "payload": {"mimeType": "text/plain", "headers": [
                {"name": "From", "value": ANN}, {"name": "To", "value": INDI},
                {"name": "Subject", "value": "lunch"},
                {"name": "Message-ID", "value": "<m1@x>"}],
                "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}}


def _go(gmail, cfg, store, people):
    return check_and_reply.process_message(gmail, None, FakeClaude(), cfg, store, _raw(),
                                           {INDI}, "system prompt", people)


def test_a_blocked_draft_is_not_sent(tmp_path, people, reply, monkeypatch):
    monkeypatch.setattr(consult, "enabled", lambda cfg=None: True)
    monkeypatch.setattr(consult, "match", lambda store, msg: None)
    monkeypatch.setattr(consult, "candidates", lambda store, r, limit=10: [{"x": 1}])
    monkeypatch.setattr(consult, "vet", lambda *a: (None, "leaks Bob's news"))
    gmail, store = FakeGmail(), ReplyStore()
    out = _go(gmail, _cfg(tmp_path), store, people)
    assert "blocked by the pre-send check" in out
    assert gmail.sent == [] and gmail.read == ["m1"]


def test_a_consultation_answer_is_not_replied_to(tmp_path, people, reply, monkeypatch):
    monkeypatch.setattr(consult, "enabled", lambda cfg=None: True)
    monkeypatch.setattr(consult, "match", lambda store, msg: {"consult_id": "c-1"})
    monkeypatch.setattr(consult, "handle_answer", lambda *a: "consultation c-1: granted")
    monkeypatch.setattr(claude_api, "generate_reply",
                        lambda *a, **k: pytest.fail("answered as mail"))
    gmail, store = FakeGmail(), ReplyStore()
    assert _go(gmail, _cfg(tmp_path), store, people) == "consultation c-1: granted"
    assert gmail.sent == [] and gmail.read == ["m1"]


def test_a_check_that_cannot_run_is_retried_then_given_up(tmp_path, people, reply, monkeypatch):
    monkeypatch.setattr(consult, "enabled", lambda cfg=None: True)
    monkeypatch.setattr(consult, "match", lambda store, msg: None)
    monkeypatch.setattr(consult, "candidates", lambda store, r, limit=10: [{"x": 1}])

    def unavailable(*a):
        raise consult.CheckUnavailable("400 - something")
    monkeypatch.setattr(consult, "vet", unavailable)
    gmail, store = FakeGmail(), ReplyStore()
    cfg = _cfg(tmp_path, base_dir=tmp_path)
    for attempt in (1, 2):
        out = _go(gmail, cfg, store, people)
        assert f"attempt {attempt} of 3" in out
        assert gmail.sent == [] and gmail.read == []          # left unread to retry
    out = _go(gmail, cfg, store, people)
    assert "after 3 attempts" in out and gmail.read == ["m1"]
