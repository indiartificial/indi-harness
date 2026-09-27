"""
drive_tables.py

The harness's non-mail records — journal, cost ledger, reminders, the
newsletter's subscribers and issues, and the decisions log — as CSV files
in Google Drive instead of tabs in a Google Sheet (MIGRATE-GMAIL.md,
steps 6 and 7).

HOW
---
`DriveSheets` answers the handful of Sheets API calls the store and its
callers make — read a range, append a row, update cells, list and add
tabs — over one CSV file per tab, named `Indi Records - <tab>.csv`. So
`SheetStore`, the reminder code in outbound.py and the newsletter code
run unchanged: they still think in tabs, rows and A1 ranges. Only the
object behind `store.sheets` is different.

Why CSV: it opens as a table from Drive (and in Sheets, if you want to
look), it is plain text anyone can read without the harness, and Python
quotes multi-line cells — journal entries, email-sized notes — properly.

WHAT TO KNOW
------------
  * **Every write uploads the whole file.** Drive has no append. The
    tabs are small (the cost ledger is the largest, a row per Claude
    call), and the harness lock means one writer at a time, so this is
    simple rather than clever. Drive keeps earlier revisions of each
    file, which is the way back from a bad write.
  * **Each file is read once per run** and kept in memory; writes go to
    memory and straight to Drive.
  * **Position is identity, as in the sheet.** Rows are only ever
    appended or updated in place, never inserted or deleted, so a row's
    number stays put — which is what the reminder and newsletter code
    rely on when they update a status by row.
  * **Files are found by name**, with the `drive.file` scope the harness
    already has, which sees only files the harness created. A copy made
    by hand in Drive is invisible to it.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from types import SimpleNamespace

log = logging.getLogger("indi.drive_tables")

PREFIX = "Indi Records - "
CSV_MIME = "text/csv"
# The id `SheetStore.responses_id` gets when records live in Drive. Only
# ever compared, never sent anywhere.
RECORDS_ID = "drive:indi-records"

_CELL = re.compile(r"^([A-Z]*)(\d*)$")


def file_name(tab: str) -> str:
    return f"{PREFIX}{tab}.csv"


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def _parse_range(a1: str):
    """'Tab!B3:D3' -> ('Tab', (row0, col0), (row1, col1)); open ends are None."""
    tab, _, cells = a1.partition("!")
    start, _, end = (cells or "A1").partition(":")

    def cell(text, default_row, default_col):
        m = _CELL.match(text.strip())
        if not m:
            raise ValueError(f"Unsupported range {a1!r}")
        letters, digits = m.groups()
        col = _col_index(letters) if letters else default_col
        row = int(digits) - 1 if digits else default_row
        return row, col

    lo = cell(start, 0, 0)
    hi = cell(end, None, None) if end else lo
    return tab, lo, hi


def to_csv(rows: list[list]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    for row in rows:
        writer.writerow(["" if v is None else str(v) for v in row])
    return buf.getvalue()


def from_csv(text: str) -> list[list[str]]:
    return [list(r) for r in csv.reader(io.StringIO(text or ""))]


class DriveIO:
    """The four Drive operations, over a real Drive client. Separate so
    tests can hand `DriveSheets` a dictionary instead."""

    def __init__(self, drive):
        self.drive = drive

    def list_names(self) -> set[str]:
        q = f"name contains '{PREFIX}' and trashed = false"
        names, token = set(), None
        while True:
            res = self.drive.files().list(q=q, spaces="drive", pageToken=token,
                                          fields="nextPageToken, files(name)").execute()
            names |= {f["name"] for f in res.get("files", [])}
            token = res.get("nextPageToken")
            if not token:
                return names

    def find(self, name: str) -> str | None:
        import memory_file
        return memory_file.find_file_id(self.drive, name)

    def read(self, file_id: str) -> str:
        import memory_file
        return memory_file.read_file(self.drive, file_id)

    def write(self, name: str, text: str, file_id: str | None) -> str:
        from googleapiclient.http import MediaIoBaseUpload
        media = MediaIoBaseUpload(io.BytesIO(text.encode("utf-8")), mimetype=CSV_MIME)
        if file_id:
            self.drive.files().update(fileId=file_id, media_body=media).execute()
            return file_id
        created = self.drive.files().create(
            body={"name": name, "mimeType": CSV_MIME}, media_body=media, fields="id",
        ).execute()
        return created["id"]


class DriveSheets:
    """Enough of `sheets.spreadsheets()` / `.values()` for the store."""

    def __init__(self, io_):
        self.io = io_
        self._rows: dict[str, list[list[str]]] = {}
        self._ids: dict[str, str | None] = {}
        self._names: set[str] | None = None

    # -- files ------------------------------------------------------------

    def _tab(self, tab: str) -> list[list[str]]:
        if tab not in self._rows:
            fid = self.io.find(file_name(tab))
            self._ids[tab] = fid
            self._rows[tab] = from_csv(self.io.read(fid)) if fid else []
        return self._rows[tab]

    def _save(self, tab: str) -> None:
        fid = self.io.write(file_name(tab), to_csv(self._rows[tab]), self._ids.get(tab))
        self._ids[tab] = fid
        if self._names is not None:
            self._names.add(file_name(tab))

    def tabs(self) -> set[str]:
        if self._names is None:
            self._names = set(self.io.list_names())
        return {n[len(PREFIX):-4] for n in self._names
                if n.startswith(PREFIX) and n.endswith(".csv")}

    # -- the client chain -------------------------------------------------

    def spreadsheets(self):
        return self

    def values(self):
        return self

    @staticmethod
    def _call(fn):
        return SimpleNamespace(execute=lambda **_: fn())

    # spreadsheets().get / batchUpdate(addSheet) / create
    def get(self, spreadsheetId=None, range=None, fields=None, **_):
        if range is None:
            return self._call(lambda: {"sheets": [{"properties": {"title": t}}
                                                  for t in sorted(self.tabs())]})
        return self._call(lambda: {"values": self._read(range)})

    def batchUpdate(self, spreadsheetId=None, body=None, **_):
        def go():
            if body and "requests" in body:
                for req in body["requests"]:
                    title = (req.get("addSheet") or {}).get("properties", {}).get("title")
                    if title:
                        self._tab(title)
                        self._save(title)
                return {}
            for item in (body or {}).get("data", []):
                self._write(item["range"], item["values"])
            return {}
        return self._call(go)

    def create(self, body=None, fields=None, **_):
        raise RuntimeError("Records live in Drive; there is no spreadsheet to create.")

    # values().append / update / batchClear
    def append(self, spreadsheetId=None, range=None, body=None, **_):
        def go():
            tab = range.partition("!")[0]
            rows = self._tab(tab)
            for values in body["values"]:
                rows.append(["" if v is None else str(v) for v in values])
            self._save(tab)
            n = len(rows)
            return {"updates": {"updatedRange": f"{tab}!A{n}:Z{n}"}}
        return self._call(go)

    def update(self, spreadsheetId=None, range=None, body=None, **_):
        def go():
            self._write(range, body["values"])
            return {}
        return self._call(go)

    def batchClear(self, spreadsheetId=None, body=None, **_):
        def go():
            for a1 in body.get("ranges", []):
                tab, (r0, c0), (r1, c1) = _parse_range(a1)
                rows = self._tab(tab)
                for r in range_(r0, (r1 if r1 is not None else len(rows) - 1) + 1):
                    if r < len(rows):
                        end = len(rows[r]) if c1 is None else c1 + 1
                        for c in range_(c0, min(end, len(rows[r]))):
                            rows[r][c] = ""
                        while rows[r] and rows[r][-1] == "":
                            rows[r].pop()
                self._save(tab)
            return {}
        return self._call(go)

    # -- ranges -----------------------------------------------------------

    def _read(self, a1: str) -> list[list[str]]:
        tab, (r0, c0), (r1, c1) = _parse_range(a1)
        rows = self._tab(tab)
        last = len(rows) - 1 if r1 is None else min(r1, len(rows) - 1)
        out = []
        for r in range_(r0, last + 1):
            row = rows[r]
            cells = row[c0:] if c1 is None else row[c0:c1 + 1]
            out.append(list(cells))
        # Sheets omits trailing empty rows; so do we.
        while out and not any(out[-1]):
            out.pop()
        return out

    def _write(self, a1: str, values: list[list]) -> None:
        tab, (r0, c0), _ = _parse_range(a1)
        rows = self._tab(tab)
        for i, new in enumerate(values):
            r = r0 + i
            while len(rows) <= r:
                rows.append([])
            row = rows[r]
            while len(row) < c0 + len(new):
                row.append("")
            for j, v in enumerate(new):
                row[c0 + j] = "" if v is None else str(v)
        self._save(tab)


range_ = range
