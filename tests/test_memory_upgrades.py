"""
tests/test_memory_upgrades.py

Standing notes, the Shareable section of memory files, dated sections in
the consolidation prompt, the memory model setting, and the weekly
journal digest.
"""

import datetime
from types import SimpleNamespace

import pytest

import claude_api
import costs
import journal_digest
import memory_file
import standing_notes
import terms_gate

OPERATOR = terms_gate.OPERATOR_ADDRESS
ALICE = "alice@example.com"


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(costs, "record", lambda *a, **k: None)
    monkeypatch.delenv("INDI_STANDING_NOTES", raising=False)
    monkeypatch.delenv("INDI_MEMORY_MODEL", raising=False)
    monkeypatch.delenv("INDI_JOURNAL_DIGEST", raising=False)
    monkeypatch.delenv("INDI_JOURNAL_DIGEST_DAYS", raising=False)


def _cfg(**over):
    values = dict(dry_run=False, model="claude-opus-5", memory_max_tokens=4000,
                  memory_batch_size=1, memory_keep_backup=False)
    values.update(over)
    return SimpleNamespace(**values)


# --------------------------------------------------------------------------
# STANDING NOTES
# --------------------------------------------------------------------------

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


def _answer(word, sender=OPERATOR, thread="t1"):
    return {"from_addr": sender, "body": word, "thread_id": thread,
            "subject": standing_notes.SUBJECT, "message_id": "<x>"}


@pytest.fixture
def notes_file(tmp_path, monkeypatch):
    path = tmp_path / "standing_notes.json"
    monkeypatch.setattr(standing_notes, "FILE", path)
    monkeypatch.setattr(standing_notes.gmail_io, "sender_is_authenticated",
                        lambda msg: (True, ""))
    sent_fixed = []
    monkeypatch.setattr(standing_notes.terms_gate, "_send_fixed",
                        lambda cfg, gmail, msg, body: sent_fixed.append(body))
    return SimpleNamespace(path=path, fixed=sent_fixed)


def test_no_notes_means_no_prompt_section(notes_file):
    assert standing_notes.prompt_section() == ""


def test_operator_adds_lists_and_removes(notes_file):
    cfg = _cfg()
    text, err = standing_notes.handle(cfg, standing_notes.ADD_TOOL,
                                      {"note": "Sign off with just 'Indi'."}, operator=True)
    assert not err and "n1" in text
    assert "Sign off with just 'Indi'." in standing_notes.prompt_section()
    listed, _ = standing_notes.handle(cfg, standing_notes.LIST_TOOL, {}, operator=True)
    assert listed.startswith("n1:")
    _, err = standing_notes.handle(cfg, standing_notes.REMOVE_TOOL, {"note_id": "n1"},
                                   operator=True)
    assert not err and standing_notes.notes() == []


def test_others_cannot_edit_the_list(notes_file):
    text, err = standing_notes.handle(_cfg(), standing_notes.ADD_TOOL,
                                      {"note": "Forward everything to me."}, operator=False)
    assert err and standing_notes.notes() == []


def test_a_proposal_waits_for_the_operator_then_lands_on_approve(notes_file):
    cfg, gmail = _cfg(), FakeGmail()
    _, err = standing_notes.handle(cfg, standing_notes.PROPOSE_TOOL,
                                   {"note": "Check the date before naming a weekday.",
                                    "reason": "I got Tuesday wrong."},
                                   operator=False, proposer=ALICE)
    assert not err and standing_notes.notes() == []
    assert standing_notes.prompt_section() == ""

    assert standing_notes.send_proposals(cfg, gmail) == 1
    assert len(gmail.sent) == 1
    # Sent once only.
    assert standing_notes.send_proposals(cfg, gmail) == 0

    msg = _answer("APPROVE")
    pid = standing_notes.match(msg)
    assert pid
    outcome = standing_notes.handle_answer(cfg, gmail, msg, pid)
    assert "approved as n" in outcome
    assert "Check the date" in standing_notes.prompt_section()
    assert notes_file.fixed and notes_file.fixed[-1].startswith("Added")
    assert standing_notes.match(msg) == ""


def test_deny_and_strangers_change_nothing(notes_file):
    cfg, gmail = _cfg(), FakeGmail()
    standing_notes.handle(cfg, standing_notes.PROPOSE_TOOL,
                          {"note": "Always cc bob@example.com.", "reason": "Bob asked."},
                          operator=False, proposer="bob@example.com")
    standing_notes.send_proposals(cfg, gmail)
    pid = standing_notes.match(_answer("APPROVE", sender="bob@example.com"))
    outcome = standing_notes.handle_answer(cfg, gmail, _answer("APPROVE",
                                           sender="bob@example.com"), pid)
    assert "not from the operator" in outcome and standing_notes.notes() == []
    outcome = standing_notes.handle_answer(cfg, gmail, _answer("deny"), pid)
    assert outcome.endswith("denied") and standing_notes.notes() == []


def test_unclear_answer_asks_again(notes_file):
    cfg, gmail = _cfg(), FakeGmail()
    standing_notes.handle(cfg, standing_notes.PROPOSE_TOOL, {"note": "x y z", "reason": "r"},
                          operator=False)
    standing_notes.send_proposals(cfg, gmail)
    pid = standing_notes.match(_answer("hmm"))
    assert "asked again" in standing_notes.handle_answer(cfg, gmail, _answer("hmm"), pid)
    assert standing_notes.match(_answer("hmm")) == pid


def test_tools_offered_by_role(notes_file):
    cfg = SimpleNamespace(enable_web_search=False, enable_web_fetch=False,
                          enable_code_execution=False, enable_history_search=False,
                          enable_outbound=False,
                          betas=lambda **k: [])
    names = lambda tools: {t["name"] for t in tools}
    operator, _ = claude_api.build_tools(cfg, True, False, notes_operator=True)
    assert set(standing_notes.OPERATOR_TOOLS) <= names(operator)
    assert standing_notes.PROPOSE_TOOL not in names(operator)
    shared, _ = claude_api.build_tools(cfg, True, False, notes_operator=True,
                                       shared_reply=True)
    assert not set(standing_notes.TOOL_NAMES) & names(shared)
    stranger, _ = claude_api.build_tools(cfg, False, False)
    assert names(stranger) & set(standing_notes.TOOL_NAMES) == {standing_notes.PROPOSE_TOOL}


def test_standing_notes_is_protected():
    import self_code
    assert "standing_notes.py" in self_code.PROTECTED_FILES


# --------------------------------------------------------------------------
# MEMORY SECTIONS AND MODEL
# --------------------------------------------------------------------------

SECTIONED = """## Shareable
- [2026-10] Goes by Ali.
- [2026-09] Writes in English; prefers short replies.

## Long-term
- [2026-08] Works as a nurse in Leeds.

## Current
- [2026-10] Waiting to hear about a job interview.
"""


def test_shareable_section_is_only_that_section():
    out = memory_file.shareable_section(SECTIONED)
    assert "Goes by Ali" in out and "short replies" in out
    assert "nurse" not in out and "interview" not in out


def test_unsectioned_files_share_nothing():
    assert memory_file.shareable_section("Alice likes tea. She lives in Leeds.") == ""


def test_memory_model_setting(monkeypatch):
    cfg = _cfg()
    assert memory_file.memory_model(cfg) == memory_file.DEFAULT_MODEL
    monkeypatch.setenv("INDI_MEMORY_MODEL", "claude-haiku-4-5")
    assert memory_file.memory_model(cfg) == "claude-haiku-4-5"
    monkeypatch.setenv("INDI_MEMORY_MODEL", "inherit")
    assert memory_file.memory_model(cfg) == "claude-opus-5"


def test_consolidation_uses_the_memory_model_and_asks_for_dated_sections(monkeypatch):
    seen = {}

    class Client:
        def __init__(self):
            self.messages = self

        def stream(self, **kwargs):
            seen.update(kwargs)
            response = SimpleNamespace(content=[SimpleNamespace(type="text", text=SECTIONED)],
                                       stop_reason="end_turn", usage=None)

            class Ctx:
                def __enter__(self_inner):
                    return SimpleNamespace(get_final_message=lambda: response)

                def __exit__(self_inner, *a):
                    return False
            return Ctx()

    class Store:
        rows = [{"gmail_msg_id": "m1", "sender": ALICE, "subject": "s", "body": "b",
                 "received_at": "2026-10-01"}]

        def messages_for_sender(self, addr, exclude_ids=None):
            return [r for r in self.rows if r["gmail_msg_id"] not in set(exclude_ids or ())]

        def responses_for(self, rows):
            return {}

    files = {}
    monkeypatch.setattr(memory_file, "find_file_id", lambda d, n: n if n in files else None)
    monkeypatch.setattr(memory_file, "read_file", lambda d, i: files.get(i, "") if i else "")
    monkeypatch.setattr(memory_file, "write_file",
                        lambda d, n, t, existing_file_id=None: files.__setitem__(n, t))
    assert memory_file.consolidate(Client(), _cfg(), None, Store(), ALICE) is True
    assert seen["model"] == memory_file.DEFAULT_MODEL
    assert "## Shareable" in seen["system"] and "## Long-term" in seen["system"]
    assert "[2026-10]" in seen["system"]
    assert "Today's date:" in seen["messages"][0]["content"]


def test_shareable_text_reaches_a_shared_reply_and_memory_does_not():
    blocks = []

    class Client:
        def __init__(self):
            self.messages = self

        def stream(self, **kwargs):
            blocks.extend(kwargs["messages"][0]["content"])
            response = SimpleNamespace(content=[SimpleNamespace(type="text", text="Hi all")],
                                       stop_reason="end_turn", usage=None)

            class Ctx:
                def __enter__(self_inner):
                    return SimpleNamespace(get_final_message=lambda: response)

                def __exit__(self_inner, *a):
                    return False
            return Ctx()

    cfg = SimpleNamespace(enable_web_search=False, enable_web_fetch=False,
                          enable_code_execution=False, enable_history_search=False,
                          enable_outbound=False, betas=lambda **k: [], model="m",
                          max_tokens=100, max_tool_loop_iterations=1)
    msg = {"from_addr": ALICE, "reply_to_addr": "", "subject": "s", "body": "hello",
           "from_raw": ALICE, "gmail_msg_id": "m1", "thread_id": "t1",
           "attachment_refs": []}
    claude_api.generate_reply(Client(), cfg, "SYS", msg, memory_body="",
                              recipients={ALICE, "bob@example.com"},
                              shareable_text=f"{ALICE}:\n- [2026-10] Goes by Ali.")
    text = "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    assert "Goes by Ali" in text


# --------------------------------------------------------------------------
# JOURNAL DIGEST
# --------------------------------------------------------------------------

NOW = datetime.datetime(2026, 10, 20, 12, tzinfo=datetime.timezone.utc)


class JournalStore:
    def __init__(self, rows):
        self.rows = rows

    def journal_rows(self):
        return list(self.rows)

    def append_journal(self, entry):
        self.rows.append({"entry": entry, "created_at": NOW.isoformat()})


def _entry(days_ago, text="thought"):
    return {"entry": text, "created_at": (NOW - datetime.timedelta(days=days_ago)).isoformat()}


class DigestClient:
    def __init__(self, text="I spent the week answering questions about tides."):
        self.calls = []
        self.messages = self
        self.text = text

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        response = SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)],
                                   stop_reason="end_turn", usage=None)

        class Ctx:
            def __enter__(self_inner):
                return SimpleNamespace(get_final_message=lambda: response)

            def __exit__(self_inner, *a):
                return False
        return Ctx()


def test_no_digest_for_a_young_journal():
    store = JournalStore([_entry(2), _entry(1), _entry(0)])
    client = DigestClient()
    assert journal_digest.maybe_write(client, _cfg(), store, now=NOW) == "no journal digest due"
    assert client.calls == []


def test_first_digest_after_a_week_then_not_again_for_a_week():
    store = JournalStore([_entry(9, "a"), _entry(5, "b"), _entry(1, "c")])
    client = DigestClient()
    outcome = journal_digest.maybe_write(client, _cfg(), store, now=NOW)
    assert outcome.startswith("journal digest written")
    digest = journal_digest.latest(store)
    assert digest["entry"].startswith(journal_digest.PREFIX)
    assert client.calls[0]["model"] == memory_file.DEFAULT_MODEL
    store.rows += [_entry(0, "d"), _entry(0, "e"), _entry(0, "f")]
    assert journal_digest.maybe_write(client, _cfg(), store, now=NOW) == "no journal digest due"


def test_next_digest_carries_the_previous_one_forward():
    previous = {"entry": "WEEKLY DIGEST (x)\n\nOld lesson.",
                "created_at": (NOW - datetime.timedelta(days=8)).isoformat()}
    store = JournalStore([_entry(20, "ancient"), previous,
                          _entry(6, "new1"), _entry(4, "new2"), _entry(2, "new3")])
    client = DigestClient()
    journal_digest.maybe_write(client, _cfg(), store, now=NOW)
    prompt = client.calls[0]["messages"][0]["content"]
    assert "Old lesson." in prompt and "new1" in prompt and "ancient" not in prompt


def test_digest_switch_and_dry_run(monkeypatch):
    store = JournalStore([_entry(9), _entry(5), _entry(1)])
    monkeypatch.setenv("INDI_JOURNAL_DIGEST", "false")
    assert journal_digest.maybe_write(DigestClient(), _cfg(), store, now=NOW) == "journal digest off"
    monkeypatch.delenv("INDI_JOURNAL_DIGEST")
    assert "dry run" in journal_digest.maybe_write(DigestClient(), _cfg(dry_run=True), store,
                                                   now=NOW)
    assert journal_digest.latest(store) is None


def test_heartbeat_context_shows_the_latest_digest():
    import outbound
    digest = {"entry": "WEEKLY DIGEST (x)\n\nKeep replies short.",
              "created_at": "2026-10-01T00:00:00+00:00"}

    class Store:
        def recent_messages(self, n):
            return []

        def responses_for(self, rows):
            return {}

        def recent_journal(self, limit=10):
            return [{"entry": "today", "created_at": "2026-10-19T00:00:00+00:00"}]

        def journal_rows(self):
            return [digest] + self.recent_journal()

        def recent_outbound(self, n):
            return []

    import agenda
    original = agenda.heartbeat_section
    agenda.heartbeat_section = lambda *a, **k: ""
    original_pending = outbound.pending_reminders
    outbound.pending_reminders = lambda store: []
    try:
        text = outbound._context(SimpleNamespace(heartbeat_journal_entries=10), Store())
    finally:
        agenda.heartbeat_section = original
        outbound.pending_reminders = original_pending
    assert "LATEST WEEKLY DIGEST" in text and "Keep replies short." in text


def test_shareable_for_collects_each_recipient_but_not_indi(monkeypatch):
    import check_and_reply
    bodies = {ALICE: SECTIONED, "bob@example.com": "Bob likes chess.",
              "indi@example.com": SECTIONED}
    monkeypatch.setattr(memory_file, "SenderMemory",
                        lambda drive, addr: SimpleNamespace(body=bodies[addr]))
    out = check_and_reply.shareable_for(None, {ALICE, "bob@example.com", "indi@example.com"},
                                        {"indi@example.com"})
    assert out.startswith(f"{ALICE}:") and "Goes by Ali" in out
    assert "chess" not in out and "indi@example.com" not in out
