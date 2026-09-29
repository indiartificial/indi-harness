"""
gmail_store.py

Step 5 of MIGRATE-GMAIL.md: Gmail answers the store's questions about mail.

`GmailStore` is a `SheetStore` whose *readers* ask Gmail. Its writers are
inherited unchanged, so the Queue, Responses and Outbound tabs keep being
written, and the Indi/* labels keep being set beside them. That is the
"flip the flag, keep writing the sheet" of step 5: if the Gmail answers
turn out wrong, turning INDI_GMAIL_READS off puts the sheet back in charge
with nothing lost. The journal, the cost ledger, reminders and the
newsletter are not mail and are still read from the sheet.

Off by default. Turn it on only after the order in MIGRATE-GMAIL.md:
`backfill_labels.py --apply`, `cutover_prep.py --apply`, and some days of
shadow reads (INDI_GMAIL_SHADOW_READS) whose disagreements are understood.

WHERE EACH ANSWER COMES FROM
----------------------------
  status (find_queue_row)   the message's Indi/<Status> label. Where the
                            label is missing but the sheet has a row, the
                            sheet's status is used and the gap is logged:
                            during the transition a missing label must not
                            turn "replied" into "never seen", which would
                            answer a person twice.
  messages_for_sender       Indi/Replied messages whose From is exactly
                            that one address, oldest first.
  replies                   Indi's messages (SENT or DRAFT) in the same
                            thread whose In-Reply-To names the message.
                            Labelled Indi/Reply ones are preferred; an
                            unlabelled one only counts when there is none,
                            which covers a draft sent by hand from a
                            client that dropped the label.
  replies_in_last_day       Indi/Reply messages to that address in the
                            exact rolling 24h, drafts included. This is
                            the rate-limit decision MIGRATE-GMAIL.md left
                            open: it counts Indi's replies, as the sheet
                            did — not everything sent (which would add
                            heartbeat mail) and not only sent mail (which
                            would count nothing while replies are drafts).
  known_correspondents      senders of Indi/Replied and Indi/Ignored mail,
                            plus recipients of Indi/Outbound mail — the
                            same three sources as the sheet, and no
                            wholesale Sent.
  outbound budgets          Indi/Outbound messages in the exact rolling 24h.
  search                    `gmail_reads.GmailReads.search_ids` — the step
                            3 reader, whose sender scope is enforced in
                            Python on every result — then the bodies of
                            just those messages.

What Gmail does not hold: the queue row's `note` and an outbound message's
`rationale`. Rows from this store carry an empty `note` (nothing reads it
but people); both are still written to the sheet, and move to the Journal
on Drive at step 6.

Cost. Gmail meters every call in quota units, per user per minute. For
projects created from May 2026 — Indi's is one — the budget is 6,000
units a minute, and a message fetch costs 20 units (a thread 40, a list
5). At those prices re-reading the same headers every run is what runs
out the budget, so:

  * **Headers that never change are cached on disk** (MetaCache below):
    a message's From, To, Cc, date and thread are fixed once it exists.
    Which label a message carries does change, and is always asked of
    Gmail — but that is a list call, 5 units per 500 messages. So each
    question costs a few list calls plus 20 units per message the cache
    hasn't seen yet, instead of 20 units per message ever handled.
  * **Every run paces itself** (Pacer below) to stay under the per-minute
    budget, rather than running into it and waiting out the retries.
  * Bodies are fetched only for rows actually returned, threads once per
    run, and gets are batched.
"""

from __future__ import annotations

import datetime
import logging
from email.utils import getaddresses

import gmail_io
import gmail_reads
import sheets_queue
from sheets_queue import (
    LABEL_NOTICE, LABEL_OUTBOUND, LABEL_REPLY, STATUS_LABELS, SheetStore,
)

log = logging.getLogger("indi.gmail_store")

# Upper bounds on how much one question may fetch. Generous for a mailbox
# of this size; hitting one is logged rather than silently truncating.
MAX_LIST_PAGES = 20          # x 500 ids per page
MAX_KNOWN_FETCH = 3000
BATCH_SIZE = 25              # Google recommends at most 50 per batch
# The client retries rateLimitExceeded and 5xx itself, with backoff, when
# asked to. Batched gets that come back failed are retried one at a time.
RETRIES = 5

# Gmail API quota units (reference/quota, as revised 2026-05-01) and the
# share of the 6,000-a-minute user budget one run lets itself spend. The
# rest is headroom for the retries, the send, and anything else running.
UNITS_GET = 20
UNITS_THREAD = 40
UNITS_PER_MINUTE = 4500

# The headers MetaCache keeps. Fixed for the life of a message.
META_HEADERS = ("From", "To", "Cc")
DEFAULT_CACHE_NAME = "gmail_meta_cache.json"

_TRUE = ("1", "true", "yes", "on")


def gmail_reads_enabled(cfg=None) -> bool:
    """INDI_GMAIL_READS, default false. Read the same way gmail_reads.py
    reads its settings, since config.py is protected."""
    configured = getattr(cfg, "gmail_reads", None)
    if isinstance(configured, bool):
        return configured
    return gmail_reads._setting("INDI_GMAIL_READS").lower() in _TRUE


def drive_records_enabled(cfg=None) -> bool:
    """INDI_DRIVE_RECORDS, default false: steps 6 and 7 of MIGRATE-GMAIL.md.
    The journal, costs, reminders, newsletter lists and the decisions log
    live in Drive (drive_tables.py), and nothing more is written to the
    Queue, Responses or Outbound tabs. Needs INDI_GMAIL_READS as well."""
    configured = getattr(cfg, "drive_records", None)
    if isinstance(configured, bool):
        return configured
    return gmail_reads._setting("INDI_DRIVE_RECORDS").lower() in _TRUE


# The why behind the mail, kept outside the mailbox: a queue row's `note`
# (why a message was skipped or set aside, what the terms gate sent) and
# an outbound message's `rationale`. Once records are in Drive this is
# where they go, one row per decision, keyed by Gmail id.
DECISIONS_TAB = "Decisions"
DECISIONS_HEADERS = ["at", "gmail_msg_id", "kind", "status", "address", "note"]


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _internal_ms(message: dict) -> int:
    try:
        return int(message.get("internalDate") or 0)
    except (TypeError, ValueError):
        return 0


def _iso(ms: int) -> str:
    if not ms:
        return ""
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).isoformat()


def _single_sender(from_raw: str) -> str:
    """The From address if the header names exactly one, else "". The same
    rule as the search scope: a From naming several people is nobody's."""
    found = [a for _, a in getaddresses([from_raw or ""]) if a.strip()]
    return gmail_reads.normalise_addr(from_raw) if len(found) == 1 else ""


class Pacer:
    """Keeps one run's Gmail spending under a per-minute budget by waiting
    before a call that would go over, instead of making the call, being
    refused and backing off. Clock and sleep are injectable for tests."""

    def __init__(self, per_minute: int = UNITS_PER_MINUTE, clock=None, sleep=None):
        import time
        self.per_minute = per_minute
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.spent: list[tuple[float, int]] = []

    def spend(self, units: int) -> None:
        now = self.clock()
        self.spent = [(t, u) for t, u in self.spent if now - t < 60]
        used = sum(u for _, u in self.spent)
        if self.spent and used + units > self.per_minute:
            # Wait until enough of the last minute's spending has aged out.
            need = used + units - self.per_minute
            for t, u in self.spent:
                need -= u
                if need <= 0:
                    wait = 60 - (now - t) + 0.05
                    log.info("Pacing Gmail calls: waiting %.1fs to stay under "
                             "%d units a minute.", wait, self.per_minute)
                    self.sleep(max(wait, 0))
                    break
            now = self.clock()
            self.spent = [(t, u) for t, u in self.spent if now - t < 60]
        self.spent.append((now, units))


class MetaCache:
    """Gmail id -> the headers of that message that never change.

    A JSON file beside the other machine-local data. It is only ever a
    copy of what Gmail holds: delete it and the next run rebuilds it,
    slowly. A message deleted from Gmail stays in the file harmlessly,
    since nothing is looked up here except ids Gmail has just listed.
    `path=None` keeps it in memory only (tests, dry runs)."""

    def __init__(self, path=None):
        self.path = path
        self.data: dict[str, dict] = {}
        self.dirty = False
        if path is not None:
            try:
                import json
                with open(path, encoding="utf-8") as fh:
                    self.data = json.load(fh)
            except FileNotFoundError:
                pass
            except Exception:
                log.warning("Gmail header cache %s unreadable; starting afresh.",
                            path, exc_info=True)
                self.data = {}

    def get(self, msg_id: str) -> dict | None:
        return self.data.get(msg_id)

    def put(self, message: dict) -> dict:
        hdrs = gmail_reads._headers(message)
        entry = {"from": hdrs.get("from", ""), "to": hdrs.get("to", ""),
                 "cc": hdrs.get("cc", ""), "date": _internal_ms(message),
                 "thread": message.get("threadId", "")}
        self.data[message["id"]] = entry
        self.dirty = True
        return entry

    def save(self) -> None:
        if self.path is None or not self.dirty:
            return
        import json
        import os
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh)
            os.replace(tmp, self.path)
            self.dirty = False
        except Exception:
            log.warning("Could not save the Gmail header cache.", exc_info=True)


def default_cache_path(cfg):
    base = getattr(cfg, "base_dir", None)
    if base is None:
        return None
    from pathlib import Path
    folder = Path(base) / "data"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return folder / DEFAULT_CACHE_NAME


class GmailStore(SheetStore):
    """A SheetStore that reads mail from Gmail. See the module docstring."""

    def __init__(self, sheets_service, drive_service, cfg, gmail_service,
                 records_in_drive: bool = False, cache: MetaCache | None = None,
                 pacer: Pacer | None = None):
        # Labels are what this store reads, so the writes that set them are
        # always on here, whatever INDI_GMAIL_STATUS_LABELS says.
        if not getattr(cfg, "gmail_status_labels", True):
            log.warning("INDI_GMAIL_READS is on, so status labels are written "
                        "even though INDI_GMAIL_STATUS_LABELS is false.")
        super().__init__(sheets_service, drive_service, cfg,
                         gmail_service=gmail_service)
        self.gmail = gmail_service
        self.reads = gmail_reads.GmailReads(gmail_service)
        self._full: dict[str, dict] = {}
        self._threads: dict[str, list[dict]] = {}
        self._known: set[str] | None = None
        # Indi's own addresses, set by the caller once known; needed to tell
        # a Bcc from a Cc and Indi's messages from anyone else's.
        self.selves: set[str] = set()
        self._grants = None
        # With records in Drive, `sheets_service` is a drive_tables.DriveSheets
        # and the mail tabs are no longer written at all: Gmail and its
        # labels are the record of the mail, the Decisions file of the why.
        self.records_in_drive = records_in_drive
        self.cache = cache if cache is not None else MetaCache(
            None if getattr(cfg, "dry_run", False) else default_cache_path(cfg))
        self.pacer = pacer or Pacer()

    # -- records in Drive (steps 6-7) -------------------------------------

    def ensure_spreadsheets(self) -> None:
        if not self.records_in_drive:
            return super().ensure_spreadsheets()
        import drive_tables
        self.queue_id = None                 # nothing reads or writes the Queue tab
        self.responses_id = drive_tables.RECORDS_ID
        for tab, headers in ((sheets_queue.JOURNAL_TAB, sheets_queue.JOURNAL_HEADERS),
                             (sheets_queue.COSTS_TAB, sheets_queue.COSTS_HEADERS),
                             (DECISIONS_TAB, DECISIONS_HEADERS)):
            self._ensure_tab(self.responses_id, tab, headers)

    def _decision(self, gmail_msg_id: str, kind: str, status: str,
                  address: str, note: str) -> None:
        """Append one decision. Best effort: a Drive hiccup must not turn a
        reply into a failure, any more than a label write may."""
        if not note and kind == "status":
            return
        try:
            self._append(self.responses_id, DECISIONS_TAB, DECISIONS_HEADERS, {
                "at": _utc_now().isoformat(), "gmail_msg_id": gmail_msg_id,
                "kind": kind, "status": status, "address": address,
                "note": (note or "")[:500],
            })
        except Exception:
            log.warning("Could not record the %s decision for %s.", kind,
                        gmail_msg_id or address, exc_info=True)

    def append_queue_row(self, msg: dict, status: str = sheets_queue.STATUS_QUEUED,
                         note: str = "") -> dict:
        if not self.records_in_drive:
            return super().append_queue_row(msg, status, note)
        row = {
            "gmail_msg_id": msg["gmail_msg_id"], "thread_id": msg.get("thread_id", ""),
            "sender": msg.get("from_raw", ""), "sender_addr": msg.get("from_addr", ""),
            "subject": msg.get("subject", ""), "status": status, "note": note[:500],
            "received_at": sheets_queue._received_at_iso(msg),
        }
        self._label_status(row["gmail_msg_id"], status)
        self._decision(row["gmail_msg_id"], "status", status, row["sender_addr"], note)
        return row

    def set_status(self, queue_row: dict, status: str, note: str = "") -> None:
        if not self.records_in_drive:
            return super().set_status(queue_row, status, note)
        queue_row["status"] = status
        if note:
            queue_row["note"] = note[:500]
        msg_id = queue_row.get("gmail_msg_id", "")
        self._label_status(msg_id, status)
        self._decision(msg_id, "status", status, queue_row.get("sender_addr", ""), note)

    def set_attachments(self, queue_row: dict, paths: list[str]) -> None:
        if not self.records_in_drive:
            return super().set_attachments(queue_row, paths)
        # The attachment store is keyed by message id already; nothing to add.

    def append_response(self, gmail_msg_id: str, sender_addr: str, text: str,
                        reply_id: str = "") -> None:
        if not self.records_in_drive:
            return super().append_response(gmail_msg_id, sender_addr, text,
                                           reply_id=reply_id)
        # The reply is in Gmail, in the thread; the label marks it as Indi's.
        self._label_add(reply_id, LABEL_REPLY)

    # -- Gmail plumbing ---------------------------------------------------

    def _label_id(self, name: str) -> str | None:
        return self._labeler.existing_id(name)

    def _status_by_label_id(self) -> dict[str, str]:
        out = {}
        for status, name in STATUS_LABELS.items():
            lid = self._label_id(name)
            if lid:
                out[lid] = status
        return out

    def _ids(self, label: str | None = None, q: str = "") -> list[str]:
        """Message ids, newest first, over at most MAX_LIST_PAGES pages."""
        kw = {}
        if label is not None:
            lid = self._label_id(label)
            if not lid:
                return []           # no such label yet: nothing carries it
            kw["labelIds"] = [lid]
        if q:
            kw["q"] = q
        ids, more = self.reads._list(max_pages=MAX_LIST_PAGES, page_size=500, **kw)
        if more:
            log.warning("Gmail list for label=%s q=%r stopped at %d ids.",
                        label, q[:80], len(ids))
        return ids

    def _get_many(self, ids: list[str], fmt: str = "full",
                  headers: tuple[str, ...] = ()) -> list[dict]:
        """Fetch messages, in the order given, batched where the client can.
        A message deleted since it was listed is skipped, not fatal."""
        if fmt == "full":
            missing = [i for i in ids if i not in self._full]
        else:
            missing = list(ids)
        got: dict[str, dict] = {}
        extra = {"metadataHeaders": list(headers)} if fmt == "metadata" else {}

        def request(mid):
            return self.gmail.users().messages().get(userId="me", id=mid,
                                                     format=fmt, **extra)

        batch_factory = getattr(self.gmail, "new_batch_http_request", None)
        if batch_factory is not None and len(missing) > 1:
            for start in range(0, len(missing), BATCH_SIZE):
                self.pacer.spend(UNITS_GET * len(missing[start:start + BATCH_SIZE]))
                def done(request_id, response, exception):
                    if exception is None and response:
                        got[request_id] = response
                    else:
                        log.debug("Could not fetch %s: %s", request_id, exception)
                batch = batch_factory(callback=done)
                for mid in missing[start:start + BATCH_SIZE]:
                    batch.add(request(mid), request_id=mid)
                batch.execute()
        # One at a time: everything when there's no batching, and whatever
        # a batch lost (a rate-limited part comes back as an exception).
        for mid in missing:
            if mid in got:
                continue
            self.pacer.spend(UNITS_GET)
            try:
                got[mid] = request(mid).execute(num_retries=RETRIES)
            except Exception:
                log.debug("Could not fetch %s", mid, exc_info=True)
        if fmt == "full":
            self._full.update(got)
            return [self._full[i] for i in ids if i in self._full]
        return [got[i] for i in ids if i in got]

    def _meta(self, ids: list[str]) -> dict[str, dict]:
        """The fixed headers of these messages: from the cache, fetching
        (and caching) only the ones it hasn't seen."""
        out, missing = {}, []
        for mid in ids:
            entry = self.cache.get(mid)
            if entry is None:
                missing.append(mid)
            else:
                out[mid] = entry
        if missing:
            for m in self._get_many(missing, fmt="metadata", headers=META_HEADERS):
                out[m["id"]] = self.cache.put(m)
            self.cache.save()
        return out

    def _thread(self, thread_id: str) -> list[dict]:
        if not thread_id:
            return []
        if thread_id not in self._threads:
            self.pacer.spend(UNITS_THREAD)
            thread = self.gmail.users().threads().get(
                userId="me", id=thread_id, format="full").execute(num_retries=RETRIES)
            self._threads[thread_id] = sorted(thread.get("messages", []) or [],
                                              key=_internal_ms)
        return self._threads[thread_id]

    def _row(self, message: dict) -> dict:
        """A Gmail message in the shape of a Queue row."""
        msg = gmail_io.extract_message(message, self.cfg.max_body_chars)
        by_id = self._status_by_label_id()
        status = next((by_id[l] for l in message.get("labelIds") or [] if l in by_id), "")
        return {
            "gmail_msg_id": msg["gmail_msg_id"],
            "thread_id": msg["thread_id"],
            "sender": msg["from_raw"],
            "sender_addr": msg["from_addr"],
            "subject": msg["subject"],
            "body": msg["body"],
            "received_at": _iso(msg["internal_date"]),
            "message_id": msg["message_id"],
            "status": status,
            "note": "",
            "attachments": "",
        }

    def _rows(self, ids: list[str]) -> list[dict]:
        rows = [self._row(m) for m in self._get_many(ids)]
        rows.sort(key=lambda r: r.get("received_at", ""))
        return rows

    # -- status -----------------------------------------------------------

    def _sheet_row(self, gmail_msg_id: str) -> dict | None:
        """The Queue sheet's row for this message, read from the sheet
        itself (`queue_rows` here means Gmail). Its `_row` is what lets the
        inherited status writes keep landing on the sheet."""
        if not self.queue_id:
            return None
        for row in self._load(self.queue_id, sheets_queue.QUEUE_TAB):
            if row.get("gmail_msg_id") == gmail_msg_id:
                return row
        return None

    def find_queue_row(self, gmail_msg_id: str) -> dict | None:
        sheet_row = self._sheet_row(gmail_msg_id)
        try:
            meta = self.gmail.users().messages().get(
                userId="me", id=gmail_msg_id, format="metadata",
                metadataHeaders=["From"]).execute(num_retries=RETRIES)
        except Exception:
            meta = None
        by_id = self._status_by_label_id()
        status = ""
        if meta:
            status = next((by_id[l] for l in meta.get("labelIds") or [] if l in by_id), "")
        if not status:
            if sheet_row is not None:
                log.warning("No Indi/* label on %s but the sheet says %r; "
                            "using the sheet's status.", gmail_msg_id,
                            sheet_row.get("status"))
            return sheet_row
        if sheet_row is not None and sheet_row.get("status") not in ("", status):
            log.warning("Status of %s: Gmail says %r, the sheet %r; Gmail's "
                        "answer is used.", gmail_msg_id, status, sheet_row.get("status"))
        from_raw = gmail_reads._headers(meta).get("from", "")
        row = sheet_row if sheet_row is not None else {}
        # `_row` stays whatever the sheet had, so status writes still land
        # on the sheet row; the answer itself is Gmail's.
        row.update({
            "gmail_msg_id": gmail_msg_id,
            "thread_id": meta.get("threadId", row.get("thread_id", "")),
            "sender": row.get("sender") or from_raw,
            "sender_addr": gmail_reads.normalise_addr(from_raw) or row.get("sender_addr", ""),
            "status": status,
        })
        row.setdefault("note", "")
        return row

    # -- a correspondent's history ---------------------------------------

    def messages_for_sender(self, sender_addr: str, exclude_ids=None) -> list[dict]:
        addr = gmail_reads.normalise_addr(sender_addr)
        if not addr:
            return []
        q = f"from:{addr}" if gmail_reads._query_addr(addr) else ""
        ids = self._ids(STATUS_LABELS[sheets_queue.STATUS_REPLIED], q)
        excluded = set(exclude_ids or ())
        # The label came from the list; the sender comes from the cache,
        # checked exactly, because `from:` is a search, not an equality.
        meta = self._meta(ids)
        mine = sorted((mid for mid, m in meta.items()
                       if _single_sender(m["from"]) == addr and mid not in excluded),
                      key=lambda mid: meta[mid]["date"])
        return self._rows(mine)

    # -- replies ----------------------------------------------------------

    def reply_messages(self, row: dict) -> list[dict]:
        """Indi's messages answering this row, oldest first."""
        msg_id = row.get("gmail_msg_id", "")
        thread = self._thread(row.get("thread_id", ""))
        rfc = (row.get("message_id") or "").strip()
        if not rfc:
            this = next((m for m in thread if m.get("id") == msg_id), None)
            rfc = gmail_reads._headers(this or {}).get("message-id", "").strip()
        if not rfc:
            return []
        own = [m for m in thread
               if {"SENT", "DRAFT"} & set(m.get("labelIds") or [])
               and gmail_reads._headers(m).get("in-reply-to", "").strip() == rfc]
        reply_label = self._label_id(LABEL_REPLY)
        labelled = [m for m in own if reply_label and reply_label in (m.get("labelIds") or [])]
        return labelled or own

    def responses_for(self, rows: list[dict], cleared_for=None) -> dict[str, list[str]]:
        """Indi's replies to each row. With `cleared_for`, only replies whose
        own audience covers those people: a private reply to Bob can repeat
        Bob's news, so it is Bob's to see, not Ann's."""
        out: dict[str, list[str]] = {}
        for row in rows:
            try:
                replies = self.reply_messages(row)
            except Exception:
                log.warning("Could not read the thread of %s.",
                            row.get("gmail_msg_id"), exc_info=True)
                continue
            if cleared_for is not None:
                import audience
                wanted = {r.lower() for r in cleared_for if r}
                replies = [m for m in replies if wanted <= audience.outgoing_audience(
                    gmail_reads._headers(m).get("to", ""),
                    gmail_reads._headers(m).get("cc", ""), self.selves)]
            texts = [gmail_io.extract_message(m, self.cfg.max_body_chars)["body"]
                     for m in replies]
            if texts:
                out[row["gmail_msg_id"]] = texts
        return out

    def responses_by_message(self) -> dict[str, list[str]]:
        """Every reply to every handled message. Reads every thread: use
        `responses_for` with the rows you have instead."""
        return self.responses_for(self.queue_rows())

    def replies_in_last_day(self, sender_addr: str) -> int:
        addr = gmail_reads.normalise_addr(sender_addr)
        if not addr:
            return 0
        cutoff_ms = int((_utc_now() - datetime.timedelta(days=1)).timestamp() * 1000)
        q = "newer_than:2d" + (f" to:{addr}" if gmail_reads._query_addr(addr) else "")
        ids = self._ids(LABEL_REPLY, q)
        return sum(1 for m in self._meta(ids).values()
                   if addr in gmail_reads._addrs(m["to"]) and m["date"] >= cutoff_ms)

    def notices_in_last_day(self, addr: str) -> int:
        """"Unable to respond" notices sent to this address in the last 24h."""
        addr = gmail_reads.normalise_addr(addr)
        if not addr:
            return 0
        cutoff_ms = int((_utc_now() - datetime.timedelta(days=1)).timestamp() * 1000)
        q = "newer_than:2d" + (f" to:{addr}" if gmail_reads._query_addr(addr) else "")
        ids = self._ids(LABEL_NOTICE, q)
        return sum(1 for m in self._meta(ids).values()
                   if addr in gmail_reads._addrs(m["to"]) and m["date"] >= cutoff_ms)

    def record_notice(self, message_id: str) -> None:
        self._label_add(message_id, LABEL_NOTICE)

    # -- handled mail in general -----------------------------------------

    def _handled_ids(self, limit: int | None = None) -> list[str]:
        ids, _ = self.reads._list(
            max_pages=1 if limit else MAX_LIST_PAGES,
            page_size=min(500, limit) if limit else 500,
            q=gmail_reads._label_query(gmail_reads.INDI_LABELS),
        )
        return ids[:limit] if limit else ids

    def queue_rows(self) -> list[dict]:
        """Every handled message, oldest first. Expensive — bodies for the
        whole history. Nothing on the reply or heartbeat path calls it."""
        return [r for r in self._rows(self._handled_ids()) if r["status"]]

    def recent_messages(self, n: int) -> list[dict]:
        if n <= 0:
            return []
        rows = [r for r in self._rows(self._handled_ids(limit=n)) if r["status"]]
        return rows[-n:]

    # Phase 2: the audience rule (audience.py) answers who may see what.
    supports_audience = True

    def grants(self):
        if self._grants is None:
            import audience
            self._grants = audience.GrantBook(self)
        return self._grants

    def _cleared(self, ids: list[str], recipients) -> list[str]:
        """The ids, in order, that every one of `recipients` may see."""
        import audience
        meta = self._meta(ids)
        grants = self.grants()
        return [i for i in ids if i in meta
                and audience.cleared(i, meta[i], recipients, self.selves, grants)]

    def search(self, query: str = "", sender: str = "", limit: int = 5,
               restrict_to_sender: str = "", cleared_for=None) -> list[dict]:
        """History search. With `cleared_for` (the people the reply will go
        to), results are every handled message those people may all see,
        whoever sent it — the audience rule, not the sender, is the scope —
        and so are the replies shown with them. Without it, the older
        sender scope applies."""
        if cleared_for is not None:
            recipients = {r.lower() for r in cleared_for if r}
            if not recipients:
                return []
            pool = self.reads.search_ids(query, sender, max(1, limit) * 5, "")
            ids = self._cleared(pool, recipients)[:max(1, limit)]
            rows = [self._row(m) for m in self._get_many(ids)]
            rows.sort(key=lambda r: r.get("received_at", ""), reverse=True)
            replies = self.responses_for(rows, cleared_for=recipients)
            return [
                {"gmail_msg_id": r["gmail_msg_id"], "sender": r["sender"],
                 "subject": r["subject"], "body": r["body"],
                 "received_at": r["received_at"],
                 "responses": replies.get(r["gmail_msg_id"], [])}
                for r in rows
            ]
        ids = self.reads.search_ids(query, sender, limit, restrict_to_sender)
        rows = [self._row(m) for m in self._get_many(ids)]
        # Belt and braces: search_ids has already scoped these, on the
        # metadata it fetched. The full message is re-checked the same
        # way before any body leaves this method.
        hard = gmail_reads.normalise_addr(restrict_to_sender) if restrict_to_sender else ""
        if hard:
            rows = [r for r in rows if _single_sender(r["sender"]) == hard]
        rows.sort(key=lambda r: r.get("received_at", ""), reverse=True)
        replies = self.responses_for(rows)
        return [
            {"gmail_msg_id": r["gmail_msg_id"], "sender": r["sender"],
             "subject": r["subject"], "body": r["body"],
             "received_at": r["received_at"],
             "responses": replies.get(r["gmail_msg_id"], [])}
            for r in rows
        ]

    # -- outbound ---------------------------------------------------------

    def append_outbound(self, recipient: str, subject: str, body: str,
                        rationale: str, message_id: str = "") -> None:
        if self.records_in_drive:
            self._label_add(message_id, LABEL_OUTBOUND)
            self._decision(message_id, "outbound", "", recipient, rationale)
        else:
            super().append_outbound(recipient, subject, body, rationale,
                                    message_id=message_id)
        if self._known is not None and recipient.strip():
            self._known.add(recipient.strip().casefold())

    def _outbound_since(self, hours: int) -> list[dict]:
        days = hours // 24 + 2
        cutoff_ms = int((_utc_now() - datetime.timedelta(hours=hours)).timestamp() * 1000)
        ids = self._ids(LABEL_OUTBOUND, f"newer_than:{days}d")
        return [m for m in self._meta(ids).values() if m["date"] >= cutoff_ms]

    def outbound_today(self) -> int:
        return len(self._outbound_since(24))

    def outbound_today_to(self, recipient: str) -> int:
        addr = gmail_reads.normalise_addr(recipient)
        return sum(1 for m in self._outbound_since(24)
                   if addr in gmail_reads._addrs(m["to"]))

    def recent_outbound(self, n: int) -> list[dict]:
        """The last `n` messages Indi started, oldest first."""
        if n <= 0 or not self._label_id(LABEL_OUTBOUND):
            return []
        ids, _ = self.reads._list(max_pages=1, page_size=n,
                                  labelIds=[self._label_id(LABEL_OUTBOUND)])
        rows = []
        for m in self._get_many(ids[:n]):
            msg = gmail_io.extract_message(m, self.cfg.max_body_chars)
            rows.append({
                "recipient": ", ".join(sorted(gmail_reads._addrs(
                    gmail_reads._headers(m).get("to", "")))),
                "subject": msg["subject"],
                "body": msg["body"],
                "rationale": "",
                "created_at": _iso(msg["internal_date"]),
            })
        rows.sort(key=lambda r: r["created_at"])
        return rows

    def outbound_rows(self) -> list[dict]:
        return self.recent_outbound(50)

    def known_correspondents(self) -> set[str]:
        if self._known is not None:
            return set(self._known)
        out: set[str] = set()
        ids = []
        for status in (sheets_queue.STATUS_REPLIED, sheets_queue.STATUS_IGNORED):
            ids += self._ids(STATUS_LABELS[status])
        outbound_ids = self._ids(LABEL_OUTBOUND)
        wanted = (ids + outbound_ids)[:MAX_KNOWN_FETCH]
        if len(ids) + len(outbound_ids) > MAX_KNOWN_FETCH:
            # Short is the safe direction — fewer people Indi may write to
            # first — but it is still wrong, so say so.
            log.warning("known_correspondents stopped at %d messages; the set "
                        "may be incomplete.", MAX_KNOWN_FETCH)
        meta = self._meta(wanted)
        for mid in ids:
            if mid in meta:
                addr = _single_sender(meta[mid]["from"])
                if addr:
                    out.add(addr)
        for mid in outbound_ids:
            if mid in meta:
                out |= gmail_reads._addrs(meta[mid]["to"]) | gmail_reads._addrs(meta[mid]["cc"])
        self._known = out
        return set(out)
