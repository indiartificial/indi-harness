"""
tests/test_status_labels.py

Step 2 of MIGRATE-GMAIL.md: every status the store records is mirrored
onto the message as one Indi/* label, and a one-off script backfills the
history.

The labels are write-only for now. Nothing reads them, so the property
that matters most is not that they are right but that they are
*harmless*: a Gmail client that raises on every call must leave the
store doing exactly what it does without one — same sheet writes, same
return values, no exception. Those tests are the ones to keep when the
rest of this file is rewritten at step 5.

Covered:

  * each status gets its own label, and the other four come off
  * a label that doesn't exist is created, once, and cached
  * a failing or raising Gmail client changes nothing about the store
  * the label is only written after the sheet write succeeds
  * the backfill's dry run makes no mutating call; `--apply` labels,
    skips messages that are gone, and a second pass changes nothing

This directory is protected. Indi can read it but not edit it.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import backfill_labels
import sheets_queue
from test_store import ALICE, _store


ALL_LABELS = set(sheets_queue.STATUS_LABELS.values())


# --------------------------------------------------------------------------
# FAKES
# --------------------------------------------------------------------------

class NotFound(Exception):
    """Shaped like googleapiclient's HttpError for a 404."""

    def __init__(self):
        super().__init__("Requested entity was not found.")
        self.resp = SimpleNamespace(status=404)


class FakeGmail:
    """Labels and per-message label sets, enough for StatusLabeler and the
    backfill. Every call is recorded in `calls` as (method, kwargs), so a
    test can assert what was — and wasn't — mutated."""

    def __init__(self, labels=(), messages=(), gone=()):
        self.labels_by_id = {f"L{i}": name for i, name in enumerate(labels)}
        self.msg_labels = {m: set() for m in messages}
        self.gone = set(gone)
        self.calls = []

    def users(self):
        return self

    def labels(self):
        return SimpleNamespace(list=self._list_labels, create=self._create_label)

    def messages(self):
        return SimpleNamespace(get=self._get, modify=self._modify)

    def _call(self, name, kwargs, result):
        self.calls.append((name, kwargs))
        return SimpleNamespace(execute=result)

    def _list_labels(self, **kw):
        return self._call("labels.list", kw, lambda: {"labels": [
            {"id": i, "name": n} for i, n in self.labels_by_id.items()]})

    def _create_label(self, **kw):
        def run():
            new_id = f"L{len(self.labels_by_id)}"
            self.labels_by_id[new_id] = kw["body"]["name"]
            return {"id": new_id, "name": kw["body"]["name"]}
        return self._call("labels.create", kw, run)

    def _check(self, msg_id):
        if msg_id in self.gone:
            raise NotFound()

    def _get(self, **kw):
        def run():
            self._check(kw["id"])
            return {"labelIds": sorted(self.msg_labels.setdefault(kw["id"], set()))}
        return self._call("messages.get", kw, run)

    def _modify(self, **kw):
        def run():
            self._check(kw["id"])
            current = self.msg_labels.setdefault(kw["id"], set())
            current |= set(kw["body"].get("addLabelIds", []))
            current -= set(kw["body"].get("removeLabelIds", []))
            return {"id": kw["id"]}
        return self._call("messages.modify", kw, run)

    def names_on(self, msg_id):
        # System labels (INBOX, UNREAD) have no entry and are their own name.
        return {self.labels_by_id.get(i, i) for i in self.msg_labels.get(msg_id, set())}

    def mutations(self):
        return [c for c in self.calls if c[0] in ("labels.create", "messages.modify")]


class BrokenGmail:
    """Every call raises, at build time or at execute time."""

    def __init__(self, at="execute"):
        self.at = at

    def users(self):
        if self.at == "build":
            raise RuntimeError("gmail is down")
        return self

    def __getattr__(self, name):
        def builder(*a, **kw):
            return SimpleNamespace(**{
                "list": self._fail, "create": self._fail,
                "get": self._fail, "modify": self._fail,
            })
        return builder

    def _fail(self, **kw):
        def execute():
            raise RuntimeError("gmail is down")
        return SimpleNamespace(execute=execute)


def _labelled_store(gmail, messages=()):
    """A test_store `_store`, with a Gmail client attached."""
    base = _store(messages=messages)
    store = sheets_queue.SheetStore(base.sheets, None, base.cfg, gmail_service=gmail)
    store.queue_id, store.responses_id = base.queue_id, base.responses_id
    return store


def _msg(msg_id):
    return {"gmail_msg_id": msg_id, "thread_id": "t", "from_raw": ALICE,
            "from_addr": ALICE, "subject": "hi", "body": "hello",
            "internal_date": 1790000000000}


# --------------------------------------------------------------------------
# STATUS WRITES
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status,label", sorted(sheets_queue.STATUS_LABELS.items()))
def test_set_status_sets_that_label_and_only_that_one(status, label):
    gmail = FakeGmail(labels=ALL_LABELS)
    store = _labelled_store(gmail, [{"gmail_msg_id": "m1", "sender_addr": ALICE}])
    # Start from a message carrying every other status label, so removal
    # is actually exercised.
    gmail.msg_labels["m1"] = {i for i, n in gmail.labels_by_id.items() if n != label}

    store.set_status(store.find_queue_row("m1"), status)

    assert gmail.names_on("m1") == {label}


def test_append_queue_row_labels_the_new_message():
    gmail = FakeGmail(labels=ALL_LABELS)
    store = _labelled_store(gmail)
    store.append_queue_row(_msg("m9"), sheets_queue.STATUS_SKIPPED, "automated")
    assert gmail.names_on("m9") == {"Indi/Skipped"}


def test_unknown_labels_on_the_message_are_left_alone():
    gmail = FakeGmail(labels=ALL_LABELS | {"Personal"})
    store = _labelled_store(gmail, [{"gmail_msg_id": "m1", "sender_addr": ALICE}])
    personal = next(i for i, n in gmail.labels_by_id.items() if n == "Personal")
    gmail.msg_labels["m1"] = {personal, "INBOX"}
    store.set_status(store.find_queue_row("m1"), sheets_queue.STATUS_REPLIED)
    assert gmail.names_on("m1") == {"INBOX", "Personal", "Indi/Replied"}


def test_missing_label_is_created_once_and_lookups_are_cached():
    gmail = FakeGmail(labels=())
    store = _labelled_store(gmail, [
        {"gmail_msg_id": "m1", "sender_addr": ALICE},
        {"gmail_msg_id": "m2", "sender_addr": ALICE},
    ])
    store.set_status(store.find_queue_row("m1"), sheets_queue.STATUS_REPLIED)
    store.set_status(store.find_queue_row("m2"), sheets_queue.STATUS_REPLIED)

    creates = [c for c in gmail.calls if c[0] == "labels.create"]
    assert [c[1]["body"]["name"] for c in creates] == ["Indi/Replied"]
    assert [c[0] for c in gmail.calls].count("labels.list") == 1
    assert gmail.names_on("m1") == gmail.names_on("m2") == {"Indi/Replied"}
    # Labels that only needed removing were never created.
    assert set(gmail.labels_by_id.values()) == {"Indi/Replied"}


def test_no_gmail_client_means_no_label_calls():
    store = _store(messages=[{"gmail_msg_id": "m1", "sender_addr": ALICE}])
    assert store._labeler is None
    store.set_status(store.find_queue_row("m1"), sheets_queue.STATUS_FAILED, "x")
    assert store.find_queue_row("m1")["status"] == sheets_queue.STATUS_FAILED


def test_check_and_reply_passes_gmail_only_when_the_flag_is_on():
    src = (Path(__file__).resolve().parent.parent / "check_and_reply.py").read_text()
    assert "gmail_service=gmail if cfg.gmail_status_labels else None" in src


def test_flag_defaults_on_and_turns_off(monkeypatch):
    import config
    monkeypatch.delenv("INDI_GMAIL_STATUS_LABELS", raising=False)
    monkeypatch.delenv("INDI_GMAIL_STATUS_LABELS" + config._PLATFORM_SUFFIX, raising=False)
    assert config._bool("INDI_GMAIL_STATUS_LABELS", True) is True
    monkeypatch.setenv("INDI_GMAIL_STATUS_LABELS", "false")
    assert config._bool("INDI_GMAIL_STATUS_LABELS", True) is False


# --------------------------------------------------------------------------
# FAILURE IS HARMLESS
# --------------------------------------------------------------------------

def _run_status_writes(gmail):
    """The same sequence of store calls, returning everything observable."""
    store = (_labelled_store(gmail, [{"gmail_msg_id": "m1", "sender_addr": ALICE}])
             if gmail is not None else
             _store(messages=[{"gmail_msg_id": "m1", "sender_addr": ALICE}]))
    results = [
        store.set_status(store.find_queue_row("m1"), sheets_queue.STATUS_FAILED, "boom"),
        store.append_queue_row(_msg("m2"), sheets_queue.STATUS_QUEUED),
    ]
    results.append(store.set_status(store.find_queue_row("m2"),
                                    sheets_queue.STATUS_REPLIED))
    return results, store.sheets.updates, store.sheets.tabs[("Q", sheets_queue.QUEUE_TAB)]


@pytest.mark.parametrize("broken", [BrokenGmail("execute"), BrokenGmail("build")],
                         ids=["execute-raises", "build-raises"])
def test_a_failing_gmail_client_changes_nothing_about_the_store(broken):
    assert _run_status_writes(broken) == _run_status_writes(None)


def test_a_failed_create_is_swallowed_and_retried_next_time():
    gmail = FakeGmail(labels=())
    real_create = gmail._create_label
    attempts = []

    def flaky(**kw):
        attempts.append(kw)
        if len(attempts) == 1:
            return SimpleNamespace(execute=lambda: (_ for _ in ()).throw(
                RuntimeError("already exists")))
        return real_create(**kw)

    gmail.labels = lambda: SimpleNamespace(list=gmail._list_labels, create=flaky)
    store = _labelled_store(gmail, [{"gmail_msg_id": "m1", "sender_addr": ALICE}])
    store.set_status(store.find_queue_row("m1"), sheets_queue.STATUS_REPLIED)
    assert gmail.names_on("m1") == set()
    store.set_status(store.find_queue_row("m1"), sheets_queue.STATUS_REPLIED)
    assert gmail.names_on("m1") == {"Indi/Replied"}


def test_label_is_not_written_when_the_sheet_write_fails():
    gmail = FakeGmail(labels=ALL_LABELS)
    store = _labelled_store(gmail, [{"gmail_msg_id": "m1", "sender_addr": ALICE}])

    def broken_update(**kw):
        raise RuntimeError("sheets is down")

    store.sheets.update = broken_update
    with pytest.raises(RuntimeError, match="sheets is down"):
        store.set_status(store.find_queue_row("m1"), sheets_queue.STATUS_REPLIED)
    assert gmail.mutations() == []


def test_row_without_a_sheet_row_number_is_not_labelled():
    gmail = FakeGmail(labels=ALL_LABELS)
    store = _labelled_store(gmail)
    store.set_status({"gmail_msg_id": "m1"}, sheets_queue.STATUS_REPLIED)
    assert gmail.mutations() == []


# --------------------------------------------------------------------------
# BACKFILL
# --------------------------------------------------------------------------

def _rows():
    return [
        {"gmail_msg_id": "a", "status": sheets_queue.STATUS_REPLIED},
        {"gmail_msg_id": "b", "status": sheets_queue.STATUS_SKIPPED},
        {"gmail_msg_id": "gone", "status": sheets_queue.STATUS_IGNORED},
        {"gmail_msg_id": "c", "status": ""},
        {"gmail_msg_id": "a", "status": sheets_queue.STATUS_FAILED},  # dup: first wins
    ]


def test_backfill_dry_run_is_the_default_and_mutates_nothing():
    gmail = FakeGmail(labels=(), messages=("a", "b", "c"), gone=("gone",))
    assert backfill_labels.parse_args([]).apply is False

    out = []
    counts = backfill_labels.backfill(gmail, _rows(), out=out.append)

    assert gmail.mutations() == []
    assert gmail.labels_by_id == {}
    assert counts["would_label"] == 2
    assert counts["missing"] == 1 and counts["no_status"] == 1
    assert any("--apply" in line for line in out)


def test_backfill_apply_labels_tolerates_gone_messages_and_is_idempotent():
    gmail = FakeGmail(labels=ALL_LABELS, messages=("a", "b", "c"), gone=("gone",))
    failed = next(i for i, n in gmail.labels_by_id.items() if n == "Indi/Failed")
    gmail.msg_labels["b"] = {failed}

    first = backfill_labels.backfill(gmail, _rows(), apply=True, out=lambda _: None)
    assert first["labelled"] == 2 and first["missing"] == 1 and first["errors"] == 0
    assert gmail.names_on("a") == {"Indi/Replied"}
    assert gmail.names_on("b") == {"Indi/Skipped"}
    assert gmail.names_on("c") == set()

    before = len(gmail.mutations())
    second = backfill_labels.backfill(gmail, _rows(), apply=True, out=lambda _: None)
    assert second["already"] == 2 and second["labelled"] == 0
    assert len(gmail.mutations()) == before


def test_backfill_carries_on_past_an_error():
    gmail = FakeGmail(labels=ALL_LABELS, messages=("a", "b"))
    real_get = gmail._get

    def get(**kw):
        if kw["id"] == "a":
            return SimpleNamespace(execute=lambda: (_ for _ in ()).throw(
                RuntimeError("500")))
        return real_get(**kw)

    gmail.messages = lambda: SimpleNamespace(get=get, modify=gmail._modify)
    counts = backfill_labels.backfill(gmail, _rows()[:2], apply=True, out=lambda _: None)
    assert counts["errors"] == 1 and counts["labelled"] == 1
    assert gmail.names_on("b") == {"Indi/Skipped"}


def test_backfill_is_not_wired_into_the_scheduled_runs():
    root = Path(__file__).resolve().parent.parent
    for name in ("check_and_reply.py", "outbound.py", "newsletter.py"):
        assert "backfill_labels" not in (root / name).read_text(), name
