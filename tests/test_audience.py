"""
tests/test_audience.py

Phase 2 groundwork: the audience rule (audience.py), grants, the
audience-scoped history search in GmailStore, and grants.py.

The rule under test: Indi may show a message to a set of people only if
every one of them was on it (From, To, Cc — not Bcc) or its owner granted
it to them. Scenario numbers refer to the design doc's test scenarios.

This directory is protected. Indi can read it but not edit it.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import audience
import drive_tables
import grants as grants_cli
import sheets_queue
from test_drive_tables import DictIO
from test_gmail_store import (ALICE as ANN, BOB, HOUR, INDI, NOW, FakeGmail,
                              _gstore, _reply)

CAROL = "carol@example.com"
SELVES = {INDI}


# --------------------------------------------------------------------------
# the rule itself
# --------------------------------------------------------------------------

def test_audience_is_from_to_and_cc_less_indi():
    got = audience.incoming_audience(f"Bob <{BOB}>", f"{INDI}, {ANN}", CAROL, SELVES)
    assert got == {BOB, ANN, CAROL}


def test_bcc_is_private_to_the_sender():                           # scenario 8
    assert audience.incoming_audience(BOB, ANN, "", SELVES) == {BOB}


def test_unknown_own_address_means_the_narrowest_answer():
    assert audience.incoming_audience(BOB, f"{INDI}, {ANN}", "", set()) == {BOB}


def test_a_from_naming_two_people_belongs_to_nobody():
    assert audience.incoming_audience(f"{BOB}, {ANN}", INDI, "", SELVES) == frozenset()


def test_indis_own_mail_is_seen_by_its_to_and_cc():
    meta = {"from": INDI, "to": BOB, "cc": ""}
    assert audience.audience_of(meta, SELVES) == {BOB}


def test_cleared_needs_every_recipient():
    meta = {"from": BOB, "to": f"{INDI}, {ANN}", "cc": ""}
    assert audience.cleared("m1", meta, {ANN}, SELVES, None)
    assert audience.cleared("m1", meta, {ANN, BOB}, SELVES, None)
    assert not audience.cleared("m1", meta, {ANN, CAROL}, SELVES, None)
    assert not audience.cleared("m1", meta, set(), SELVES, None)


# --------------------------------------------------------------------------
# grants
# --------------------------------------------------------------------------

def _book():
    store = SimpleNamespace(sheets=drive_tables.DriveSheets(DictIO()),
                            responses_id=drive_tables.RECORDS_ID)
    sheet = sheets_queue.SheetStore(store.sheets, None, SimpleNamespace(max_body_chars=45000))
    sheet.responses_id = drive_tables.RECORDS_ID
    return audience.GrantBook(sheet), sheet


def test_grants_widen_one_owners_mail_until_withdrawn():           # scenario 4
    book, sheet = _book()
    private = {"from": BOB, "to": INDI, "cc": ""}
    assert not audience.cleared("m1", private, {ANN}, SELVES, book)
    g = book.add(BOB, ANN, note="yes by phone")
    assert audience.cleared("m1", private, {ANN}, SELVES, book)
    assert audience.cleared("m2", private, {ANN}, SELVES, audience.GrantBook(sheet))
    assert not audience.cleared("m1", private, {ANN, CAROL}, SELVES, book)
    assert book.revoke(g.grant_id)
    assert not audience.cleared("m1", private, {ANN}, SELVES, audience.GrantBook(sheet))
    # Withdrawn, not deleted.
    assert [x.revoked_at != "" for x in audience.GrantBook(sheet).all()] == [True]


def test_a_grant_can_cover_one_message_only():
    book, _ = _book()
    book.add(BOB, ANN, scope="m1")
    private = {"from": BOB, "to": INDI, "cc": ""}
    assert audience.cleared("m1", private, {ANN}, SELVES, book)
    assert not audience.cleared("m2", private, {ANN}, SELVES, book)


def test_indis_own_messages_are_not_widened_by_a_grant():
    book, _ = _book()
    book.add(INDI, ANN)
    assert not audience.cleared("r1", {"from": INDI, "to": BOB, "cc": ""}, {ANN}, SELVES, book)


def test_the_grants_tool_lists_adds_and_withdraws():
    book, _ = _book()
    lines = []
    grants_cli.run(book, grants_cli.parse_args(["add", "--owner", BOB, "--to", ANN]), lines.append)
    gid = lines[-1].split()[1].rstrip(":")
    grants_cli.run(book, grants_cli.parse_args(["list"]), lines.append)
    assert "active" in lines[-1]
    assert grants_cli.run(book, grants_cli.parse_args(["revoke", gid]), lines.append) == 0
    assert grants_cli.run(book, grants_cli.parse_args(["revoke", gid]), lines.append) == 1


# --------------------------------------------------------------------------
# history search under the rule
# --------------------------------------------------------------------------

def _mail(g, store, mid, frm, to, cc="", body="", when=NOW, thread=None):
    g.add(mid, frm, to, subject="plans", body=body, when=when, thread=thread,
          labels=("INBOX",))
    if cc:
        g.msgs[mid]["payload"]["headers"].append({"name": "Cc", "value": cc})
    store.set_status(store.append_queue_row({
        "gmail_msg_id": mid, "thread_id": g.msgs[mid]["threadId"], "from_raw": frm,
        "from_addr": audience.single(frm), "subject": "plans", "body": body,
        "internal_date": 0}), sheets_queue.STATUS_REPLIED)


@pytest.fixture
def world():
    g = FakeGmail()
    store = _gstore(g)
    store.selves = set(SELVES)
    store.sheets.tabs[("R", audience.GRANTS_TAB)] = [list(audience.GRANTS_HEADERS)]
    # Bob, privately: can't make Wednesday.                       (scenario 1)
    _mail(g, store, "b-private", BOB, INDI, body="plans wednesday impossible",
          when=NOW - 5 * HOUR)
    _reply(store, g, "b-private", "r-private", "plans noted, wednesday is out",
           when=NOW - 4 * HOUR)
    # Bob, with Ann on the Cc: budget is tight.                    (scenario 2)
    _mail(g, store, "b-shared", BOB, INDI, cc=ANN, body="plans budget tight",
          when=NOW - 3 * HOUR, thread="t-shared")
    # Ann's own message.
    _mail(g, store, "a-own", ANN, INDI, body="plans thursday maybe", when=NOW - 2 * HOUR)
    # Carol added to the shared thread later.                      (scenario 7)
    _mail(g, store, "b-later", BOB, INDI, cc=f"{ANN}, {CAROL}", body="plans carol joins",
          when=NOW - HOUR, thread="t-shared")
    return g, store


def _ids(results):
    return {r["gmail_msg_id"] for r in results}


def test_ann_sees_what_she_was_party_to_and_nothing_else(world):
    _, store = world
    got = store.search("plans", limit=10, cleared_for={ANN})
    assert _ids(got) == {"b-shared", "a-own", "b-later"}


def test_a_reply_to_ann_and_carol_sees_only_what_both_have_seen(world):
    _, store = world
    got = store.search("plans", limit=10, cleared_for={ANN, CAROL})
    assert _ids(got) == {"b-later"}


def test_indis_private_reply_to_bob_is_not_shown_to_ann(world):
    g, store = world
    store.grants().add(BOB, ANN)
    store._grants = None
    got = {r["gmail_msg_id"]: r for r in store.search("plans", limit=10, cleared_for={ANN})}
    # The grant opens Bob's message to Ann, but not Indi's private reply to him.
    assert "b-private" in got and got["b-private"]["responses"] == []


def test_bob_sees_his_own_private_exchange(world):
    _, store = world
    got = {r["gmail_msg_id"]: r for r in store.search("plans", limit=10, cleared_for={BOB})}
    assert got["b-private"]["responses"] == ["plans noted, wednesday is out"]


def test_no_recipients_means_nothing(world):
    _, store = world
    assert store.search("plans", limit=10, cleared_for=set()) == []


def test_a_store_without_the_rule_is_never_widened():
    import claude_api
    calls = []

    class OldStore:
        def search(self, **kw):
            calls.append(kw)
            return []
    claude_api._run_history_search(OldStore(), SimpleNamespace(max_history_search_results=5),
                                   {"query": "x"}, ANN, cleared_for={ANN})
    assert calls == [{"query": "x", "sender": "", "limit": 5, "restrict_to_sender": ANN}]
