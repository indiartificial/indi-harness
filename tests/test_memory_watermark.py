"""
tests/test_memory_watermark.py

Which of a sender's messages are already in their memory file.

The header used to say `folded: N`, and "not yet folded" was
`messages_for_sender()[N:]` — an offset into a list. Anything that changed
the list's shape (a retried message landing before the offset, or the
store moving from Sheets to Gmail) silently shifted it: memory re-folded
what it had, or skipped what it hadn't, and nothing raised. The v2 header
names the folded messages by Gmail id instead.

What must hold:

  * a v2 header round-trips, and pending is "replied, not in the set"
  * a v1 header still reads, and resolves N against the store's current
    ordering into exactly the rows the old slice treated as folded — so
    the transition neither re-folds nor drops a message
  * a file with no header behaves as folded-nothing, as it always has
  * consolidation writes v2, folding what was pending and nothing else
  * once written as ids, the watermark does not move when the store's
    ordering does — the property the whole change exists for

This directory is protected. Indi can read it but not edit it.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import costs
import memory_file


ALICE = "alice@example.com"


# --------------------------------------------------------------------------
# FAKES
# --------------------------------------------------------------------------

class FakeStore:
    """messages_for_sender as the store defines it: that sender's replied
    rows, oldest first. Rows are given already in that order."""

    def __init__(self, ids):
        self.rows = [
            {"gmail_msg_id": i, "sender_addr": ALICE, "sender": ALICE,
             "subject": f"s-{i}", "body": f"b-{i}", "received_at": f"2026-09-{n + 1:02d}"}
            for n, i in enumerate(ids)
        ]

    def messages_for_sender(self, sender_addr):
        assert sender_addr == ALICE
        return list(self.rows)

    def responses_by_message(self):
        return {r["gmail_msg_id"]: [f"reply to {r['gmail_msg_id']}"] for r in self.rows}


@pytest.fixture
def drive(monkeypatch):
    """One memory file in a dict instead of on Drive."""
    files = {}

    def find(_drive, name):
        return name if name in files else None

    def read(_drive, file_id):
        return files.get(file_id, "") if file_id else ""

    def write(_drive, name, text, existing_file_id=None):
        files[existing_file_id or name] = text
        return existing_file_id or name

    monkeypatch.setattr(memory_file, "find_file_id", find)
    monkeypatch.setattr(memory_file, "read_file", read)
    monkeypatch.setattr(memory_file, "write_file", write)
    return files


def _put(files, text):
    files[memory_file.memory_file_name(ALICE)] = text


def _get(files):
    return files[memory_file.memory_file_name(ALICE)]


def _pending(store):
    memory = memory_file.SenderMemory(None, ALICE)
    return [r["gmail_msg_id"] for r in memory.pending_rows(store)]


V1 = "# indi-memory v1 | sender: alice@example.com | folded: {n} | updated: 2026-09-01T00:00:00+00:00\n\nAlice likes tea.\n"


# --------------------------------------------------------------------------
# PARSING
# --------------------------------------------------------------------------

def test_v2_round_trips():
    text = memory_file.serialise_memory(ALICE, {"b2", "a1"}, "Alice likes tea.")
    first = text.splitlines()[0]
    assert first.startswith("# indi-memory v2 | sender: alice@example.com | folded_ids: a1,b2 |")
    mark, body = memory_file.parse_memory(text)
    assert mark.ids == {"a1", "b2"}
    assert body.strip() == "Alice likes tea."


def test_v2_with_empty_set_round_trips():
    mark, body = memory_file.parse_memory(memory_file.serialise_memory(ALICE, set(), "x" * 30))
    assert mark.ids == frozenset()
    assert body.strip() == "x" * 30


def test_v1_parses_as_an_unresolved_count():
    mark, body = memory_file.parse_memory(V1.format(n=2))
    assert mark.ids is None and mark.legacy_count == 2
    assert body.strip() == "Alice likes tea."


def test_headerless_and_empty_files_have_folded_nothing():
    mark, body = memory_file.parse_memory("Just some notes.\nMore notes.\n")
    assert mark.ids == frozenset()
    assert body == "Just some notes.\nMore notes.\n"
    mark, body = memory_file.parse_memory("")
    assert mark.ids == frozenset() and body == ""


# --------------------------------------------------------------------------
# PENDING ROWS
# --------------------------------------------------------------------------

@pytest.mark.parametrize("n", [0, 1, 2, 3, 4, 10])
def test_v1_resolves_to_exactly_what_the_old_slice_left_pending(drive, n):
    ids = ["m1", "m2", "m3", "m4"]
    store = FakeStore(ids)
    _put(drive, V1.format(n=n))
    assert _pending(store) == ids[n:]


def test_v1_resolution_is_fixed_once_resolved(drive):
    # Resolving once pins the ids; a message that later appears *before*
    # them in the ordering is pending, where the old offset would have
    # silently treated it as folded.
    store = FakeStore(["m1", "m2", "m3"])
    _put(drive, V1.format(n=2))
    memory = memory_file.SenderMemory(None, ALICE)
    assert memory.folded_ids(store) == {"m1", "m2"}
    store.rows.insert(0, {**store.rows[0], "gmail_msg_id": "late", "received_at": "2026-08-01"})
    assert [r["gmail_msg_id"] for r in memory.pending_rows(store)] == ["late", "m3"]


def test_v2_pending_is_replied_rows_not_in_the_set(drive):
    store = FakeStore(["m1", "m2", "m3", "m4"])
    _put(drive, memory_file.serialise_memory(ALICE, {"m1", "m3"}, "notes"))
    assert _pending(store) == ["m2", "m4"]


def test_v2_watermark_does_not_drift_when_the_ordering_changes(drive):
    store = FakeStore(["m1", "m2", "m3", "m4"])
    _put(drive, memory_file.serialise_memory(ALICE, {"m1", "m2"}, "notes"))
    store.rows.reverse()
    assert sorted(_pending(store)) == ["m3", "m4"]
    store.rows.insert(1, {**store.rows[0], "gmail_msg_id": "new"})
    assert sorted(_pending(store)) == ["m3", "m4", "new"]


def test_headerless_file_leaves_everything_pending(drive):
    store = FakeStore(["m1", "m2"])
    _put(drive, "Old-style memory with no header.\n")
    assert _pending(store) == ["m1", "m2"]


def test_no_file_leaves_everything_pending(drive):
    assert _pending(FakeStore(["m1", "m2"])) == ["m1", "m2"]


def test_exclude_msg_ids_still_applies(drive):
    store = FakeStore(["m1", "m2", "m3"])
    _put(drive, V1.format(n=1))
    memory = memory_file.SenderMemory(None, ALICE)
    got = memory.pending_rows(store, exclude_msg_ids=["m3"])
    assert [r["gmail_msg_id"] for r in got] == ["m2"]


# --------------------------------------------------------------------------
# CONSOLIDATION
# --------------------------------------------------------------------------

class FakeClient:
    def __init__(self, text):
        self.prompts = []
        self._text = text
        self.messages = self

    def stream(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"])
        response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._text)],
            stop_reason="end_turn", usage=None,
        )

        class Ctx:
            def __enter__(self_inner):
                return SimpleNamespace(get_final_message=lambda: response)

            def __exit__(self_inner, *a):
                return False

        return Ctx()


def _cfg(batch=2):
    return SimpleNamespace(
        memory_batch_size=batch, model="claude-sonnet-5", memory_max_tokens=4000,
        dry_run=False, memory_keep_backup=False,
    )


@pytest.fixture(autouse=True)
def no_cost_ledger(monkeypatch):
    monkeypatch.setattr(costs, "record", lambda *a, **k: None)


NEW_BODY = "Alice likes tea, and now also coffee on Thursdays."


@pytest.mark.parametrize("header, folded_before", [
    (V1.format(n=2), {"m1", "m2"}),
    (memory_file.serialise_memory(ALICE, {"m1", "m2"}, "Alice likes tea."), {"m1", "m2"}),
    ("Alice likes tea.\n", set()),
])
def test_consolidation_folds_exactly_the_pending_rows_and_writes_v2(drive, header, folded_before):
    store = FakeStore(["m1", "m2", "m3", "m4"])
    _put(drive, header)
    client = FakeClient(NEW_BODY)

    assert memory_file.consolidate(client, _cfg(), None, store, ALICE) is True

    # The prompt carried the pending rows, and not the folded ones.
    prompt = client.prompts[0]
    for i in ["m1", "m2", "m3", "m4"]:
        assert (f"Subject: s-{i}\n" in prompt) == (i not in folded_before)

    mark, body = memory_file.parse_memory(_get(drive))
    assert _get(drive).startswith("# indi-memory v2 |")
    assert mark.ids == {"m1", "m2", "m3", "m4"}
    assert body.strip() == NEW_BODY
    assert _pending(store) == []


def test_consolidation_waits_for_a_full_batch(drive):
    store = FakeStore(["m1", "m2", "m3"])
    _put(drive, V1.format(n=2))
    client = FakeClient(NEW_BODY)
    assert memory_file.consolidate(client, _cfg(batch=2), None, store, ALICE) is False
    assert client.prompts == []
    # Nothing was written, so the v1 file is untouched until it is due.
    assert _get(drive) == V1.format(n=2)


def test_upgraded_file_then_behaves_like_any_v2_file(drive):
    store = FakeStore(["m1", "m2", "m3"])
    _put(drive, V1.format(n=1))
    memory_file.consolidate(FakeClient(NEW_BODY), _cfg(), None, store, ALICE)
    store.rows.append({**store.rows[0], "gmail_msg_id": "m4", "received_at": "2026-09-30"})
    assert _pending(store) == ["m4"]
