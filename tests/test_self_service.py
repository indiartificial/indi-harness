"""
tests/test_self_service.py

A correspondent withdrawing their own sharing permissions and joining or
leaving the newsletter by email (self_service.py). Every tool acts on the
sender and nobody else, and none is offered on a reply to several people.

This directory is protected. Indi can read it but not edit it.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audience
import claude_api
import newsletter
import self_service as ss
from test_drive_tables import CFG, _records_store

ANN, BOB, CAROL = "ann@example.com", "bob@example.com", "carol@example.com"


def _store():
    store, _ = _records_store()
    book = audience.GrantBook(store)
    book.add(BOB, ANN)
    book.add(BOB, CAROL)
    book.add(ANN, BOB)
    return store


def _active(store, owner):
    return [g for g in audience.GrantBook(store).all() if g.owner == owner and g.active]


def test_a_sender_sees_only_their_own_permissions():
    store = _store()
    text, err = ss.handle(CFG, store, BOB, ss.LIST_PERMISSIONS_TOOL, {})
    assert not err and ANN in text and CAROL in text and "all of your mail" in text
    assert text.count("active") == 2
    text, _ = ss.handle(CFG, store, CAROL, ss.LIST_PERMISSIONS_TOOL, {})
    assert "has not given any" in text


def test_withdrawing_by_person_by_id_and_all():
    store = _store()
    text, err = ss.handle(CFG, store, BOB, ss.WITHDRAW_PERMISSION_TOOL, {"which": ANN})
    assert not err and "can't be recalled" in text
    assert [g.to for g in _active(store, BOB)] == [CAROL]
    gid = _active(store, BOB)[0].grant_id
    assert not ss.handle(CFG, store, BOB, ss.WITHDRAW_PERMISSION_TOOL, {"which": gid})[1]
    assert _active(store, BOB) == []
    # Withdrawn, not deleted: still listed.
    assert "withdrawn" in ss.handle(CFG, store, BOB, ss.LIST_PERMISSIONS_TOOL, {})[0]


def test_nobody_can_withdraw_someone_elses():
    store = _store()
    text, err = ss.handle(CFG, store, ANN, ss.WITHDRAW_PERMISSION_TOOL, {"which": "all"})
    assert not err and len(_active(store, BOB)) == 2 and _active(store, ANN) == []
    gid = _active(store, BOB)[0].grant_id
    assert ss.handle(CFG, store, ANN, ss.WITHDRAW_PERMISSION_TOOL, {"which": gid})[1]
    assert len(_active(store, BOB)) == 2


def test_joining_and_leaving_the_newsletter():
    store, _ = _records_store()
    text, err = ss.handle(CFG, store, ANN, ss.SUBSCRIBE_TOOL, {})
    assert not err and ANN in newsletter.list_subscribers(store, CFG)
    text, err = ss.handle(CFG, store, ANN, ss.UNSUBSCRIBE_TOOL, {})
    assert not err and ANN not in newsletter.list_subscribers(store, CFG)


def test_offered_only_on_a_reply_to_the_sender_alone():
    cfg = SimpleNamespace(enable_web_search=False, enable_web_fetch=False,
                          enable_code_execution=False, enable_history_search=False,
                          enable_outbound=False, betas=lambda **kw: [])
    def names(shared):
        tools, _ = claude_api.build_tools(cfg, False, True, reminder_sender=ANN,
                                          shared_reply=shared)
        return {t.get("name") for t in tools}
    assert ss.TOOL_NAMES <= names(False)
    assert not ss.TOOL_NAMES & names(True)


def test_web_fetch_can_be_offered_to_everyone(monkeypatch):
    cfg = SimpleNamespace(enable_web_search=False, enable_web_fetch=True,
                          enable_code_execution=True, enable_history_search=False,
                          enable_outbound=False, betas=lambda **kw: [],
                          web_fetch_tool_type="web_fetch_x", max_fetches_per_reply=3,
                          web_fetch_allowed_domains=(), code_execution_tool_type="code_x")
    names = lambda trusted: {t["name"] for t in claude_api.build_tools(cfg, trusted, False)[0]}
    monkeypatch.setenv("INDI_WEB_FETCH_FOR", "trusted")
    assert "web_fetch" not in names(False) and "web_fetch" in names(True)
    monkeypatch.setenv("INDI_WEB_FETCH_FOR", "everyone")
    assert "web_fetch" in names(False)
    assert "code_execution" not in names(False)          # still trusted-only
