"""
tests/test_reply_send_path.py

What happens after the mail has gone out.

Once `process_message` has handed a reply to Gmail, the two things that
stop that message being answered again — a terminal queue row and the
UNREAD label coming off — must happen, and nothing that happens
afterwards may prevent them. A reply is a thing said to a real person;
losing a bookkeeping row is a blemish, saying it twice is not.

This is not hypothetical. The post-send block used to read

    clean_body, attachments, warnings = claude_api.extract_outgoing_...
    ...
    rel = attachments.store_text(...)

where the first line made `attachments` a local for the whole function,
so the second called `.store_text` on a list. It raised AttributeError
after the send and before the status update and the mark-as-read, the
caller recorded the row as failed, the message stayed unread, and the
next run replied all over again — once per run, but only ever for
replies that carried an attachment, which is why it lived so long.

So these tests cover:

  * a reply with a generated attachment gets all the way through, stores
    the outgoing copy through the real attachments module, and leaves the
    message read and the row replied
  * bookkeeping that raises after the send is logged and swallowed, and
    the message is still read and the row still replied
  * the name `attachments` is not rebound inside process_message, which
    is the specific mistake above rather than the class of it

This directory is protected. Indi can read it but not edit it.
"""

import ast
import base64
import sys
from email import message_from_bytes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import attachments
import check_and_reply
import claude_api
import correspondents
import memory_file
import sheets_queue
import terms


SENDER = "alice@example.com"


# --------------------------------------------------------------------------
# FAKES
# --------------------------------------------------------------------------

class FakeGmail:
    """Records what was sent, drafted and marked read."""

    def __init__(self):
        self.sent = []
        self.drafted = []
        self.read = []

    def users(self):
        return self

    def messages(self):
        return self

    def drafts(self):
        return self

    def send(self, userId, body):
        self.sent.append(body)
        return self

    def create(self, userId, body):
        self.drafted.append(body["message"])
        return self

    def modify(self, userId, id, body):
        self.read.append(id)
        return self

    def execute(self):
        return {"id": "x"}


class FakeClaude:
    """Only the emergency classifier is reached; the reply model is stubbed
    out at claude_api.generate_reply, so `stream` raising is an assertion."""

    def __init__(self):
        self.messages = self

    def create(self, **kwargs):
        block = type("B", (), {"type": "text", "text": "NO"})()
        return type("R", (), {"content": [block], "usage": None})()

    def stream(self, **kwargs):
        raise AssertionError("the real reply model was called")


class FakeStore:
    """The sheets queue, reduced to what the reply path touches. Any
    method can be made to raise, which is how the post-send failures
    below are provoked."""

    def __init__(self, raises=()):
        self.rows = []
        self.responses = []
        self.attachment_paths = []
        self.raises = set(raises)

    def _maybe_raise(self, name):
        if name in self.raises:
            raise RuntimeError(f"{name} is broken")

    def find_queue_row(self, msg_id):
        return None

    def append_queue_row(self, msg, status, note=""):
        row = {"gmail_msg_id": msg["gmail_msg_id"], "status": status, "note": note}
        self.rows.append(row)
        return row

    def set_status(self, row, status, note=""):
        self._maybe_raise("set_status")
        row["status"], row["note"] = status, note

    def replies_in_last_day(self, sender):
        return 0

    def append_response(self, msg_id, sender, body, reply_id=""):
        self._maybe_raise("append_response")
        self.responses.append((msg_id, sender, body, reply_id))

    def set_attachments(self, row, paths):
        self._maybe_raise("set_attachments")
        self.attachment_paths = list(paths)


class FakeMemory:
    """No Drive in a test, so nothing to read and nothing pending."""

    def __init__(self, drive, sender):
        self.body = ""
        self.folded = 0

    def pending_rows(self, store, exclude_msg_ids=None):
        return []

    def pending_text(self, store, exclude_msg_ids=None):
        return ""


def _cfg(tmp_path, **over):
    base = dict(
        dry_run=False, send_automatically=True, model="claude-sonnet-5",
        max_body_chars=45000, max_quoted_chars=6000,
        max_replies_per_sender_per_day=10, allowed_senders=(),
        max_thread_messages=0, max_thread_chars=0,
        max_outgoing_attachment_bytes=100000, enable_web_search=False,
        require_authenticated_sender=False, enable_self_code=False,
        enable_claude_code=False, claude_code_senders=(),
        enable_local_files=False, enable_local_exec=False,
        local_files_root=None, local_files_writable_root=None,
        attachments_dir=tmp_path / "attachments", store_attachments=True,
        max_stored_attachment_mb=10, max_stored_attachments_per_message_mb=25,
    )
    base.update(over)
    methods = {
        "is_allowed": lambda self, addr: True,
        "is_trusted": lambda self, addr: False,
        "may_search_all_history": lambda self, addr: False,
        "may_use_local_tools": lambda self, addr: False,
        "may_run_claude_code": lambda self, addr, authentic: False,
        "may_modify_own_source": lambda self, addr: False,
    }
    return type("C", (), {**base, **methods})()


def _raw(body="could you send me that list?", sender=SENDER, subject="lists"):
    """A Gmail `format=full` message, the way process_message is handed one."""
    return {
        "id": "m1", "threadId": "t1", "internalDate": "1700000000000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
                {"name": "Message-ID", "value": "<m1@example.com>"},
            ],
            "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
        },
    }


REPLY_WITH_ATTACHMENT = (
    "Here is the list you asked for.\n\n"
    "[ATTACHMENT: list.txt]\n"
    "one\ntwo\nthree\n"
    "[/ATTACHMENT]\n\n"
    "--- Indi"
)

STATS = {
    "server_tools": 0, "history_searches": 0, "cache_read": 0,
    "cache_created": 0, "tokens": {"input": 100, "output": 50},
}


def _attachments_of(mime):
    message = message_from_bytes(base64.urlsafe_b64decode(mime["raw"]))
    if not message.is_multipart():
        return {}
    return {
        part.get_filename(): part.get_payload(decode=True).decode()
        for part in message.get_payload()[1:]
    }


@pytest.fixture
def terms_dir(tmp_path, monkeypatch):
    directory = tmp_path / "terms"
    directory.mkdir()
    (directory / "v0.2.txt").write_text("These are the terms.\n", encoding="utf-8")
    monkeypatch.setattr(terms, "TERMS_DIR", directory)
    return directory


@pytest.fixture
def people(tmp_path, terms_dir):
    """A correspondent who has already accepted the terms in force, so the
    gate says proceed and the reply path is what is under test."""
    store = correspondents.CorrespondentStore(tmp_path / "correspondents.db")
    store.record_acceptance(SENDER, terms.load().version)
    yield store
    store.close()


@pytest.fixture
def reply(monkeypatch):
    """Everything upstream of the send that costs money or needs Google.
    `reply.text` is what the model is taken to have written."""
    holder = type("H", (), {"text": REPLY_WITH_ATTACHMENT})()
    monkeypatch.setattr(claude_api, "build_attachment_blocks",
                        lambda *a, **k: ([], [], []))
    monkeypatch.setattr(claude_api, "generate_reply",
                        lambda *a, **k: (holder.text, dict(STATS)))
    monkeypatch.setattr(memory_file, "SenderMemory", FakeMemory)
    monkeypatch.setattr(memory_file, "consolidate", lambda *a, **k: None)
    return holder


def _run(gmail, cfg, store, people):
    return check_and_reply.process_message(
        gmail, None, FakeClaude(), cfg, store, _raw(), set(), "system prompt",
        people,
    )


# --------------------------------------------------------------------------
# THE HAPPY PATH, WITH AN ATTACHMENT ON IT
# --------------------------------------------------------------------------

def test_a_reply_with_an_attachment_gets_all_the_way_through(
        tmp_path, people, reply, monkeypatch):
    """The regression test proper. Against the old code this raised
    AttributeError: 'list' object has no attribute 'store_text'."""
    calls = []
    real_store_text = attachments.store_text

    def spy(cfg, message_id, filename, text, direction, **kwargs):
        calls.append((filename, direction))
        return real_store_text(cfg, message_id, filename, text, direction,
                              **kwargs)

    monkeypatch.setattr(attachments, "store_text", spy)

    gmail, cfg, store = FakeGmail(), _cfg(tmp_path), FakeStore()
    outcome = _run(gmail, cfg, store, people)

    assert outcome == "sent"
    assert len(gmail.sent) == 1
    assert _attachments_of(gmail.sent[0]) == {"list.txt": "one\ntwo\nthree"}

    # store_text on the module, not on the list of outgoing attachments.
    assert calls == [("list.txt", attachments.DIRECTION_OUT)]
    stored = attachments.stored(cfg)
    assert [s.filename for s in stored] == ["list.txt"]
    assert (attachments.root(cfg) / store.attachment_paths[0]).read_text() == (
        "one\ntwo\nthree")

    assert gmail.read == ["m1"]
    assert store.rows[0]["status"] == sheets_queue.STATUS_REPLIED
    assert store.responses and store.responses[0][1] == SENDER
    # The sent reply's own Gmail id reaches the store, which labels it.
    assert store.responses[0][3] == "x"


def test_a_reply_without_an_attachment_is_unchanged(tmp_path, people, reply):
    reply.text = "No list needed.\n\n--- Indi"
    gmail, cfg, store = FakeGmail(), _cfg(tmp_path), FakeStore()

    assert _run(gmail, cfg, store, people) == "sent"
    assert _attachments_of(gmail.sent[0]) == {}
    assert gmail.read == ["m1"]
    assert store.rows[0]["status"] == sheets_queue.STATUS_REPLIED
    assert store.attachment_paths == []


def test_a_dry_run_sends_nothing_and_stores_nothing(tmp_path, people, reply,
                                                    capsys):
    gmail, cfg, store = FakeGmail(), _cfg(tmp_path, dry_run=True), FakeStore()

    outcome = _run(gmail, cfg, store, people)

    assert outcome == "dry run (nothing written)"
    assert gmail.sent == [] and gmail.drafted == [] and gmail.read == []
    assert "[attachment list.txt: 13 chars]" in capsys.readouterr().out
    assert attachments.stored(cfg) == []


# --------------------------------------------------------------------------
# BOOKKEEPING THAT FAILS AFTER THE SEND
# --------------------------------------------------------------------------

@pytest.mark.parametrize("broken", ["append_response", "set_attachments"])
def test_a_broken_sheet_after_the_send_does_not_lose_the_mark_as_read(
        tmp_path, people, reply, broken, caplog):
    """The sheet failing is not the sender's problem, and it must not
    become their problem next run either."""
    gmail, cfg = FakeGmail(), _cfg(tmp_path)
    store = FakeStore(raises=[broken])

    with caplog.at_level("WARNING"):
        outcome = _run(gmail, cfg, store, people)

    assert outcome == "sent"
    assert gmail.read == ["m1"]
    assert store.rows[0]["status"] == sheets_queue.STATUS_REPLIED
    assert broken in caplog.text or "Could not" in caplog.text


def test_storing_the_outgoing_copy_failing_does_not_propagate(
        tmp_path, people, reply, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("the disk is gone")

    monkeypatch.setattr(attachments, "store_text", boom)
    gmail, cfg, store = FakeGmail(), _cfg(tmp_path), FakeStore()

    with caplog.at_level("WARNING"):
        outcome = _run(gmail, cfg, store, people)

    assert outcome == "sent"
    assert len(gmail.sent) == 1
    assert gmail.read == ["m1"]
    assert store.rows[0]["status"] == sheets_queue.STATUS_REPLIED
    assert "outgoing attachment copies" in caplog.text


def test_memory_consolidation_failing_does_not_propagate(
        tmp_path, people, reply, monkeypatch, caplog):
    monkeypatch.setattr(memory_file, "consolidate",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no drive")))
    gmail, cfg, store = FakeGmail(), _cfg(tmp_path), FakeStore()

    with caplog.at_level("WARNING"):
        assert _run(gmail, cfg, store, people) == "sent"
    assert gmail.read == ["m1"]
    assert store.rows[0]["status"] == sheets_queue.STATUS_REPLIED


def test_the_queue_row_failing_still_marks_the_message_read(
        tmp_path, people, reply, caplog):
    """The two guards back each other up: if the row cannot be written,
    the read label is what keeps the message from coming back."""
    gmail, cfg = FakeGmail(), _cfg(tmp_path)
    store = FakeStore(raises=["set_status"])

    with caplog.at_level("WARNING"):
        assert _run(gmail, cfg, store, people) == "sent"
    assert gmail.read == ["m1"]


# --------------------------------------------------------------------------
# THE SHADOWING ITSELF
# --------------------------------------------------------------------------

def test_the_attachments_module_is_not_shadowed_in_process_message():
    """`attachments` is a module this file imports and calls. Binding that
    name to anything inside process_message makes it local for the whole
    body, and every earlier-looking use of the module becomes a runtime
    error — after the mail has gone."""
    assert "attachments" not in check_and_reply.process_message.__code__.co_varnames


def test_no_imported_module_name_is_assigned_inside_process_message():
    """The general form of the bug above, read off the source."""
    source = Path(check_and_reply.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = {
        alias.asname or alias.name.split(".")[0]
        for node in tree.body if isinstance(node, ast.Import)
        for alias in node.names
    }
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "process_message")
    assigned = {
        target.id
        for node in ast.walk(function) if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Store)
        for target in [node]
    }
    assert imported and not (imported & assigned), (
        f"process_message rebinds imported module name(s): "
        f"{sorted(imported & assigned)}"
    )
