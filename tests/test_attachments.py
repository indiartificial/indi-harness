"""
tests/test_attachments.py

Attachments are supposed to survive the run that carried them. These
tests are about that promise and the ones that come with it:

  * a filename from a stranger cannot become a path — no separators, no
    traversal, no absolute paths, no dotfiles
  * a second file with the same name is a second file, not an overwrite
  * an oversized file is skipped and logged, not raised
  * an incoming attachment and an outgoing one both round-trip: stored,
    listed, read back byte for byte
  * the two tools cannot see outside the store, and a correspondent whose
    history search is scoped to their own thread sees only their own files
  * a missing store, a missing index and a queue row written before the
    `attachments` column existed are all ordinary conditions, not errors

This directory is protected. Indi can read it but not edit it.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import attachments
import sheets_queue
from local_tools import ToolError


ALICE = "alice@example.com"
BOB = "bob@example.com"


def _cfg(tmp_path, **over):
    base = dict(
        attachments_dir=tmp_path / "attachments",
        store_attachments=True,
        max_stored_attachment_mb=10,
        max_stored_attachments_per_message_mb=25,
        max_inline_attachment_chars=40000,
        max_body_chars=45000,
    )
    base.update(over)
    return type("C", (), base)()


@pytest.fixture
def cfg(tmp_path):
    return _cfg(tmp_path)


# --------------------------------------------------------------------------
# FILENAMES
# --------------------------------------------------------------------------

@pytest.mark.parametrize("hostile", [
    "../../../../etc/passwd",
    "..\\..\\windows\\system32\\config",
    "/etc/shadow",
    "sub/dir/notes.txt",
    "..",
    ".",
    ".bashrc",
    ".env",
    ".ssh/authorized_keys",
])
def test_a_filename_cannot_become_a_path(cfg, hostile):
    rel = attachments.store(cfg, "m1", hostile, b"payload",
                            attachments.DIRECTION_IN, sender=ALICE)
    assert rel is not None
    stored_file = attachments.root(cfg) / rel

    # One directory below the root, one file below that, and nothing
    # hidden. Everything the sender wrote is now a leaf name.
    assert stored_file.is_file()
    assert stored_file.parent.parent == attachments.root(cfg)
    assert not stored_file.name.startswith(".")
    assert "/" not in rel.split("/", 1)[1]
    assert stored_file.read_bytes() == b"payload"


def test_the_message_id_cannot_become_a_path_either(cfg):
    rel = attachments.store(cfg, "../../escape", "notes.txt", b"x",
                            attachments.DIRECTION_IN, sender=ALICE)
    assert rel is not None
    assert (attachments.root(cfg) / rel).resolve().is_relative_to(
        attachments.root(cfg).resolve()
    )


def test_a_collision_makes_a_second_file(cfg):
    first = attachments.store(cfg, "m1", "report.csv", b"one",
                              attachments.DIRECTION_IN, sender=ALICE)
    second = attachments.store(cfg, "m1", "report.csv", b"two",
                               attachments.DIRECTION_IN, sender=ALICE)
    assert first != second
    assert (attachments.root(cfg) / first).read_bytes() == b"one"
    assert (attachments.root(cfg) / second).read_bytes() == b"two"
    assert second.endswith("report-2.csv")


# --------------------------------------------------------------------------
# SIZE CAPS
# --------------------------------------------------------------------------

def test_an_oversized_file_is_skipped_not_raised(tmp_path, caplog):
    cfg = _cfg(tmp_path, max_stored_attachment_mb=1)
    too_big = b"x" * (1024 * 1024 + 1)
    with caplog.at_level("WARNING"):
        assert attachments.store(cfg, "m1", "huge.bin", too_big,
                                 attachments.DIRECTION_IN, sender=ALICE) is None
    assert "per-file cap" in caplog.text
    assert attachments.stored(cfg) == []


def test_the_per_message_cap_stops_the_pile_growing(tmp_path, caplog):
    cfg = _cfg(tmp_path, max_stored_attachment_mb=10,
               max_stored_attachments_per_message_mb=1)
    half = b"y" * (600 * 1024)
    assert attachments.store(cfg, "m1", "a.bin", half,
                             attachments.DIRECTION_IN, sender=ALICE)
    with caplog.at_level("WARNING"):
        assert attachments.store(cfg, "m1", "b.bin", half,
                                 attachments.DIRECTION_IN, sender=ALICE) is None
    assert "per-message cap" in caplog.text
    # A different message is unaffected: the cap is per message, not global.
    assert attachments.store(cfg, "m2", "b.bin", half,
                             attachments.DIRECTION_IN, sender=ALICE)


def test_storing_can_be_turned_off_entirely(tmp_path):
    cfg = _cfg(tmp_path, store_attachments=False)
    assert attachments.store(cfg, "m1", "a.txt", b"x",
                             attachments.DIRECTION_IN, sender=ALICE) is None
    assert not attachments.root(cfg).exists()


def test_a_dry_run_writes_nothing_to_disk(tmp_path):
    """--dry-run means nothing is written anywhere, and someone else's
    file appearing on the disk is exactly what that promise covers."""
    cfg = _cfg(tmp_path, dry_run=True)
    assert attachments.store(cfg, "m1", "a.txt", b"x",
                             attachments.DIRECTION_IN, sender=ALICE) is None
    assert attachments.store_text(cfg, "m1", "b.txt", "y",
                                  attachments.DIRECTION_OUT, sender=ALICE) is None
    assert not attachments.root(cfg).exists()


# --------------------------------------------------------------------------
# ROUND TRIPS
# --------------------------------------------------------------------------

def test_an_incoming_attachment_round_trips(cfg):
    rel = attachments.store(cfg, "m1", "notes.txt", "hello, world\n".encode(),
                            attachments.DIRECTION_IN, sender=ALICE,
                            subject="my notes")
    listed = attachments.stored(cfg)
    assert [s.path for s in listed] == [rel]
    entry = listed[0]
    assert entry.direction == attachments.DIRECTION_IN
    assert entry.sender == ALICE
    assert entry.subject == "my notes"
    assert entry.size == len("hello, world\n")
    assert entry.stored_at
    assert attachments.read(cfg, rel) == "hello, world\n"


def test_an_outgoing_attachment_round_trips(cfg):
    rel = attachments.store_text(cfg, "m1", "summary.csv", "a,b\n1,2\n",
                                 attachments.DIRECTION_OUT, sender=BOB,
                                 subject="the numbers")
    entry = attachments.stored(cfg)[0]
    assert entry.direction == attachments.DIRECTION_OUT
    assert entry.sender == BOB
    assert attachments.read(cfg, rel) == "a,b\n1,2\n"
    assert "to bob@example.com" in entry.describe()


def test_both_directions_of_one_message_live_together(cfg):
    attachments.store(cfg, "m1", "theirs.txt", b"in",
                      attachments.DIRECTION_IN, sender=ALICE)
    attachments.store_text(cfg, "m1", "mine.txt", "out",
                           attachments.DIRECTION_OUT, sender=ALICE)
    paths = {s.path for s in attachments.stored(cfg)}
    assert {p.split("/")[0] for p in paths} == {"m1"}
    assert {s.direction for s in attachments.stored(cfg)} == {
        attachments.DIRECTION_IN, attachments.DIRECTION_OUT
    }


def test_a_substring_filter_narrows_the_listing(cfg):
    attachments.store(cfg, "m1", "invoice.txt", b"x",
                      attachments.DIRECTION_IN, sender=ALICE, subject="billing")
    attachments.store(cfg, "m2", "holiday.txt", b"y",
                      attachments.DIRECTION_IN, sender=ALICE, subject="away")
    assert [s.filename for s in attachments.stored(cfg, query="invoice")] \
        == ["invoice.txt"]
    assert [s.filename for s in attachments.stored(cfg, query="away")] \
        == ["holiday.txt"]
    assert attachments.stored(cfg, query="nothing-like-this") == []


# --------------------------------------------------------------------------
# READING
# --------------------------------------------------------------------------

def test_a_binary_file_says_so_rather_than_returning_mojibake(cfg):
    rel = attachments.store(cfg, "m1", "scan.pdf", b"%PDF-1.4\x00\x01\x02",
                            attachments.DIRECTION_IN, sender=ALICE)
    with pytest.raises(ToolError) as exc:
        attachments.read(cfg, rel)
    assert "binary, not readable as text" in str(exc.value)


def test_invalid_utf8_is_binary_too(cfg):
    rel = attachments.store(cfg, "m1", "weird.txt", b"\xff\xfe\xfd",
                            attachments.DIRECTION_IN, sender=ALICE)
    with pytest.raises(ToolError) as exc:
        attachments.read(cfg, rel)
    assert "binary" in str(exc.value)


def test_a_very_large_text_file_is_truncated(tmp_path):
    cfg = _cfg(tmp_path, max_inline_attachment_chars=50)
    rel = attachments.store_text(cfg, "m1", "long.txt", "z" * 500,
                                 attachments.DIRECTION_IN, sender=ALICE)
    out = attachments.read(cfg, rel, max_chars=50)
    assert out.startswith("z" * 50)
    assert "truncated at 50 characters" in out


@pytest.mark.parametrize("escape", [
    "../../../etc/passwd",
    "/etc/passwd",
    "m1/../../outside.txt",
])
def test_reads_cannot_escape_the_store(cfg, escape, tmp_path):
    attachments.store(cfg, "m1", "ok.txt", b"fine",
                      attachments.DIRECTION_IN, sender=ALICE)
    (tmp_path / "outside.txt").write_text("SECRET")
    with pytest.raises(ToolError):
        attachments.read(cfg, escape)


def test_a_symlink_out_of_the_store_is_refused(cfg, tmp_path):
    attachments.store(cfg, "m1", "ok.txt", b"fine",
                      attachments.DIRECTION_IN, sender=ALICE)
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET")
    (attachments.root(cfg) / "m1" / "escape.txt").symlink_to(secret)
    with pytest.raises(ToolError):
        attachments.read(cfg, "m1/escape.txt")


def test_the_index_is_not_an_attachment(cfg):
    attachments.store(cfg, "m1", "ok.txt", b"fine",
                      attachments.DIRECTION_IN, sender=ALICE)
    with pytest.raises(ToolError) as exc:
        attachments.read(cfg, attachments.INDEX_NAME)
    assert attachments.LIST_TOOL in str(exc.value)


# --------------------------------------------------------------------------
# SCOPE
# --------------------------------------------------------------------------

def test_a_scoped_correspondent_sees_only_their_own_files(cfg):
    mine = attachments.store(cfg, "m1", "mine.txt", b"mine",
                             attachments.DIRECTION_IN, sender=ALICE)
    theirs = attachments.store(cfg, "m2", "theirs.txt", b"theirs",
                               attachments.DIRECTION_IN, sender=BOB)

    assert [s.path for s in attachments.stored(cfg, restrict_to_sender=ALICE)] \
        == [mine]
    assert attachments.read(cfg, mine, restrict_to_sender=ALICE) == "mine"
    with pytest.raises(ToolError) as exc:
        attachments.read(cfg, theirs, restrict_to_sender=ALICE)
    # Same sentence as a file that isn't there: whose file it is, is not
    # something to tell them.
    assert str(exc.value) == "No stored attachment at that path."

    # Unscoped — what a trusted sender gets — sees both.
    assert len(attachments.stored(cfg)) == 2


def test_an_unattributable_file_is_hidden_from_a_scoped_correspondent(cfg):
    """A file stored before the index existed has no owner on record.
    Unattributed means hidden, not shared."""
    attachments.store(cfg, "m1", "orphan.txt", b"x",
                      attachments.DIRECTION_IN, sender=ALICE)
    (attachments.root(cfg) / attachments.INDEX_NAME).unlink()

    assert attachments.stored(cfg, restrict_to_sender=ALICE) == []
    with pytest.raises(ToolError):
        attachments.read(cfg, "m1/orphan.txt", restrict_to_sender=ALICE)
    # Still visible, and still readable, to a sender with full scope.
    assert [s.filename for s in attachments.stored(cfg)] == ["orphan.txt"]
    assert attachments.read(cfg, "m1/orphan.txt") == "x"


# --------------------------------------------------------------------------
# THE TOOLS
# --------------------------------------------------------------------------

def test_the_schemas_describe_their_own_scope():
    scoped = attachments.schemas(scoped_to_sender=True)
    everyone = attachments.schemas(scoped_to_sender=False)
    assert [s["name"] for s in scoped] == [attachments.LIST_TOOL,
                                           attachments.READ_TOOL]
    assert "only attachments from your correspondence" \
        in scoped[0]["description"].lower()
    assert "every correspondent" in everyone[0]["description"].lower()
    for schema in scoped + everyone:
        assert schema["input_schema"]["type"] == "object"


def test_listing_and_reading_through_the_tools(cfg):
    rel = attachments.store_text(cfg, "m1", "plan.md", "# the plan\n",
                                 attachments.DIRECTION_IN, sender=ALICE,
                                 subject="planning")

    text, is_error = attachments.handle(cfg, attachments.LIST_TOOL,
                                        {"query": "plan"})
    assert not is_error
    assert rel in text and "incoming" in text and "planning" in text

    text, is_error = attachments.handle(cfg, attachments.READ_TOOL,
                                        {"path": rel})
    assert not is_error
    assert text == "# the plan\n"


def test_the_tools_report_refusals_as_errors_not_exceptions(cfg):
    attachments.store(cfg, "m1", "ok.txt", b"fine",
                      attachments.DIRECTION_IN, sender=ALICE)

    text, is_error = attachments.handle(cfg, attachments.READ_TOOL,
                                        {"path": "../../../etc/passwd"})
    assert is_error and text

    text, is_error = attachments.handle(cfg, attachments.READ_TOOL, {"path": ""})
    assert is_error and text

    text, is_error = attachments.handle(cfg, "some_other_tool", {})
    assert is_error


def test_an_empty_store_is_an_answer_not_a_failure(cfg):
    text, is_error = attachments.handle(cfg, attachments.LIST_TOOL, {})
    assert not is_error
    assert text == "No stored attachments match."

    text, is_error = attachments.handle(cfg, attachments.READ_TOOL,
                                        {"path": "m1/whatever.txt"})
    assert is_error
    assert "attachment store" in text


def test_a_corrupt_index_costs_metadata_and_not_files(cfg):
    rel = attachments.store(cfg, "m1", "notes.txt", b"kept",
                            attachments.DIRECTION_IN, sender=ALICE)
    index = attachments.root(cfg) / attachments.INDEX_NAME
    index.write_text("not json at all\n{\"half\": \n", encoding="utf-8")

    listed = attachments.stored(cfg)
    assert [s.path for s in listed] == [rel]
    assert listed[0].direction == ""
    assert attachments.read(cfg, rel) == "kept"


def test_the_index_records_what_the_listing_reports(cfg):
    attachments.store(cfg, "m1", "notes.txt", b"kept",
                      attachments.DIRECTION_IN, sender="ALICE@Example.com",
                      subject="hello")
    lines = (attachments.root(cfg) / attachments.INDEX_NAME) \
        .read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["sender"] == ALICE          # lowercased for matching
    assert record["direction"] == attachments.DIRECTION_IN
    assert record["size"] == 4
    assert record["message_id"] == "m1"


# --------------------------------------------------------------------------
# THE SHEET COLUMN
# --------------------------------------------------------------------------

class FakeSheets:
    """Enough of the Sheets service to see what was written where."""

    def __init__(self):
        self.updates = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def update(self, spreadsheetId, range, valueInputOption, body):
        self.updates.append((range, body["values"]))
        return type("X", (), {"execute": lambda self: {}})()


def _store():
    sheets = FakeSheets()
    store = sheets_queue.SheetStore(sheets, None,
                                    type("C", (), {"max_body_chars": 45000})())
    store.queue_id = "qid"
    return store, sheets


def test_the_attachments_column_is_last():
    """Appended, never inserted: `_ensure_headers` upgrades an old sheet
    by adding missing columns at the end, and `set_status` addresses
    status..note as one contiguous range."""
    assert sheets_queue.QUEUE_HEADERS[-1] == "attachments"
    assert sheets_queue.QUEUE_HEADERS.index("note") \
        == sheets_queue.QUEUE_HEADERS.index("status") + 1


def test_set_attachments_writes_one_cell():
    store, sheets = _store()
    row = {"gmail_msg_id": "m1", "_row": 7}
    store.set_attachments(row, ["m1/a.txt", "m1/b.txt"])
    assert row["attachments"] == "m1/a.txt, m1/b.txt"
    assert sheets.updates == [("Queue!J7", [["m1/a.txt, m1/b.txt"]])]


def test_a_row_from_before_the_column_existed_is_left_alone(caplog):
    """An old row read back has no `_row` only if something went wrong,
    but an old *sheet* simply has no such column — `_load` pads short
    rows, so reading one gives "" rather than a KeyError."""
    store, sheets = _store()
    old_row = {"gmail_msg_id": "m1", "status": "replied"}   # no _row
    with caplog.at_level("WARNING"):
        store.set_attachments(old_row, ["m1/a.txt"])
    assert sheets.updates == []
    assert "cannot record attachments" in caplog.text
    assert old_row.get("attachments") == "m1/a.txt"


class OldSchemaSheets(FakeSheets):
    """A Queue tab as it was before the `attachments` column existed."""

    HEADERS = sheets_queue.QUEUE_HEADERS[:-1]
    ROW = ["m1", "t1", "Alice", ALICE, "hi", "body", "2026-09-01", "replied", ""]

    def get(self, spreadsheetId, range):
        return type("X", (), {
            "execute": lambda _self: {"values": [self.HEADERS, self.ROW]}
        })()


def test_an_old_row_read_back_survives_the_new_column():
    """A sheet written before this change has one column fewer. Reading it
    must not raise and must not shift any value into the wrong field."""
    sheets = OldSchemaSheets()
    store = sheets_queue.SheetStore(sheets, None,
                                    type("C", (), {"max_body_chars": 45000})())
    store.queue_id = "qid"
    rows = store.queue_rows()
    assert len(rows) == 1
    assert rows[0]["sender_addr"] == ALICE
    assert rows[0]["status"] == "replied"
    assert rows[0].get("attachments", "") == ""

    # And it can still be updated in place, gaining the column it lacked.
    store.set_attachments(rows[0], ["m1/a.txt"])
    assert sheets.updates == [("Queue!J2", [["m1/a.txt"]])]
