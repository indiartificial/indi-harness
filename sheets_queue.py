"""
sheets_queue.py

Google Sheets as the queue/response store, rewritten as a `SheetStore`
object that loads each tab once per run.

What changed and why:

  * **No more counted integer IDs.** The old `next_id()` read the sheet,
    counted rows and added one — a read-then-write race that produces
    duplicate IDs under overlapping cron runs, and that silently corrupts
    the queue<->response linkage when it does. The Gmail message id is
    already a unique, stable key, so responses now link by
    `gmail_msg_id` directly. This also removes the latent bug where the
    responses window was filtered on the response's own `id` rather than
    on `queue_row_id`.

  * **A `status` column.** Idempotency is now keyed on *completion*, not
    on row existence. The old code appended the queue row before
    generating a reply and, on any exception, left the message unread with
    a queue row present — so the next run's duplicate check skipped it
    *and* never marked it read, stranding that message forever and
    re-fetching it on every run from then on.

  * **One read per tab per run.** The old module read the full sheet
    (every email body ever processed) roughly eight to ten times per
    message. Now each tab is fetched once and kept in memory, with local
    appends applied to the cache.

  * **Bodies are truncated before writing.** A single Sheets cell caps at
    50,000 characters; one long email would otherwise fail the write and
    abort the run.
"""

from __future__ import annotations

import datetime
import logging
import re

log = logging.getLogger("indi.sheets")

QUEUE_TITLE = "Indi Queue"
QUEUE_TAB = "Queue"
# `attachments` is last on purpose. `_ensure_headers` upgrades an existing
# sheet by appending whatever columns it is missing, so a new column at the
# end is the only kind that leaves every existing row's values where they
# are — and `set_status` writes the status..note range by index, which
# stays contiguous only while nothing is inserted before it.
QUEUE_HEADERS = [
    "gmail_msg_id", "thread_id", "sender", "sender_addr", "subject",
    "body", "received_at", "status", "note", "attachments",
]

RESPONSES_TITLE = "Indi Responses"
RESPONSES_TAB = "Responses"
RESPONSES_HEADERS = ["gmail_msg_id", "sender_addr", "response_text", "created_at"]

# Status values written to the Queue sheet's `status` column.
STATUS_QUEUED = "queued"     # row created, reply not yet produced
STATUS_REPLIED = "replied"   # reply drafted/sent successfully
STATUS_SKIPPED = "skipped"   # deliberately not replied to (automated, blocked)
STATUS_FAILED = "failed"     # an error occurred; eligible for retry
STATUS_IGNORED = "ignored"   # Indi read it and chose not to reply

TERMINAL_STATUSES = {STATUS_REPLIED, STATUS_SKIPPED, STATUS_IGNORED}

# Indi's own outbound-initiated messages and its journal entries. Both are
# separate tabs in the Responses spreadsheet so the reply pipeline and the
# initiative pipeline can't be confused for one another.
OUTBOUND_TAB = "Outbound"
OUTBOUND_HEADERS = ["recipient", "subject", "body", "rationale", "created_at"]

JOURNAL_TAB = "Journal"
JOURNAL_HEADERS = ["entry", "created_at"]

# One row per Claude call, priced by costs.py. Kept on the Responses
# spreadsheet so the whole ledger is one file to open.
COSTS_TAB = "Costs"
COSTS_HEADERS = [
    "at", "kind", "model", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_write_tokens", "web_searches", "usd", "note",
]

_RANGE_ROW = re.compile(r"![A-Z]+(\d+)")


def _now_iso() -> str:
    # datetime.utcnow() is deprecated in 3.12+ and returns a naive value.
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _escape_drive_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _col_letter(index_zero_based: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    result = ""
    n = index_zero_based
    while True:
        result = chr(ord("A") + (n % 26)) + result
        n = n // 26 - 1
        if n < 0:
            return result


class SheetStore:
    def __init__(self, sheets_service, drive_service, cfg):
        self.sheets = sheets_service
        self.drive = drive_service
        self.cfg = cfg
        self.queue_id: str | None = None
        self.responses_id: str | None = None
        self._cache: dict[str, list[dict]] = {}
        # The header row each tab actually had when it was read, kept
        # alongside the parsed rows. `_load` throws the order away by
        # turning rows into dicts, and an exporter needs the columns in
        # the order and spelling the sheet holds them, not the order this
        # module happens to declare.
        self._headers: dict[str, list[str]] = {}

    # -- spreadsheet lookup / creation ------------------------------------

    def _find_spreadsheet(self, title: str) -> str | None:
        q = (
            f"name = '{_escape_drive_query(title)}' and "
            f"mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false"
        )
        files = (
            self.drive.files()
            .list(q=q, spaces="drive", fields="files(id, name)")
            .execute()
            .get("files", [])
        )
        if len(files) > 1:
            # Picking files[0] blindly is how you end up silently writing
            # to a second, forked copy of your history.
            log.warning(
                "Found %d spreadsheets named %r — using %s. Remove the "
                "duplicates or the history will fork.",
                len(files), title, files[0]["id"],
            )
        return files[0]["id"] if files else None

    def _create_spreadsheet(self, title: str, tab: str, headers: list[str]) -> str:
        created = (
            self.sheets.spreadsheets()
            .create(
                body={"properties": {"title": title},
                      "sheets": [{"properties": {"title": tab}}]},
                fields="spreadsheetId",
            )
            .execute()
        )
        sid = created["spreadsheetId"]
        self.sheets.spreadsheets().values().update(
            spreadsheetId=sid,
            range=f"{tab}!A1",
            valueInputOption="RAW",
            body={"values": [headers]},
        ).execute()
        log.info("Created spreadsheet %r (%s)", title, sid)
        return sid

    def ensure_spreadsheets(self) -> None:
        self.queue_id = self._find_spreadsheet(QUEUE_TITLE) or self._create_spreadsheet(
            QUEUE_TITLE, QUEUE_TAB, QUEUE_HEADERS
        )
        self.responses_id = self._find_spreadsheet(RESPONSES_TITLE) or self._create_spreadsheet(
            RESPONSES_TITLE, RESPONSES_TAB, RESPONSES_HEADERS
        )
        self._ensure_headers(self.queue_id, QUEUE_TAB, QUEUE_HEADERS)
        self._ensure_headers(self.responses_id, RESPONSES_TAB, RESPONSES_HEADERS)
        self._ensure_tab(self.responses_id, OUTBOUND_TAB, OUTBOUND_HEADERS)
        self._ensure_tab(self.responses_id, JOURNAL_TAB, JOURNAL_HEADERS)
        self._ensure_tab(self.responses_id, COSTS_TAB, COSTS_HEADERS)

    def _ensure_tab(self, spreadsheet_id: str, tab: str, headers: list[str]) -> None:
        """Create a tab if it isn't there yet, then make sure its headers
        are current. Lets an existing deployment gain new tabs without
        anyone hand-editing the spreadsheet."""
        meta = self.sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="sheets.properties.title"
        ).execute()
        titles = {sh["properties"]["title"] for sh in meta.get("sheets", [])}
        if tab not in titles:
            self.sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
            ).execute()
            self.sheets.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{tab}!A1",
                valueInputOption="RAW",
                body={"values": [headers]},
            ).execute()
            log.info("Created tab %r", tab)
            return
        self._ensure_headers(spreadsheet_id, tab, headers)

    def _ensure_headers(self, spreadsheet_id: str, tab: str, headers: list[str]) -> None:
        """Adds any missing columns to an existing sheet, so upgrading from
        the old schema doesn't require hand-editing."""
        current = (
            self.sheets.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"{tab}!A1:Z1")
            .execute()
            .get("values", [[]])
        )
        existing = current[0] if current else []
        if existing == headers:
            return
        merged = list(existing)
        for h in headers:
            if h not in merged:
                merged.append(h)
        if merged != existing:
            self.sheets.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{tab}!A1",
                valueInputOption="RAW",
                body={"values": [merged]},
            ).execute()
            log.info("Extended %s headers to: %s", tab, ", ".join(merged))

    # -- cached reads ------------------------------------------------------

    def _load(self, spreadsheet_id: str, tab: str) -> list[dict]:
        """Read a tab once per run. Each returned dict carries `_row`, the
        1-based sheet row number, so a cell can be updated in place."""
        key = f"{spreadsheet_id}:{tab}"
        if key in self._cache:
            return self._cache[key]

        values = (
            self.sheets.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"{tab}!A1:Z")
            .execute()
            .get("values", [])
        )
        rows: list[dict] = []
        headers: list[str] = []
        if values:
            headers = values[0]
            for offset, row in enumerate(values[1:], start=2):
                if not row:
                    continue
                padded = list(row) + [""] * (len(headers) - len(row))
                record = dict(zip(headers, padded))
                record["_row"] = offset
                rows.append(record)
        self._cache[key] = rows
        self._headers[key] = list(headers)
        log.debug("Loaded %d rows from %s", len(rows), tab)
        return rows

    def tab_headers(self, spreadsheet_id: str, tab: str) -> list[str]:
        """The header row of a tab, as the sheet spells it.

        Loads the tab if it hasn't been read yet, so this costs nothing
        extra when the rows are wanted too. An empty list means the tab
        is empty or was never reachable — callers decide what to fall
        back to rather than getting a guess from here.
        """
        key = f"{spreadsheet_id}:{tab}"
        if key not in self._headers:
            self._load(spreadsheet_id, tab)
        return list(self._headers.get(key) or [])

    def queue_rows(self) -> list[dict]:
        return self._load(self.queue_id, QUEUE_TAB)

    def response_rows(self) -> list[dict]:
        return self._load(self.responses_id, RESPONSES_TAB)

    # -- writes ------------------------------------------------------------

    def _append(self, spreadsheet_id: str, tab: str, headers: list[str],
                record: dict) -> int:
        """Append a record and return its real sheet row number, taken from
        the API's `updates.updatedRange`. No counting, no race."""
        values = [str(record.get(h, "")) for h in headers]
        result = (
            self.sheets.spreadsheets()
            .values()
            .append(
                spreadsheetId=spreadsheet_id,
                range=f"{tab}!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                includeValuesInResponse=False,
                body={"values": [values]},
            )
            .execute()
        )
        updated_range = result.get("updates", {}).get("updatedRange", "")
        match = _RANGE_ROW.search(updated_range)
        row_number = int(match.group(1)) if match else -1

        cached = self._cache.get(f"{spreadsheet_id}:{tab}")
        if cached is not None:
            stored = dict(record)
            stored["_row"] = row_number
            cached.append(stored)
        return row_number

    def find_queue_row(self, gmail_msg_id: str) -> dict | None:
        for row in self.queue_rows():
            if row.get("gmail_msg_id") == gmail_msg_id:
                return row
        return None

    def append_queue_row(self, msg: dict, status: str = STATUS_QUEUED,
                         note: str = "") -> dict:
        body = msg.get("body", "")
        if len(body) > self.cfg.max_body_chars:
            body = body[: self.cfg.max_body_chars] + "\n[... truncated for storage ...]"
        record = {
            "gmail_msg_id": msg["gmail_msg_id"],
            "thread_id": msg["thread_id"],
            "sender": msg["from_raw"][:500],
            "sender_addr": msg["from_addr"],
            "subject": msg["subject"][:500],
            "body": body,
            # The message's own timestamp, not the time we happened to
            # process it — the old version recorded processing time, which
            # scrambled "recent" ordering whenever a backlog was drained.
            "received_at": _received_at_iso(msg),
            "status": status,
            "note": note[:500],
            # Filled in by set_attachments once the files are on disk:
            # incoming ones are stored after the row exists, and outgoing
            # ones only exist after the reply has gone out.
            "attachments": "",
        }
        row_number = self._append(self.queue_id, QUEUE_TAB, QUEUE_HEADERS, record)
        record["_row"] = row_number
        return record

    def set_status(self, queue_row: dict, status: str, note: str = "") -> None:
        row_number = queue_row.get("_row", -1)
        queue_row["status"] = status
        if note:
            queue_row["note"] = note[:500]
        if row_number <= 0:
            log.warning("No row number for %s; cannot update status",
                        queue_row.get("gmail_msg_id"))
            return
        status_col = _col_letter(QUEUE_HEADERS.index("status"))
        note_col = _col_letter(QUEUE_HEADERS.index("note"))
        self.sheets.spreadsheets().values().update(
            spreadsheetId=self.queue_id,
            range=f"{QUEUE_TAB}!{status_col}{row_number}:{note_col}{row_number}",
            valueInputOption="RAW",
            body={"values": [[status, (note or queue_row.get("note", ""))[:500]]]},
        ).execute()

    def set_attachments(self, queue_row: dict, paths: list[str]) -> None:
        """Record which stored attachments belong to this message.

        Rows written before this column existed simply have no value in
        it; `_load` pads short rows, so reading one back gives "" rather
        than a KeyError, and nothing else in the harness reads the column
        at all. The value is a comma-separated list of store paths, which
        is what `read_stored_attachment` takes.
        """
        value = ", ".join(paths)[: self.cfg.max_body_chars]
        queue_row["attachments"] = value
        row_number = queue_row.get("_row", -1)
        if row_number <= 0:
            log.warning("No row number for %s; cannot record attachments",
                        queue_row.get("gmail_msg_id"))
            return
        column = _col_letter(QUEUE_HEADERS.index("attachments"))
        self.sheets.spreadsheets().values().update(
            spreadsheetId=self.queue_id,
            range=f"{QUEUE_TAB}!{column}{row_number}",
            valueInputOption="RAW",
            body={"values": [[value]]},
        ).execute()

    def append_response(self, gmail_msg_id: str, sender_addr: str, text: str) -> None:
        if len(text) > self.cfg.max_body_chars:
            text = text[: self.cfg.max_body_chars] + "\n[... truncated for storage ...]"
        self._append(
            self.responses_id, RESPONSES_TAB, RESPONSES_HEADERS,
            {
                "gmail_msg_id": gmail_msg_id,
                "sender_addr": sender_addr,
                "response_text": text,
                "created_at": _now_iso(),
            },
        )

    # -- queries -----------------------------------------------------------

    def responses_by_message(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for row in self.response_rows():
            out.setdefault(row.get("gmail_msg_id", ""), []).append(
                row.get("response_text", "")
            )
        return out

    def messages_for_sender(self, sender_addr: str) -> list[dict]:
        """That sender's replied-to messages, oldest first."""
        rows = [
            r for r in self.queue_rows()
            if r.get("sender_addr", "").lower() == sender_addr.lower()
            and r.get("status") == STATUS_REPLIED
        ]
        rows.sort(key=lambda r: r.get("received_at", ""))
        return rows

    def replies_in_last_day(self, sender_addr: str) -> int:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
        count = 0
        for row in self.response_rows():
            if row.get("sender_addr", "").lower() != sender_addr.lower():
                continue
            try:
                created = datetime.datetime.fromisoformat(row.get("created_at", ""))
            except ValueError:
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=datetime.timezone.utc)
            if created >= cutoff:
                count += 1
        return count

    def search(self, query: str = "", sender: str = "", limit: int = 5,
               restrict_to_sender: str = "") -> list[dict]:
        """Substring search over subject+body, newest first.

        `restrict_to_sender` is a hard scope applied on top of whatever
        Claude asked for. It's how an untrusted correspondent gets a
        history search that can only ever see their own thread.
        """
        q = (query or "").lower().strip()
        s = (sender or "").lower().strip()
        hard = (restrict_to_sender or "").lower().strip()

        matches = []
        for row in self.queue_rows():
            sender_field = f"{row.get('sender', '')} {row.get('sender_addr', '')}".lower()
            if hard and row.get("sender_addr", "").lower() != hard:
                continue
            if s and s not in sender_field:
                continue
            if q and q not in f"{row.get('subject', '')} {row.get('body', '')}".lower():
                continue
            matches.append(row)

        matches.sort(key=lambda r: r.get("received_at", ""), reverse=True)
        matches = matches[: max(1, limit)]
        if not matches:
            return []

        replies = self.responses_by_message()
        return [
            {
                "gmail_msg_id": m.get("gmail_msg_id"),
                "sender": m.get("sender"),
                "subject": m.get("subject"),
                "body": m.get("body"),
                "received_at": m.get("received_at"),
                "responses": replies.get(m.get("gmail_msg_id"), []),
            }
            for m in matches
        ]


def _received_at_iso(msg: dict) -> str:
    ts = msg.get("internal_date") or 0
    if ts:
        return datetime.datetime.fromtimestamp(
            ts / 1000, tz=datetime.timezone.utc
        ).isoformat()
    return _now_iso()


def format_exchanges(queue_rows: list[dict], responses: dict[str, list[str]],
                     max_chars_each: int = 2000) -> str:
    """Readable message/reply pairs for the memory prompt and the history
    search tool result."""
    blocks = []
    for row in queue_rows:
        replies = responses.get(row.get("gmail_msg_id", "")) or ["(no reply recorded)"]
        body = (row.get("body") or "")[:max_chars_each]
        reply = " / ".join(replies)[:max_chars_each]
        blocks.append(
            f"Date: {row.get('received_at', '')}\n"
            f"From: {row.get('sender', '')}\n"
            f"Subject: {row.get('subject', '')}\n"
            f"Message: {body}\n"
            f"Reply: {reply}"
        )
    return "\n\n---\n\n".join(blocks)


# ---------------------------------------------------------------------------
# OUTBOUND + JOURNAL  (Indi's own initiative, not replies)
# ---------------------------------------------------------------------------

def _outbound_methods():
    """Attached to SheetStore below. Kept here so the reply-path code above
    stays readable."""

    def outbound_rows(self) -> list[dict]:
        return self._load(self.responses_id, OUTBOUND_TAB)

    def journal_rows(self) -> list[dict]:
        return self._load(self.responses_id, JOURNAL_TAB)

    def append_outbound(self, recipient: str, subject: str, body: str,
                        rationale: str) -> None:
        self._append(self.responses_id, OUTBOUND_TAB, OUTBOUND_HEADERS, {
            "recipient": recipient,
            "subject": subject[:500],
            "body": body[: self.cfg.max_body_chars],
            "rationale": rationale[:500],
            "created_at": _now_iso(),
        })

    def append_journal(self, entry: str) -> None:
        self._append(self.responses_id, JOURNAL_TAB, JOURNAL_HEADERS, {
            "entry": entry[: self.cfg.max_body_chars],
            "created_at": _now_iso(),
        })

    def _count_since(self, rows: list[dict], hours: int, field: str = "",
                     value: str = "") -> int:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
        count = 0
        for row in rows:
            if field and row.get(field, "").lower() != value.lower():
                continue
            try:
                when = datetime.datetime.fromisoformat(row.get("created_at", ""))
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=datetime.timezone.utc)
            if when >= cutoff:
                count += 1
        return count

    def outbound_today(self) -> int:
        return self._count_since(self.outbound_rows(), 24)

    def outbound_today_to(self, recipient: str) -> int:
        return self._count_since(self.outbound_rows(), 24, "recipient", recipient)

    def journal_today(self) -> int:
        return self._count_since(self.journal_rows(), 24)

    def recent_journal(self, limit: int = 10) -> list[dict]:
        rows = sorted(self.journal_rows(), key=lambda r: r.get("created_at", ""))
        return rows[-limit:]

    def known_correspondents(self) -> set[str]:
        """Addresses Indi has actually exchanged mail with. The default
        recipient policy for self-initiated mail: Indi may write to people
        already in conversation with it, not to addresses it invented or
        found on a web page."""
        out = set()
        for row in self.queue_rows():
            addr = row.get("sender_addr", "").strip().lower()
            if addr and row.get("status") in (STATUS_REPLIED, STATUS_IGNORED):
                out.add(addr)
        for row in self.outbound_rows():
            addr = row.get("recipient", "").strip().lower()
            if addr:
                out.add(addr)
        return out

    for fn in (outbound_rows, journal_rows, append_outbound, append_journal,
               _count_since, outbound_today, outbound_today_to, journal_today,
               recent_journal, known_correspondents):
        setattr(SheetStore, fn.__name__, fn)


def _cost_methods():
    """The spend ledger. Attached the same way as the outbound methods."""

    def cost_rows(self) -> list[dict]:
        return self._load(self.responses_id, COSTS_TAB)

    def cost_table(self) -> tuple[list[str], list[dict]]:
        """The Costs tab as (header row, data rows), for exporting verbatim.

        `cost_rows` is all an aggregate needs, but handing the ledger back
        as a file means reproducing the sheet's own header line: a
        deployment whose tab gained a column through `_ensure_headers`
        should export that column, under the name the sheet gives it and
        in the position the sheet puts it. The declared COSTS_HEADERS are
        only the fallback for a tab we could read rows from but no header.

        This one degrades instead of raising. It is called from the reply
        path, where an unreachable Sheets API or a missing tab would
        otherwise turn a question about bookkeeping into a failed reply;
        an empty result says "nothing to export" and the caller says so.
        """
        try:
            rows = self.cost_rows()
            headers = self.tab_headers(self.responses_id, COSTS_TAB)
        except Exception:
            log.warning("Could not read the %s tab for export.", COSTS_TAB,
                        exc_info=True)
            return [], []
        if not headers:
            headers = list(COSTS_HEADERS) if rows else []
        return headers, rows

    def append_costs(self, rows: list[dict]) -> None:
        """Write a run's priced calls. One append per row — a run makes a
        handful of calls at most, and batching them would mean holding the
        ledger open across the whole run for no real gain."""
        for row in rows:
            self._append(self.responses_id, COSTS_TAB, COSTS_HEADERS, row)

    def spend_since(self, hours: int = 24) -> tuple[float, int]:
        """(dollars, calls) over the window. Rows with a blank `usd` are
        counted as calls but contribute no dollars — they're the ones
        costs.py couldn't price."""
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
        total, calls = 0.0, 0
        for row in self.cost_rows():
            try:
                when = datetime.datetime.fromisoformat(row.get("at", ""))
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=datetime.timezone.utc)
            if when < cutoff:
                continue
            calls += 1
            try:
                total += float(row.get("usd") or 0)
            except ValueError:
                continue
        return round(total, 6), calls

    for fn in (cost_rows, cost_table, append_costs, spend_since):
        setattr(SheetStore, fn.__name__, fn)


_outbound_methods()
_cost_methods()
