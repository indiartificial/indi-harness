"""
gmail_reads.py

Gmail-backed versions of three store readers, run in the *shadow* of the
Sheets ones (MIGRATE-GMAIL.md, step 3):

  * the sender-scoped history search behind `search_message_history`
  * `replies_in_last_day`, the per-sender rate limit
  * `known_correspondents`, who Indi may write to on its own initiative

Nothing here decides anything. `SheetStore` computes its answer from the
sheet exactly as before and returns it; when INDI_GMAIL_SHADOW_READS is on
it also hands that answer to a `ShadowReader`, which asks Gmail the same
question and logs, at WARNING, wherever the two disagree. Those log lines
are what step 4 watches before step 5 flips the readers over.

Every Gmail failure is caught in `ShadowReader` and logged, the same way
the status-label writes of step 2 are: a shadow read that raises must not
turn a reply into a failure. The readers themselves (`GmailReads`) do not
swallow anything, so that when step 5 makes them authoritative the caller
gets to decide.

Why this is a module of its own and not methods on `gmail_io`: gmail_io
is protected from self-modification, and all this needs is an
already-built Gmail service object.

Three things are deliberate, because they are where a naive port breaks
quietly:

  1. **Sender scope is enforced in Python.** The model writes part of the
     search query and Gmail's `q` is a query language, so a query holding
     its own `OR`, `from:`, `in:anywhere` or a stray bracket can widen
     whatever scope the query string expresses. The `from:` term sent to
     Gmail is an optimisation. The control is `_scope_ok`, which re-checks
     every returned message's From address — the addr-spec, case-folded,
     never the display name — before anything is returned or compared.

  2. **The 24h rate-limit window is cut in Python.** Gmail's `newer_than:`
     is coarse, so the query over-fetches two days and `internalDate` is
     compared against an exact rolling 24h cutoff, inclusive like the
     sheet's.

  3. **known_correspondents does not take Sent wholesale.** Sent holds the
     account's whole pre-Indi history, and this set gates unsolicited
     mail. It is built from the senders of messages Indi itself labelled
     Replied or Ignored — the same two statuses the sheet counts — plus
     the To/Cc recipients of mail sent at or after INDI_START_DATE. With
     no start date set, the Sent half is left out entirely rather than
     guessed at.

Cost. Each shadow read is extra Gmail traffic on top of the sheet read,
and each is bounded:

  * search: at most 1 `labels.list` (once per run, cached), 1
    `messages.list`, and `SEARCH_MAX_CANDIDATES` metadata gets — usually
    far fewer, since it stops as soon as `limit` messages pass the filter.
  * replies_in_last_day: 1 `messages.list` page and at most
    `REPLIES_MAX_FETCH` metadata gets.
  * known_correspondents: 1 `labels.list` (cached), at most
    `KNOWN_MAX_LIST_PAGES` list pages per source, and at most
    `KNOWN_MAX_FETCH` metadata gets in total. Hitting that ceiling is
    logged, because a truncated set will disagree with the sheet for a
    reason that has nothing to do with the data.

Every get is `format=metadata` with only the headers the comparison
needs. No bodies are fetched: comparing message ids is enough to tell
whether the two searches agree.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
from email.utils import getaddresses, parseaddr

log = logging.getLogger("indi.gmail_reads")

# The status labels, by name. Mirrors sheets_queue.STATUS_LABELS; kept as
# names here so this module doesn't import the store it shadows.
INDI_LABELS = ("Indi/Queued", "Indi/Replied", "Indi/Skipped",
               "Indi/Failed", "Indi/Ignored")

# The statuses that make someone a correspondent. The sheet counts
# `replied` and `ignored` senders; Skipped is automated or blocked mail and
# Failed/Queued were never actually handled, and none of those should
# widen who Indi may write to first.
KNOWN_LABELS = ("Indi/Replied", "Indi/Ignored")

SEARCH_MAX_CANDIDATES = 25
REPLIES_MAX_FETCH = 60
KNOWN_MAX_LIST_PAGES = 5
KNOWN_MAX_FETCH = 300

# Bounds on what a disagreement line may carry, so a wide result set
# can't dump the mailbox into log.txt.
LOG_MAX_ITEMS = 10
LOG_MAX_CHARS = 200

# Addresses that are safe to put into a Gmail query term as they are.
# Anything else (quotes, spaces, brackets — all legal in an addr-spec) is
# left out of the query and handled by the Python filter alone.
_QUERY_SAFE_ADDR = re.compile(r"^[a-z0-9._%+'-]+@[a-z0-9.-]+$")

_TRUE = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Settings. config.py is protected, so these two are read here, through its
# platform-aware `_env` when it has one — the same route outbound.py uses
# for INDI_HEARTBEAT_MODEL.
# ---------------------------------------------------------------------------

def _setting(name: str) -> str:
    try:
        import config as _config
        getter = getattr(_config, "_env", None)
        if callable(getter):
            return (getter(name) or "").strip()
    except Exception:  # pragma: no cover - config import can't realistically fail
        log.debug("Could not use config._env for %s", name, exc_info=True)
    return (os.environ.get(name) or "").strip()


def shadow_reads_enabled(cfg=None) -> bool:
    """INDI_GMAIL_SHADOW_READS, default false. A `gmail_shadow_reads`
    attribute on the config wins if config.py ever grows one."""
    configured = getattr(cfg, "gmail_shadow_reads", None)
    if isinstance(configured, bool):
        return configured
    return _setting("INDI_GMAIL_SHADOW_READS").lower() in _TRUE


def start_date(cfg=None) -> datetime.datetime | None:
    """INDI_START_DATE: when Indi started using this mailbox, as an ISO
    date or datetime (UTC if no offset is given). None if unset or
    unparseable, which leaves Sent out of known_correspondents."""
    configured = getattr(cfg, "start_date", None)
    raw = configured if isinstance(configured, str) else _setting("INDI_START_DATE")
    if not raw:
        return None
    try:
        when = datetime.datetime.fromisoformat(raw)
    except ValueError:
        log.warning("INDI_START_DATE=%r is not an ISO date; ignoring it.", raw[:50])
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalise_addr(value: str) -> str:
    """The addr-spec out of any header form, case-folded.
    'Alice <Alice@Example.com>' -> 'alice@example.com'. A display name is
    free text and never taken as the address, so
    'alice@example.com <mallory@evil.test>' is mallory."""
    return (parseaddr(value or "")[1] or "").strip().casefold()


def _addrs(value: str) -> set[str]:
    return {a.strip().casefold() for _, a in getaddresses([value or ""]) if a.strip()}


def _headers(meta: dict) -> dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "")
            for h in (meta.get("payload") or {}).get("headers", [])}


def _internal_dt(meta: dict) -> datetime.datetime | None:
    try:
        ms = int(meta.get("internalDate") or 0)
    except (TypeError, ValueError):
        return None
    if not ms:
        return None
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)


def _query_addr(addr: str) -> str | None:
    return addr if _QUERY_SAFE_ADDR.match(addr) else None


def _label_query(names) -> str:
    # Gmail's search spells a nested label with a hyphen: Indi/Replied is
    # label:indi-replied. Braces are OR. Only an optimisation — the label
    # ids are re-checked on every message that comes back.
    return "{" + " ".join(f"label:{n.lower().replace('/', '-')}" for n in names) + "}"


def _brief(value) -> str:
    text = repr(value)
    return text if len(text) <= LOG_MAX_CHARS else text[:LOG_MAX_CHARS] + "...'"


def _some(items) -> str:
    items = sorted(items) if isinstance(items, set) else list(items)
    shown = ", ".join(str(i) for i in items[:LOG_MAX_ITEMS])
    more = len(items) - LOG_MAX_ITEMS
    return f"[{shown}{f', +{more} more' if more > 0 else ''}]"


# ---------------------------------------------------------------------------
# The readers
# ---------------------------------------------------------------------------

class GmailReads:
    """The three readers over Gmail. Raises whatever the client raises."""

    def __init__(self, gmail, start: datetime.datetime | None = None):
        self.gmail = gmail
        self.start = start
        self._label_ids: dict[str, str] | None = None
        # Set by known_correspondents when a bound was hit, for the log.
        self.truncated = False

    # -- Gmail calls ------------------------------------------------------

    def _labels(self) -> dict[str, str]:
        if self._label_ids is None:
            listed = self.gmail.users().labels().list(userId="me").execute()
            self._label_ids = {lab["name"]: lab["id"] for lab in listed.get("labels", [])}
        return self._label_ids

    def _list(self, max_pages: int = 1, page_size: int = 100, **kw) -> tuple[list[str], bool]:
        """Message ids, newest first, over at most `max_pages` pages. The
        flag says whether more were left unread."""
        ids, token = [], None
        for _ in range(max_pages):
            args = {"userId": "me", "maxResults": page_size, **kw}
            if token:
                args["pageToken"] = token
            page = self.gmail.users().messages().list(**args).execute()
            ids.extend(m["id"] for m in page.get("messages", []))
            token = page.get("nextPageToken")
            if not token:
                return ids, False
        return ids, True

    def _meta(self, msg_id: str, headers: list[str]) -> dict:
        return self.gmail.users().messages().get(
            userId="me", id=msg_id, format="metadata", metadataHeaders=headers,
        ).execute()

    # -- search -----------------------------------------------------------

    def search_ids(self, query: str = "", sender: str = "", limit: int = 5,
                   restrict_to_sender: str = "") -> list[str]:
        """Ids of messages Indi has handled that match, newest first — the
        Gmail counterpart of `SheetStore.search`, reduced to ids because
        ids are all the comparison needs.

        "Handled" means carrying one of the Indi/* labels, which is what
        makes this comparable with the Queue sheet: the sheet only ever
        holds mail Indi processed.
        """
        hard = normalise_addr(restrict_to_sender) if restrict_to_sender else ""
        soft = (sender or "").lower().strip()
        indi_ids = {self._labels().get(n) for n in INDI_LABELS} - {None}
        if not indi_ids:
            return []

        parts = []
        if hard and _query_addr(hard):
            parts.append(f"from:{hard}")
        parts.append(_label_query(INDI_LABELS))
        q_user = (query or "").strip()
        if q_user:
            # Parenthesised so a leading OR in the model's text doesn't bind
            # to our terms. Not a control: _scope_ok below is.
            parts.append(f"({q_user})")
        candidates, _ = self._list(q=" ".join(parts), page_size=SEARCH_MAX_CANDIDATES)

        want = max(1, limit)
        kept: list[tuple[datetime.datetime, str]] = []
        for msg_id in candidates[:SEARCH_MAX_CANDIDATES]:
            meta = self._meta(msg_id, ["From"])
            if not indi_ids.intersection(meta.get("labelIds") or []):
                continue
            from_raw = _headers(meta).get("from", "")
            if not self._scope_ok(from_raw, hard):
                continue
            if soft and soft not in f"{from_raw} {normalise_addr(from_raw)}".lower():
                continue
            kept.append((_internal_dt(meta) or datetime.datetime.min.replace(
                tzinfo=datetime.timezone.utc), meta.get("id", msg_id)))
            if len(kept) >= want:
                break
        kept.sort(key=lambda pair: pair[0], reverse=True)
        return [msg_id for _, msg_id in kept[:want]]

    @staticmethod
    def _scope_ok(from_raw: str, hard: str) -> bool:
        """The sender scope. With no restriction everything passes; with
        one, only a message whose From holds exactly one address and that
        address is this one. A From naming several people is nobody's own
        mail for this purpose, whichever of them parseaddr would pick."""
        if not hard:
            return True
        found = [a for _, a in getaddresses([from_raw or ""]) if a.strip()]
        return len(found) == 1 and normalise_addr(from_raw) == hard

    # -- replies_in_last_day ---------------------------------------------

    def replies_in_last_day(self, sender_addr: str,
                            now: datetime.datetime | None = None) -> int:
        """Mail sent to this address in the rolling 24h before `now`."""
        now = now or datetime.datetime.now(datetime.timezone.utc)
        cutoff = now - datetime.timedelta(days=1)
        addr = normalise_addr(sender_addr)
        if not addr:
            return 0
        q = "in:sent newer_than:2d"
        if _query_addr(addr):
            q += f" to:{addr}"
        ids, _ = self._list(q=q, page_size=REPLIES_MAX_FETCH)
        count = 0
        for msg_id in ids[:REPLIES_MAX_FETCH]:
            meta = self._meta(msg_id, ["To"])
            if "SENT" not in (meta.get("labelIds") or []):
                continue
            if addr not in _addrs(_headers(meta).get("to", "")):
                continue
            when = _internal_dt(meta)
            if when is not None and when >= cutoff:
                count += 1
        return count

    # -- known_correspondents --------------------------------------------

    def known_correspondents(self) -> set[str]:
        self.truncated = False
        budget = KNOWN_MAX_FETCH
        out: set[str] = set()

        labels = self._labels()
        want = {labels[n] for n in KNOWN_LABELS if n in labels}
        labelled: dict[str, None] = {}   # ordered, deduplicated
        for name in KNOWN_LABELS:
            if name not in labels:
                continue
            ids, more = self._list(max_pages=KNOWN_MAX_LIST_PAGES, page_size=500,
                                   labelIds=[labels[name]])
            self.truncated |= more
            labelled.update(dict.fromkeys(ids))
        for msg_id in labelled:
            if budget <= 0:
                self.truncated = True
                break
            budget -= 1
            meta = self._meta(msg_id, ["From"])
            if not want.intersection(meta.get("labelIds") or []):
                continue
            addr = normalise_addr(_headers(meta).get("from", ""))
            if addr:
                out.add(addr)

        if self.start is not None:
            after = int(self.start.timestamp())
            ids, more = self._list(max_pages=KNOWN_MAX_LIST_PAGES, page_size=500,
                                   q=f"in:sent after:{after}")
            self.truncated |= more
            for msg_id in ids:
                if budget <= 0:
                    self.truncated = True
                    break
                budget -= 1
                meta = self._meta(msg_id, ["To", "Cc"])
                if "SENT" not in (meta.get("labelIds") or []):
                    continue
                when = _internal_dt(meta)
                if when is None or when < self.start:
                    continue
                hdrs = _headers(meta)
                out |= _addrs(hdrs.get("to", "")) | _addrs(hdrs.get("cc", ""))
        return out


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------

class ShadowReader:
    """Runs a Gmail reader beside a sheet answer and logs disagreement.

    Every public method returns None and never raises: its only effect is
    on the log.
    """

    def __init__(self, gmail, start: datetime.datetime | None = None):
        self.reads = GmailReads(gmail, start)

    def compare_search(self, sheet_ids: list[str], query: str, sender: str,
                       limit: int, restrict_to_sender: str) -> None:
        inputs = (f"query={_brief(query)} sender={_brief(sender)} limit={limit} "
                  f"restrict_to_sender={_brief(restrict_to_sender)}")
        try:
            gmail_ids = self.reads.search_ids(query, sender, limit, restrict_to_sender)
            if gmail_ids == list(sheet_ids):
                return
            sheet_set, gmail_set = set(sheet_ids), set(gmail_ids)
            if sheet_set == gmail_set:
                detail = (f"same {len(gmail_set)} ids, different order: "
                          f"sheet {_some(sheet_ids)}, gmail {_some(gmail_ids)}")
            else:
                detail = (f"sheet returned {len(sheet_ids)}, gmail {len(gmail_ids)}; "
                          f"sheet only {_some(sheet_set - gmail_set)}, "
                          f"gmail only {_some(gmail_set - sheet_set)}")
            log.warning("Shadow read disagreement [search] %s: %s", inputs, detail)
        except Exception:
            log.warning("Shadow read failed [search] %s; the sheet's answer "
                        "stands.", inputs, exc_info=True)

    def compare_replies_in_last_day(self, sheet_count: int, sender_addr: str,
                                    now: datetime.datetime) -> None:
        inputs = f"sender_addr={_brief(sender_addr)} now={now.isoformat()}"
        try:
            gmail_count = self.reads.replies_in_last_day(sender_addr, now)
            if gmail_count != sheet_count:
                log.warning("Shadow read disagreement [replies_in_last_day] %s: "
                            "sheet %d, gmail %d", inputs, sheet_count, gmail_count)
        except Exception:
            log.warning("Shadow read failed [replies_in_last_day] %s; the "
                        "sheet's answer stands.", inputs, exc_info=True)

    def compare_known_correspondents(self, sheet_set: set[str]) -> None:
        try:
            gmail_set = self.reads.known_correspondents()
            sheet_norm = {a.casefold() for a in sheet_set}
            if gmail_set == sheet_norm:
                return
            # Expected to differ until the label backfill has run and the
            # start date is set; this line is how step 4 sees by how much.
            log.warning(
                "Shadow read disagreement [known_correspondents] "
                "start_date=%s%s: sheet %d, gmail %d; sheet only %s (%d), "
                "gmail only %s (%d)",
                self.reads.start.isoformat() if self.reads.start else "unset",
                " (gmail side truncated at its fetch bound)" if self.reads.truncated else "",
                len(sheet_norm), len(gmail_set),
                _some(sheet_norm - gmail_set), len(sheet_norm - gmail_set),
                _some(gmail_set - sheet_norm), len(gmail_set - sheet_norm),
            )
        except Exception:
            log.warning("Shadow read failed [known_correspondents]; the sheet's "
                        "answer stands.", exc_info=True)
