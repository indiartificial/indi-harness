"""
tests/test_address_links.py

Linking a second address: the code goes only to the invited address, only
that address, authenticated, with the right code and in time, links
anything; a link merges the notes, grants mutual sight of mail, carries
the terms acceptance over, and points the memory file at the main address.
"""

import datetime
from types import SimpleNamespace

import pytest

import address_links
import costs
import memory_file
import memory_rights
from test_memory_rights import FakeDrive

MAIN = "ann@home.example"
WORK = "ann@work.example"


class FakeGmail:
    def __init__(self):
        self.sent = []

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, userId, body):
        self.sent.append(body)
        return SimpleNamespace(execute=lambda: {"threadId": f"t{len(self.sent)}"})


class People:
    def __init__(self, accepted=(MAIN,), declined=()):
        self.records = {}
        for a in accepted:
            self.records[a] = SimpleNamespace(status="accepted", has_accepted=True,
                                              terms_version_accepted="0.3")
        for a in declined:
            self.records[a] = SimpleNamespace(status="declined", has_accepted=False,
                                              terms_version_accepted="")
        self.accepted_now = []

    def get(self, addr):
        return self.records.get(addr, SimpleNamespace(status="unknown", has_accepted=False,
                                                      terms_version_accepted=""))

    def record_acceptance(self, addr, version):
        self.accepted_now.append((addr, version))


class Book:
    grants = []

    def __init__(self, store):
        pass

    def add(self, owner, to, evidence="", note=""):
        g = SimpleNamespace(grant_id=f"g{len(Book.grants) + 1}", owner=owner, to=to,
                            active=True)
        Book.grants.append(g)
        return g

    def revoke(self, gid):
        for g in Book.grants:
            if g.grant_id == gid and g.active:
                g.active = False
                return True
        return False


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "address_links.json"
    monkeypatch.setattr(address_links, "FILE", path)
    address_links.reset_cache()
    monkeypatch.setattr(address_links.gmail_io, "sender_is_authenticated",
                        lambda msg: (True, ""))
    fixed = []
    monkeypatch.setattr(address_links.terms_gate, "_send_fixed",
                        lambda cfg, gmail, msg, body: fixed.append(body))
    import audience
    Book.grants = []
    monkeypatch.setattr(audience, "GrantBook", Book)
    import approvals
    monkeypatch.setattr(approvals, "denied", lambda addr: False)
    monkeypatch.setattr(costs, "record", lambda *a, **k: None)
    yield SimpleNamespace(path=path, fixed=fixed)
    address_links.reset_cache()


def _cfg(**over):
    values = dict(dry_run=False)
    values.update(over)
    return SimpleNamespace(**values)


def _code(gmail):
    import base64
    import re
    from email import message_from_bytes
    message = message_from_bytes(base64.urlsafe_b64decode(gmail.sent[-1]["raw"]))
    parts = message.walk() if message.is_multipart() else [message]
    text = "".join((p.get_payload(decode=True) or b"").decode("utf-8", "replace")
                   for p in parts if p.get_content_type() == "text/plain")
    return re.search(r"\b(\d{6})\b", text).group(1)


def _reply(body, sender=WORK, thread="t1"):
    return {"from_addr": sender, "body": body, "thread_id": thread, "subject": "Re",
            "message_id": "<r>"}


# --------------------------------------------------------------------------
# ASKING
# --------------------------------------------------------------------------

def test_request_sends_a_code_to_the_other_address_only(env):
    gmail = FakeGmail()
    text, err = address_links.request_link(_cfg(), gmail, People(), MAIN, WORK)
    assert not err and WORK in text and len(gmail.sent) == 1
    assert len(_code(gmail)) == 6
    assert address_links.canonical(WORK) == WORK  # nothing linked yet


@pytest.mark.parametrize("other, people, selves, why", [
    ("not-an-address", People(), (), "doesn't look"),
    (MAIN, People(), (), "can't be linked"),
    ("indi@example.com", People(), ("indi@example.com",), "can't be linked"),
    (WORK, People(declined=(WORK,)), (), "asked not to hear"),
])
def test_refused_addresses(env, other, people, selves, why):
    gmail = FakeGmail()
    text, err = address_links.request_link(_cfg(), gmail, people, MAIN, other, selves)
    assert err and why in text and gmail.sent == []


def test_operator_cannot_be_linked(env):
    import terms_gate
    text, err = address_links.request_link(_cfg(), FakeGmail(), People(), MAIN,
                                           terms_gate.OPERATOR_ADDRESS)
    assert err


def test_one_code_per_address_at_a_time(env):
    gmail = FakeGmail()
    address_links.request_link(_cfg(), gmail, People(), MAIN, WORK)
    text, err = address_links.request_link(_cfg(), gmail, People(), MAIN, WORK)
    assert err and len(gmail.sent) == 1


# --------------------------------------------------------------------------
# CONFIRMING
# --------------------------------------------------------------------------

def _start(env):
    gmail = FakeGmail()
    address_links.request_link(_cfg(), gmail, People(), MAIN, WORK)
    return gmail, _code(gmail)


def test_only_the_invited_address_with_the_right_code_links(env, monkeypatch):
    gmail, code = _start(env)
    entry = address_links.match(_reply(code))
    out = address_links.confirm(_cfg(), gmail, None, None, People(),
                                _reply(code, sender="thief@example.com"), entry)
    assert "ignored" in out and address_links.canonical(WORK) == WORK

    monkeypatch.setattr(address_links.gmail_io, "sender_is_authenticated",
                        lambda msg: (False, "dkim=fail"))
    out = address_links.confirm(_cfg(), gmail, None, None, People(), _reply(code), entry)
    assert "authentication" in out and address_links.canonical(WORK) == WORK


def test_three_wrong_codes_cancel(env):
    gmail, code = _start(env)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(3):
        entry = address_links.match(_reply(wrong))
        out = address_links.confirm(_cfg(), gmail, None, None, People(), _reply(wrong), entry)
    assert "0 tries left" in out
    assert address_links.match(_reply(code)) is None


def test_an_expired_code_does_nothing(env, monkeypatch):
    gmail, code = _start(env)
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=73)
    monkeypatch.setattr(address_links.clock, "now", lambda *a: later)
    entry = address_links.match(_reply(code))
    out = address_links.confirm(_cfg(), gmail, None, None, People(), _reply(code), entry)
    assert "expired" in out and address_links.canonical(WORK) == WORK


def test_a_good_code_links_grants_accepts_and_merges(env, monkeypatch):
    drive = FakeDrive()
    monkeypatch.setattr(memory_file, "read_file",
                        lambda d, fid: drive.files_by_id.get(fid, {}).get("text", "") if fid else "")

    def write(d, name, text, existing_file_id=None):
        if existing_file_id:
            drive.files_by_id[existing_file_id]["text"] = text
            return existing_file_id
        return drive.add(name, text)
    monkeypatch.setattr(memory_file, "write_file", write)
    drive.add(memory_file.memory_file_name(MAIN),
              memory_file.serialise_memory(MAIN, {"m1"},
                                           "## Shareable\n- Goes by Ann.\n\n## Long-term\n- Likes tea."))
    drive.add(memory_file.memory_file_name(WORK),
              memory_file.serialise_memory(WORK, {"w1"},
                                           "## Long-term\n- Works in accounts.\n\n## Current\n- Audit due."))
    drive.add(memory_file.timestamped_backup_name(WORK), "old work notes")

    gmail, code = _start(env)
    people = People()
    entry = address_links.match(_reply(f"here it is: {code}"))
    out = address_links.confirm(_cfg(), gmail, drive, object(), people,
                                _reply(f"here it is: {code}"), entry)
    assert out.startswith(f"linked {WORK} to {MAIN}")
    assert address_links.canonical(WORK) == MAIN
    assert memory_file.memory_file_name(WORK) == memory_file.memory_file_name(MAIN)
    assert people.accepted_now == [(WORK, "0.3")]
    assert {(g.owner, g.to) for g in Book.grants} == {(WORK, MAIN), (MAIN, WORK)}

    names = drive.names()
    assert not any("work.example" in n for n in names), names
    main = next(f["text"] for f in drive.files_by_id.values()
                if f["name"] == memory_file.memory_file_name(MAIN))
    mark, body = memory_file.parse_memory(main)
    assert mark.ids == {"m1", "w1"}
    assert "Likes tea." in body and "Works in accounts." in body and "Audit due." in body
    assert body.index("## Shareable") < body.index("## Long-term") < body.index("## Current")
    assert "Not merged" in env.fixed[-1]

    # Unlinking withdraws the grants; the notes stay with the main address.
    text, err = address_links.unlink(_cfg(), object(), MAIN, WORK)
    assert not err and address_links.canonical(WORK) == WORK
    assert not any(g.active for g in Book.grants)


def test_listing(env):
    gmail, code = _start(env)
    assert "Waiting for the code: " + WORK in address_links.list_links(MAIN)
    address_links.confirm(_cfg(), gmail, None, None, People(), _reply(code),
                          address_links.match(_reply(code)))
    assert address_links.list_links(MAIN) == "Linked: " + WORK
    # Asked from the linked address, it answers for the person.
    assert address_links.list_links(WORK) == "Linked: " + WORK


def test_a_main_address_with_links_cannot_become_an_alias(env):
    gmail, code = _start(env)
    address_links.confirm(_cfg(), gmail, None, None, People(), _reply(code),
                          address_links.match(_reply(code)))
    text, err = address_links.request_link(_cfg(), FakeGmail(), People(), "x@example.com",
                                           MAIN)
    assert err


def test_merge_bodies_keeps_every_line_once():
    a = "## Shareable\n- A\n\n## Long-term\n- B"
    b = "Old unsectioned note.\n## Long-term\n- B\n- C"
    merged = address_links.merge_bodies(a, b)
    assert merged.count("- B") == 1
    for line in ("- A", "- C", "Old unsectioned note."):
        assert line in merged


def test_address_links_is_protected():
    import self_code
    assert "address_links.py" in self_code.PROTECTED_FILES


def test_tools_offered_only_with_a_runner():
    import claude_api
    cfg = SimpleNamespace(enable_web_search=False, enable_web_fetch=False,
                          enable_code_execution=False, enable_history_search=False,
                          enable_outbound=False, betas=lambda **k: [])
    runner = address_links.Runner(_cfg(), None, None, None, MAIN)
    names = lambda tools: {t["name"] for t in tools}
    assert not names(claude_api.build_tools(cfg, False, False)[0]) & address_links.TOOL_NAMES
    assert address_links.TOOL_NAMES <= names(
        claude_api.build_tools(cfg, False, False, address_links_runner=runner)[0])
    assert not names(claude_api.build_tools(cfg, False, False, address_links_runner=runner,
                                            shared_reply=True)[0]) & address_links.TOOL_NAMES
