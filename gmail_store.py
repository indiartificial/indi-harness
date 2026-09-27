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

Cost. Every read here is Gmail traffic where the sheet was one cached
read per run. Metadata gets are used wherever a header is all that is
needed, bodies are fetched only for rows actually returned, threads are
cached for the run, and gets are batched. The lists that could grow
without bound (known correspondents, a long-standing correspondent's
history) are capped, and hitting a cap is logged.
"""

from __future__ import annotations

import datetime
import logging
from email.utils import getaddresses

import gmail_io
import gmail_reads
import sheets_queue
from sheets_queue import (
    LABEL_OUTBOUND, LABEL_REPLY, STATUS_LABELS, SheetStore,
)

log = logging.getLogger("indi.gmail_store")

# Upper bounds on how much one question may fetch. Generous for a mailbox
# of this size; hitting one is logged rather than silently truncating.
MAX_LIST_PAGES = 20          # x 500 ids per page
MAX_KNOWN_FETCH = 3000
BATCH_SIZE = 25              # Gmail's per-user rate is 250 units/s; a get is 5

_TRUE = ("1", "true", "yes", "on")


def gmail_reads_enabled(cfg=None) -> bool:
    """INDI_GMAIL_READS, default false. Read the same way gmail_reads.py
    reads its settings, since config.py is protected."""
    configured = getattr(cfg, "gmail_reads", None)
    if isinstance(configured, bool):
        return configured
    return gmail_reads._setting("INDI_GMAIL_READS").lower() in _TRUE


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


class GmailStore(SheetStore):
    """A SheetStore that reads mail from Gmail. See the module docstring."""

    def __init__(self, sheets_service, drive_service, cfg, gmail_service):
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
        if batch_factory is None or len(missing) <= 1:
            for mid in missing:
                try:
                    got[mid] = request(mid).execute()
                except Exception:
                    log.debug("Could not fetch %s", mid, exc_info=True)
        else:
            for start in range(0, len(missing), BATCH_SIZE):
                def done(request_id, response, exception):
                    if exception is None and response:
                        got[request_id] = response
                    else:
                        log.debug("Could not fetch %s: %s", request_id, exception)
                batch = batch_factory(callback=done)
                for mid in missing[start:start + BATCH_SIZE]:
                    batch.add(request(mid), request_id=mid)
                batch.execute()
        if fmt == "full":
            self._full.update(got)
            return [self._full[i] for i in ids if i in self._full]
        return [got[i] for i in ids if i in got]

    def _thread(self, thread_id: str) -> list[dict]:
        if not thread_id:
            return []
        if thread_id not in self._threads:
            thread = self.gmail.users().threads().get(
                userId="me", id=thread_id, format="full").execute()
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
                metadataHeaders=["From"]).execute()
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
        metas = self._get_many(ids, fmt="metadata", headers=("From",))
        replied = self._label_id(STATUS_LABELS[sheets_queue.STATUS_REPLIED])
        excluded = set(exclude_ids or ())
        mine = [m for m in metas
                if replied in (m.get("labelIds") or [])
                and _single_sender(gmail_reads._headers(m).get("from", "")) == addr
                and m.get("id") not in excluded]
        mine.sort(key=_internal_ms)
        return self._rows([m["id"] for m in mine])

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

    def responses_for(self, rows: list[dict]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for row in rows:
            try:
                replies = self.reply_messages(row)
            except Exception:
                log.warning("Could not read the thread of %s.",
                            row.get("gmail_msg_id"), exc_info=True)
                continue
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
        metas = self._get_many(ids, fmt="metadata", headers=("To",))
        reply_label = self._label_id(LABEL_REPLY)
        return sum(1 for m in metas
                   if reply_label in (m.get("labelIds") or [])
                   and addr in gmail_reads._addrs(gmail_reads._headers(m).get("to", ""))
                   and _internal_ms(m) >= cutoff_ms)

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

    def search(self, query: str = "", sender: str = "", limit: int = 5,
               restrict_to_sender: str = "") -> list[dict]:
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
        super().append_outbound(recipient, subject, body, rationale,
                                message_id=message_id)
        if self._known is not None and recipient.strip():
            self._known.add(recipient.strip().casefold())

    def _outbound_since(self, hours: int) -> list[dict]:
        days = hours // 24 + 2
        cutoff_ms = int((_utc_now() - datetime.timedelta(hours=hours)).timestamp() * 1000)
        ids = self._ids(LABEL_OUTBOUND, f"newer_than:{days}d")
        metas = self._get_many(ids, fmt="metadata", headers=("To",))
        return [m for m in metas if _internal_ms(m) >= cutoff_ms]

    def outbound_today(self) -> int:
        return len(self._outbound_since(24))

    def outbound_today_to(self, recipient: str) -> int:
        addr = gmail_reads.normalise_addr(recipient)
        return sum(1 for m in self._outbound_since(24)
                   if addr in gmail_reads._addrs(gmail_reads._headers(m).get("to", "")))

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
        budget = MAX_KNOWN_FETCH
        for status in (sheets_queue.STATUS_REPLIED, sheets_queue.STATUS_IGNORED):
            ids = self._ids(STATUS_LABELS[status])[:budget]
            budget -= len(ids)
            for m in self._get_many(ids, fmt="metadata", headers=("From",)):
                addr = _single_sender(gmail_reads._headers(m).get("from", ""))
                if addr:
                    out.add(addr)
        ids = self._ids(LABEL_OUTBOUND)[:max(budget, 0)]
        budget -= len(ids)
        for m in self._get_many(ids, fmt="metadata", headers=("To", "Cc")):
            hdrs = gmail_reads._headers(m)
            out |= gmail_reads._addrs(hdrs.get("to", "")) | gmail_reads._addrs(hdrs.get("cc", ""))
        if budget <= 0:
            # Short is the safe direction — fewer people Indi may write to
            # first — but it is still wrong, so say so.
            log.warning("known_correspondents stopped at %d fetches; the set "
                        "may be incomplete.", MAX_KNOWN_FETCH)
        self._known = out
        return set(out)
