"""
tests/test_cost_export.py

The CSV export of the spend ledger. Where test_costs.py is about the
numbers being right, this is about the rows arriving intact: the window
that was asked for, an honest account of anything left out, and CSV that
a spreadsheet will actually parse back into the columns it came from.

The failure this guards against is a quiet one. An export that silently
drops half the rows still looks like an export — the person reading it
has no way to tell it is short — so every bound here has to announce
itself in the preamble, and that is asserted rather than assumed.

Nothing here touches the network: the Sheets service is a fake that
hands back a literal values array.

This directory is protected. Indi can read it but not edit it.
"""

import csv
import datetime
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import claude_api
import sheets_queue


# --------------------------------------------------------------------------
# A FAKE COSTS TAB
# --------------------------------------------------------------------------

class FakeSheets:
    """Enough of the Sheets service to serve one tab's values.

    `fail` makes the read raise the way an unreachable API or a missing
    tab does, so the degradation path is exercised for real rather than
    by monkeypatching the method under test.
    """

    def __init__(self, values, fail=False):
        self.values_returned = values
        self.fail = fail
        self.reads = 0

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, spreadsheetId, range):
        return self

    def execute(self):
        self.reads += 1
        if self.fail:
            raise RuntimeError("sheets unavailable")
        return {"values": self.values_returned}


def _store(values, fail=False):
    sheets = FakeSheets(values, fail=fail)
    store = sheets_queue.SheetStore(sheets, None,
                                    type("C", (), {"max_body_chars": 45000})())
    store.responses_id = "rid"
    return store, sheets


def _iso(hours_ago: float) -> str:
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        hours=hours_ago
    )
    return when.isoformat()


def _row(hours_ago, kind="reply", model="claude-opus-5", usd="0.0100",
         note="someone@example.com"):
    """One Costs row in sheet order, as a list of cell values."""
    return [_iso(hours_ago), kind, model, "100", "200", "0", "0", "0", usd, note]


def _values(rows):
    return [list(sheets_queue.COSTS_HEADERS)] + list(rows)


def _csv_body(text):
    """The CSV out of a tool result: everything between the markers."""
    assert claude_api.UNTRUSTED_OPEN in text, text
    body = text.split(claude_api.UNTRUSTED_OPEN, 1)[1]
    body = body.rsplit(claude_api.UNTRUSTED_CLOSE, 1)[0]
    return body.lstrip("\n")


def _parsed(text):
    """(header, data rows) as a spreadsheet would read them back."""
    rows = list(csv.reader(io.StringIO(_csv_body(text))))
    return rows[0], rows[1:]


# --------------------------------------------------------------------------
# READING THE TAB
# --------------------------------------------------------------------------

def test_cost_table_returns_the_sheets_own_header_row():
    store, _ = _store(_values([_row(1)]))
    headers, rows = store.cost_table()
    assert headers == sheets_queue.COSTS_HEADERS
    assert len(rows) == 1
    assert rows[0]["kind"] == "reply"


def test_cost_table_carries_a_column_the_sheet_gained():
    """`_ensure_headers` upgrades a live sheet by appending columns, so
    the export has to follow the sheet rather than the declared list."""
    headers = list(sheets_queue.COSTS_HEADERS) + ["run_id"]
    store, _ = _store([headers, _row(1) + ["run-7"]])
    got_headers, rows = store.cost_table()
    assert got_headers == headers
    assert rows[0]["run_id"] == "run-7"


def test_cost_table_reads_the_tab_once_for_rows_and_headers():
    store, sheets = _store(_values([_row(1)]))
    store.cost_table()
    store.cost_table()
    assert sheets.reads == 1


def test_cost_table_degrades_to_empty_when_sheets_is_unavailable(caplog):
    store, _ = _store([], fail=True)
    with caplog.at_level("WARNING"):
        assert store.cost_table() == ([], [])
    assert "Costs" in caplog.text


def test_cost_table_on_an_empty_tab_is_empty_not_a_bare_header():
    store, _ = _store([])
    assert store.cost_table() == ([], [])


# --------------------------------------------------------------------------
# THE WINDOW
# --------------------------------------------------------------------------

def test_only_rows_inside_the_window_are_exported():
    store, _ = _store(_values([
        _row(100, note="old@example.com"),
        _row(2, note="recent@example.com"),
    ]))
    text = claude_api._run_cost_ledger_csv(store, {"hours": 24},
                                           include_notes=True)
    _, rows = _parsed(text)
    assert [r[-1] for r in rows] == ["recent@example.com"]


def test_rows_come_back_oldest_first():
    store, _ = _store(_values([
        _row(2, note="newer@example.com"),
        _row(5, note="older@example.com"),
    ]))
    text = claude_api._run_cost_ledger_csv(store, {}, include_notes=True)
    _, rows = _parsed(text)
    assert [r[-1] for r in rows] == ["older@example.com", "newer@example.com"]


def test_the_window_is_capped_at_the_same_ceiling_as_the_aggregate():
    store, _ = _store(_values([_row(1)]))
    text = claude_api._run_cost_ledger_csv(store, {"hours": 10 ** 6},
                                           include_notes=True)
    assert f"last {claude_api.MAX_LEDGER_HOURS}h" in text


def test_a_nonsense_hours_value_falls_back_to_the_default():
    store, _ = _store(_values([_row(1)]))
    text = claude_api._run_cost_ledger_csv(store, {"hours": "lots"},
                                           include_notes=True)
    assert f"last {claude_api.DEFAULT_LEDGER_HOURS}h" in text


def test_an_undated_row_is_counted_in_the_preamble_not_exported():
    bad = _row(1)
    bad[0] = "not a date"
    store, _ = _store(_values([bad, _row(1)]))
    text = claude_api._run_cost_ledger_csv(store, {}, include_notes=True)
    _, rows = _parsed(text)
    assert len(rows) == 1
    assert "1 ledger row(s) have an unreadable timestamp" in text


# --------------------------------------------------------------------------
# LIMIT AND TRUNCATION
# --------------------------------------------------------------------------

def test_the_limit_keeps_the_newest_rows_and_says_what_it_dropped():
    store, _ = _store(_values([
        _row(5, note="a@example.com"),
        _row(4, note="b@example.com"),
        _row(3, note="c@example.com"),
    ]))
    text = claude_api._run_cost_ledger_csv(store, {"limit": 2},
                                           include_notes=True)
    _, rows = _parsed(text)
    assert [r[-1] for r in rows] == ["b@example.com", "c@example.com"]
    assert "1 older row(s) in the window were left out" in text


def test_the_limit_is_hard_capped():
    store, _ = _store(_values([_row(1)]))
    assert claude_api._ledger_csv_limit({"limit": 10 ** 9}) \
        == claude_api.MAX_LEDGER_CSV_ROWS
    assert claude_api._ledger_csv_limit({"limit": 0}) \
        == claude_api.DEFAULT_LEDGER_CSV_ROWS
    assert claude_api._ledger_csv_limit({"limit": -5}) == 1
    assert claude_api._ledger_csv_limit({}) == claude_api.DEFAULT_LEDGER_CSV_ROWS


def test_an_oversized_export_is_truncated_loudly_not_silently():
    # Each note is padded out so that the character cap, not the row cap,
    # is what bites.
    padding = "x" * 400
    rows = [_row(h, note=f"{padding}{h}@example.com") for h in range(300, 0, -1)]
    store, _ = _store(_values(rows))
    text = claude_api._run_cost_ledger_csv(store, {"hours": 400},
                                           include_notes=True)
    header, data = _parsed(text)
    assert len(data) < len(rows)
    assert len(_csv_body(text)) <= claude_api.MAX_LEDGER_CSV_CHARS
    assert "This export is truncated" in text
    # Truncation eats the old end, so the most recent row must survive.
    assert data[-1][-1].endswith("1@example.com")


def test_the_preamble_states_the_row_count_and_the_estimate_caveat():
    store, _ = _store(_values([_row(1), _row(2)]))
    text = claude_api._run_cost_ledger_csv(store, {}, include_notes=True)
    preamble = text.split(claude_api.UNTRUSTED_OPEN)[0]
    assert "2 ledger row(s) as CSV" in preamble
    assert "not the invoice" in preamble
    assert "last 24h" in preamble


# --------------------------------------------------------------------------
# CSV WELL-FORMEDNESS
# --------------------------------------------------------------------------

def test_the_header_is_the_real_ledger_header():
    store, _ = _store(_values([_row(1)]))
    header, _ = _parsed(claude_api._run_cost_ledger_csv(store, {},
                                                        include_notes=True))
    assert header == sheets_queue.COSTS_HEADERS


def test_commas_and_quotes_in_a_cell_survive_the_round_trip():
    nasty = 'Steve "The Boss", Yegge <steve@example.com>'
    store, _ = _store(_values([_row(1, note=nasty)]))
    header, rows = _parsed(claude_api._run_cost_ledger_csv(store, {},
                                                           include_notes=True))
    assert len(rows) == 1
    assert dict(zip(header, rows[0]))["note"] == nasty


def test_a_newline_in_a_cell_does_not_become_an_extra_row():
    store, _ = _store(_values([_row(1, note="line one\nline two")]))
    header, rows = _parsed(claude_api._run_cost_ledger_csv(store, {},
                                                           include_notes=True))
    assert len(rows) == 1
    assert dict(zip(header, rows[0]))["note"] == "line one\nline two"


def test_a_short_row_from_an_older_schema_exports_as_empty_cells():
    """`_load` pads short rows; the export must keep the padding rather
    than shifting values left into the wrong column."""
    short = _row(1)[:-1]                       # written before `note` existed
    store, _ = _store(_values([short]))
    header, rows = _parsed(claude_api._run_cost_ledger_csv(store, {},
                                                           include_notes=True))
    record = dict(zip(header, rows[0]))
    assert record["usd"] == "0.0100"
    assert record["note"] == ""


def test_the_bookkeeping_row_number_is_not_a_column():
    store, _ = _store(_values([_row(1)]))
    header, _ = _parsed(claude_api._run_cost_ledger_csv(store, {},
                                                        include_notes=True))
    assert "_row" not in header


# --------------------------------------------------------------------------
# NOTHING TO EXPORT
# --------------------------------------------------------------------------

def test_an_empty_sheet_gives_a_sentence_not_an_empty_csv():
    store, _ = _store([])
    text = claude_api._run_cost_ledger_csv(store, {}, include_notes=True)
    assert claude_api.UNTRUSTED_OPEN not in text
    assert "no rows to export" in text


def test_an_unreadable_sheet_gives_a_sentence_and_does_not_raise():
    store, _ = _store([], fail=True)
    text = claude_api._run_cost_ledger_csv(store, {}, include_notes=True)
    assert claude_api.UNTRUSTED_OPEN not in text
    assert "no rows to export" in text


def test_a_store_that_raises_on_read_is_reported_not_propagated():
    class Exploding:
        def cost_table(self):
            raise RuntimeError("boom")

    text = claude_api._run_cost_ledger_csv(Exploding(), {}, include_notes=True)
    assert "Could not read the cost ledger" in text


def test_an_empty_window_says_how_many_rows_exist_on_file():
    store, _ = _store(_values([_row(500)]))
    text = claude_api._run_cost_ledger_csv(store, {"hours": 24},
                                           include_notes=True)
    assert claude_api.UNTRUSTED_OPEN not in text
    assert "no rows in the last 24h" in text


# --------------------------------------------------------------------------
# SCOPE: THE NOTE COLUMN AND WHO IS OFFERED THE TOOL
# --------------------------------------------------------------------------

def test_the_note_column_is_withheld_from_a_sender_without_full_scope():
    store, _ = _store(_values([_row(1, note="alice@example.com")]))
    text = claude_api._run_cost_ledger_csv(store, {}, include_notes=False)
    header, rows = _parsed(text)
    assert "note" not in header
    assert "alice@example.com" not in text
    assert "note column is omitted" in text


def _tools(cfg, **kw):
    params = dict(trusted=True, have_store=True, scope_all_history=True)
    params.update(kw)
    tools, _ = claude_api.build_tools(cfg, **params)
    return {t.get("name") for t in tools}


@pytest.fixture
def cfg():
    return type("C", (), {
        "enable_web_search": False, "enable_web_fetch": False,
        "enable_code_execution": False, "enable_history_search": False,
        "store_attachments": False, "enable_outbound": False,
        "web_search_tool_type": "web_search_20250305",
        "web_fetch_tool_type": "web_fetch_20250910",
        "code_execution_tool_type": "code_execution_20250825",
        "web_fetch_allowed_domains": (), "max_searches_per_reply": 1,
        "max_fetches_per_reply": 1,
        "betas": lambda self=None, **kw: [],
    })()


def test_the_export_is_offered_to_exactly_the_ledger_readers(cfg):
    """Same gate, not a similar one: any sender who is offered one of the
    two must be offered the other, and no sender may get only the CSV."""
    for trusted in (True, False):
        for have_store in (True, False):
            for scope in (True, False):
                names = _tools(cfg, trusted=trusted, have_store=have_store,
                               scope_all_history=scope)
                assert (claude_api.COST_LEDGER_CSV_TOOL_NAME in names) == \
                    (claude_api.COST_LEDGER_TOOL_NAME in names), (
                        f"gates diverge at trusted={trusted}, "
                        f"have_store={have_store}, scope={scope}"
                    )


def test_an_untrusted_sender_is_offered_neither(cfg):
    names = _tools(cfg, trusted=False)
    assert claude_api.COST_LEDGER_CSV_TOOL_NAME not in names
    assert claude_api.COST_LEDGER_TOOL_NAME not in names


def test_a_trusted_sender_is_offered_both(cfg):
    names = _tools(cfg)
    assert claude_api.COST_LEDGER_CSV_TOOL_NAME in names
    assert claude_api.COST_LEDGER_TOOL_NAME in names


# --------------------------------------------------------------------------
# THE DESCRIPTION
# --------------------------------------------------------------------------

def test_the_description_points_at_the_aggregate_for_summary_questions():
    schema = claude_api.cost_ledger_csv_schema(include_notes=True)
    assert claude_api.COST_LEDGER_TOOL_NAME in schema["description"]


def test_the_description_warns_that_the_rows_name_correspondents():
    with_notes = claude_api.cost_ledger_csv_schema(include_notes=True)
    without = claude_api.cost_ledger_csv_schema(include_notes=False)
    assert "email addresses" in with_notes["description"]
    assert "note column" in without["description"]
    for schema in (with_notes, without):
        assert schema["name"] == claude_api.COST_LEDGER_CSV_TOOL_NAME
        assert set(schema["input_schema"]["properties"]) == {"hours", "limit"}
