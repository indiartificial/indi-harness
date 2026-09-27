"""
gmail_io.py

Everything that touches the shape of an email: parsing an incoming Gmail
message into a normalised dict, deciding whether it's the kind of message
we should reply to at all, and building the outgoing MIME reply.

Fixes over the previous version:
  * Header lookup is case-insensitive. `Message-Id` and `Message-ID` are
    both common in the wild; the old dict comprehension keyed on exact
    case, so a `Message-Id` meant no In-Reply-To and a broken thread.
  * HTML-only email produces a body. The old parser only collected
    `text/plain` parts, so any `text/html`-only message reached Claude
    with an empty body.
  * Quoted reply chains are split off rather than merged into the body,
    which keeps Sheets rows and memory consolidations from carrying the
    same text over and over — while still leaving the chain available to
    the caller, since for a while it was the only record of the earlier
    conversation and throwing it away left Indi answering "yes, go ahead"
    with no idea what it was agreeing to.
  * The rest of a thread can be fetched and rendered as context
    (`format_thread_context`), which is the first-hand version of what a
    sender's quoting would have shown.
  * `Reply-To` is preferred over `From` when present.
  * The `References` chain is preserved rather than replaced.
  * Automated mail (autoresponders, list traffic, our own address) is
    detected, which is what stops a send-enabled agent from getting into
    an infinite mail loop with a vacation responder.
  * Outgoing attachment filenames are sanitised before going into a
    Content-Disposition header.
"""

from __future__ import annotations

import base64
import datetime
import mimetypes
import os
import re
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser


# ---------------------------------------------------------------------------
# HTML -> TEXT
# ---------------------------------------------------------------------------

class _HTMLTextExtractor(HTMLParser):
    """Minimal HTML-to-text conversion. Deliberately dependency-free —
    good enough to give Claude something to read when a sender's client
    didn't include a text/plain alternative."""

    _BLOCK_TAGS = {
        "p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
        "table", "blockquote", "section", "article", "header", "footer",
    }
    _SKIP_TAGS = {"script", "style", "head", "title", "meta"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        raw = re.sub(r"[ \t\xa0]+", " ", raw)
        raw = re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)
        return raw.strip()


def html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # Malformed HTML shouldn't take down the run; fall back to a crude
        # tag strip rather than returning nothing.
        return re.sub(r"<[^>]+>", " ", html).strip()
    return parser.text()


# ---------------------------------------------------------------------------
# QUOTE TRIMMING
# ---------------------------------------------------------------------------

# Markers that reliably indicate the start of a quoted previous message.
# Kept conservative on purpose: over-trimming loses real content, which is
# worse than carrying a bit of quoted text.
_QUOTE_MARKERS = [
    # Gmail wraps the attribution line, so "On <date> <name> <address>" and
    # the trailing "wrote:" routinely land on different lines. Matching only
    # within one line meant this pattern missed, the marker stayed in the
    # body, and the '>' pass below then stripped everything it introduced —
    # which is how a reply arrived reading "Yes, please do.\n\nOn Sun, 20
    # Sep 2026 ... wrote:" with the entire quoted thread gone. `[\s\S]` here
    # crosses newlines; the length bound keeps it from swallowing the
    # message when some later line happens to end in "wrote:".
    re.compile(r"^[ \t>]*On\b[\s\S]{0,300}?\bwrote:[ \t\r]*$", re.MULTILINE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Forwarded message\s*-{2,}\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*_{10,}\s*$", re.MULTILINE),
    re.compile(r"^\s*Sent from my \w+\s*$", re.MULTILINE),
]


def split_quoted(body: str) -> tuple[str, str]:
    """Split a message into (newly written text, quoted chain below it).

    The quoted part used to be thrown away here. That is right for the
    Sheets row and for memory — the same text repeating down a long thread
    is noise — but it was also the only copy of the earlier conversation
    the model ever got, so a reply saying "yes, do that" above a quoted
    thread reached Claude as three words with no referent. Both halves are
    returned now; the caller decides what to show.
    """
    if not body:
        return "", ""

    cut = len(body)
    for pattern in _QUOTE_MARKERS:
        m = pattern.search(body)
        if m and m.start() < cut:
            cut = m.start()

    visible = body[:cut].rstrip()
    quoted = body[cut:].strip()

    # A trailing block of '> ...' lines is quoted material too, even when no
    # attribution line introduced it.
    lines = visible.split("\n")
    tail: list[str] = []
    while lines and (lines[-1].lstrip().startswith(">") or not lines[-1].strip()):
        tail.append(lines.pop())
    visible = "\n".join(lines).rstrip()
    if tail:
        quoted = ("\n".join(reversed(tail)).strip() + "\n" + quoted).strip()

    return visible, quoted


# ---------------------------------------------------------------------------
# PARSING AN INCOMING MESSAGE
# ---------------------------------------------------------------------------

def normalise_headers(payload: dict) -> dict:
    """Header names are case-insensitive per RFC 5322 and senders are
    inconsistent about them. Lowercase every key once, here, so nothing
    downstream has to guess."""
    out: dict[str, str] = {}
    for h in payload.get("headers", []) or []:
        name = (h.get("name") or "").strip().lower()
        if not name:
            continue
        # Keep the first occurrence; duplicates are rare and the first is
        # the one the sender's client actually set.
        out.setdefault(name, h.get("value", ""))
    return out


def _addr_of(header_value: str) -> str:
    return (parseaddr(header_value or "")[1] or "").strip().lower()


def extract_message(message: dict, max_body_chars: int,
                    max_quoted_chars: int = 0) -> dict:
    """Turns a Gmail `format=full` message into a normalised dict.

    `max_quoted_chars` is the budget for the quoted chain hanging below the
    sender's own text. Zero — the default, and what the Sheets row and
    memory want — discards it.
    """
    payload = message.get("payload", {}) or {}
    headers = normalise_headers(payload)

    text_parts: list[str] = []
    html_parts: list[str] = []
    attachment_refs: list[dict] = []

    def decode(data: str) -> str:
        text = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        # Mail arrives CRLF-terminated. Normalising here means every
        # pattern below can anchor on "\n" alone — a line-ending the quote
        # markers didn't allow for was enough, on its own, to leave a whole
        # quoted thread unrecognised.
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def walk(part: dict) -> None:
        filename = part.get("filename")
        part_body = part.get("body", {}) or {}
        mime_type = part.get("mimeType", "")

        if filename and part_body.get("attachmentId"):
            attachment_refs.append(
                {
                    "filename": filename,
                    "mime_type": mime_type or "application/octet-stream",
                    "attachment_id": part_body["attachmentId"],
                    "size": int(part_body.get("size", 0) or 0),
                }
            )
        elif mime_type == "text/plain" and "data" in part_body:
            text_parts.append(decode(part_body["data"]))
        elif mime_type == "text/html" and "data" in part_body:
            html_parts.append(decode(part_body["data"]))

        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)

    body = "\n".join(p for p in text_parts if p.strip()).strip()
    if not body and html_parts:
        body = html_to_text("\n".join(html_parts))
    if not body:
        # Single-part message with the content on the top-level body.
        top = payload.get("body", {}) or {}
        if "data" in top:
            raw = decode(top["data"])
            body = html_to_text(raw) if payload.get("mimeType") == "text/html" else raw

    body, quoted = split_quoted(body.strip())
    if not body and quoted:
        # Nothing but a quoted chain — a bare forward. Show it as the
        # message rather than reporting an empty body.
        body, quoted = quoted, ""

    truncated = False
    if len(body) > max_body_chars:
        body = body[:max_body_chars] + "\n\n[... message truncated ...]"
        truncated = True

    quoted_truncated = False
    if max_quoted_chars <= 0:
        quoted = ""
    elif len(quoted) > max_quoted_chars:
        # Keep the top of the chain: it is the most recent exchange, and
        # the deepest nesting at the bottom is the least useful.
        quoted = quoted[:max_quoted_chars] + "\n\n[... quoted history truncated ...]"
        quoted_truncated = True

    from_raw = headers.get("from", "")
    reply_to_raw = headers.get("reply-to", "")
    # Reply-To wins when the sender set one — that's the whole point of it.
    to_raw = reply_to_raw or from_raw

    try:
        internal_date = int(message.get("internalDate", 0) or 0)
    except (TypeError, ValueError):
        internal_date = 0

    return {
        "gmail_msg_id": message.get("id", ""),
        "thread_id": message.get("threadId", ""),
        "headers": headers,
        "subject": headers.get("subject", "(no subject)"),
        "from_raw": from_raw,
        "from_addr": _addr_of(from_raw),
        "reply_to_raw": to_raw,
        "reply_to_addr": _addr_of(to_raw),
        "message_id": headers.get("message-id", ""),
        "references": headers.get("references", ""),
        "body": body,
        "body_truncated": truncated,
        "quoted": quoted,
        "quoted_truncated": quoted_truncated,
        "attachment_refs": attachment_refs,
        "internal_date": internal_date,
    }


# ---------------------------------------------------------------------------
# THE REST OF THE THREAD
# ---------------------------------------------------------------------------
#
# A reply arrives holding only its own new sentences: everything earlier is
# either quoted below it (and stripped above, as noise) or not present at
# all. The earlier messages do exist, though — Gmail keeps them in the same
# thread — so they can be fetched and handed over as real, first-hand text
# rather than inferred from whatever the sender's client chose to quote.


def _thread_timestamp(internal_date_ms: int) -> str:
    if not internal_date_ms:
        return "date unknown"
    try:
        when = datetime.datetime.fromtimestamp(
            internal_date_ms / 1000, tz=datetime.timezone.utc
        )
    except (OverflowError, OSError, ValueError):
        return "date unknown"
    return when.strftime("%Y-%m-%d %H:%M UTC")


def format_thread_context(raw_messages: list[dict], exclude_msg_id: str = "",
                          self_addrs: set[str] | None = None,
                          max_messages: int = 10,
                          max_chars: int = 12000) -> tuple[str, int]:
    """Render the earlier messages of a thread, oldest first.

    Returns (text, count). Only each message's own new text is used, so a
    thread of ten replies reads as ten entries rather than ten nested
    copies of itself. When the budget binds, the oldest go first.
    """
    selves = {a.lower() for a in (self_addrs or set())}
    entries: list[str] = []

    ordered = sorted(raw_messages or [],
                     key=lambda m: int(m.get("internalDate", 0) or 0))
    for raw in ordered:
        if exclude_msg_id and raw.get("id") == exclude_msg_id:
            continue
        try:
            parsed = extract_message(raw, max_body_chars=max_chars)
        except Exception:
            # One unparseable message in a thread shouldn't cost us the
            # rest of the conversation.
            continue
        text = (parsed["body"] or "").strip()
        if not text:
            continue
        who = parsed["from_raw"] or parsed["from_addr"] or "(unknown sender)"
        if parsed["from_addr"] and parsed["from_addr"] in selves:
            who = f"{who} — this was you"
        entries.append(f"[{_thread_timestamp(parsed['internal_date'])}] "
                       f"{who}:\n{text}")

    if not entries:
        return "", 0

    entries = entries[-max_messages:] if max_messages > 0 else entries

    separator = "\n\n"
    while len(entries) > 1 and \
            len(separator.join(entries)) > max_chars:
        entries.pop(0)
    text = separator.join(entries)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[... earlier thread truncated ...]"
    return text, len(entries)


# Lines a mail client generates rather than a person: they differ between
# the fetched copy of a message and a quotation of it, so comparing them
# would report a difference in wording where there is none in substance.
# Note how narrowly the "On ..." arm is drawn: it needs an address on the
# line. "On thinking: the mechanism already exists" is prose, and dropping
# it from one copy of a message but not the other desynchronises the two
# word streams and reports a difference that isn't there.
_ATTRIBUTION_LINE = re.compile(
    r"^(?:.*\bwrote:|On\b.{0,200}@\S+.*|-{2,}.*-{2,}|_{4,}"
    r"|(?:From|Sent|To|Cc|Subject|Date)\s*:.*)$",
    re.IGNORECASE,
)


def _comparable_text(text: str) -> str:
    """One whitespace-normalised string, quote markers and client-generated
    lines removed. Flattening to a single line is what makes the comparison
    survive re-wrapping: a quotation of a message is the same words as the
    message, broken across lines at a different width."""
    kept = []
    for line in (text or "").splitlines():
        line = re.sub(r"^[ \t>]+", "", line).strip()
        if not line or _ATTRIBUTION_LINE.match(line):
            continue
        kept.append(line)
    return re.sub(r"\s+", " ", " ".join(kept)).strip().lower()


def quoted_adds_context(quoted: str, thread_text: str) -> bool:
    """True when the quoted chain holds substance the fetched thread does
    not — a conversation forwarded from some other mailbox, typically.

    Without this check every reply would carry its history twice: once as
    fetched thread messages and again as the sender's quotation of them.
    """
    q = _comparable_text(quoted)
    if len(q) < 40:
        return False
    if not (thread_text or "").strip():
        return True

    haystack = _comparable_text(thread_text)
    words = q.split()
    # Overlapping runs of words, sampled rather than exhaustive — enough to
    # tell "the same conversation again" from "something else entirely".
    shingles = [" ".join(words[i:i + 8])
                for i in range(0, max(1, len(words) - 7), 4)]
    missing = sum(1 for s in shingles if s not in haystack)
    return missing > len(shingles) * 0.25


# ---------------------------------------------------------------------------
# SENDER AUTHENTICATION
# ---------------------------------------------------------------------------

# Gmail adds an Authentication-Results header recording what SPF, DKIM and
# DMARC said about the message. This matters the moment permissions are
# granted by address: the From header is free text that anyone can set, so
# without checking these, "only alice@example.com may run code" really
# means "anyone willing to type alice@example.com may run code".
_AUTH_RESULT = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*([a-z]+)", re.IGNORECASE)


def authentication_results(headers: dict) -> dict:
    raw = headers.get("authentication-results", "") or ""
    out: dict[str, str] = {}
    for mechanism, verdict in _AUTH_RESULT.findall(raw):
        out.setdefault(mechanism.lower(), verdict.lower())
    return out


def sender_is_authenticated(msg: dict) -> tuple[bool, str]:
    """Returns (passed, explanation).

    Requires DMARC to pass, or — when the sending domain publishes no
    DMARC policy — DKIM or SPF to pass. A missing header means we cannot
    tell, which is treated as a failure for privileged tiers.
    """
    results = authentication_results(msg.get("headers", {}))
    if not results:
        return False, "no Authentication-Results header (cannot verify sender)"

    dmarc = results.get("dmarc", "")
    dkim = results.get("dkim", "")
    spf = results.get("spf", "")

    if dmarc == "pass":
        return True, "dmarc=pass"
    if dmarc == "fail":
        return False, "dmarc=fail (sender address is probably forged)"
    if dkim == "pass" or spf == "pass":
        return True, f"dkim={dkim or 'none'} spf={spf or 'none'}"
    return False, f"dkim={dkim or 'none'} spf={spf or 'none'} dmarc={dmarc or 'none'}"


# ---------------------------------------------------------------------------
# SHOULD WE REPLY AT ALL?
# ---------------------------------------------------------------------------

# Headers that mark a message as machine-generated. Replying to any of
# these is how mail loops start.
_AUTOMATED_HEADERS = (
    "auto-submitted",
    "x-auto-response-suppress",
    "x-autoreply",
    "x-autorespond",
    "x-indi-agent",          # our own outgoing marker, in case of a bounce
    "list-id",
    "list-unsubscribe",
    "list-post",
    "mailing-list",
)

_BULK_PRECEDENCE = {"bulk", "list", "junk", "auto_reply"}

_NOREPLY_PATTERN = re.compile(
    r"(^|[._\-+])(no-?reply|do-?not-?reply|donotreply|postmaster|mailer-daemon|bounce)",
    re.IGNORECASE,
)


def classify_message(msg: dict, self_addrs: set[str]) -> tuple[bool, str]:
    """Returns (should_reply, reason_if_not).

    This is the single most important guard in the system once
    INDI_SEND_AUTOMATICALLY is on. Without it, one vacation autoresponder
    on the other end produces an unbounded reply loop that runs until the
    API balance or Gmail's sending quota stops it.
    """
    headers = msg["headers"]
    from_addr = msg["from_addr"]
    reply_addr = msg["reply_to_addr"]

    if not from_addr:
        return False, "no parseable From address"

    if from_addr in self_addrs or reply_addr in self_addrs:
        return False, "message is from this account (loop guard)"

    # Don't reply into a thread we'd be talking to ourselves in.
    delivered_to = {
        addr.lower()
        for _, addr in getaddresses([headers.get("delivered-to", "")])
        if addr
    }
    if headers.get("x-indi-agent"):
        return False, "message carries our own agent marker (loop guard)"

    auto_submitted = (headers.get("auto-submitted", "") or "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return False, f"Auto-Submitted: {auto_submitted}"

    precedence = (headers.get("precedence", "") or "").strip().lower()
    if precedence in _BULK_PRECEDENCE:
        return False, f"Precedence: {precedence}"

    for header in _AUTOMATED_HEADERS:
        if header == "auto-submitted":
            continue
        if headers.get(header):
            return False, f"automated header: {header}"

    if _NOREPLY_PATTERN.search(from_addr.split("@", 1)[0]):
        return False, "no-reply style sender address"

    subject = (msg.get("subject") or "").strip().lower()
    if subject.startswith(("out of office", "automatic reply", "undeliverable",
                           "delivery status notification", "mail delivery")):
        return False, f"autoresponder subject: {subject[:40]}"

    if (not (msg.get("body") or "").strip()
            and not (msg.get("quoted") or "").strip()
            and not msg.get("attachment_refs")):
        return False, "empty message body and no attachments"

    # `delivered_to` is collected above for future use (e.g. detecting
    # alias-based loops); referencing it keeps linters quiet without
    # changing behaviour.
    del delivered_to

    return True, ""


# ---------------------------------------------------------------------------
# BUILDING THE REPLY
# ---------------------------------------------------------------------------

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._\- ]")

# Extensions we cannot honestly produce: the attachment mechanism carries
# text, so labelling text as a PDF or spreadsheet would be a lie to the
# recipient's mail client.
_BINARY_EXTENSIONS = {
    ".pdf", ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt", ".zip",
    ".gz", ".tar", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp3", ".mp4",
}


def sanitise_filename(name: str) -> str:
    """Strip path separators and control characters before a filename goes
    into a MIME header."""
    name = os.path.basename((name or "").strip().replace("\\", "/"))
    name = _UNSAFE_FILENAME.sub("_", name)
    name = name.strip("._ ") or "attachment.txt"
    if len(name) > 120:
        stem, ext = os.path.splitext(name)
        name = stem[: 120 - len(ext)] + ext
    return name


def _attachment_part(filename: str, content: str):
    """Build one MIME part, labelling it honestly."""
    ext = os.path.splitext(filename)[1].lower()
    guessed, _ = mimetypes.guess_type(filename)

    if ext in _BINARY_EXTENSIONS:
        # We only have text. Rename rather than mislabel.
        filename = os.path.splitext(filename)[0] + ".txt"
        guessed = "text/plain"

    if guessed and guessed.startswith("text/"):
        subtype = guessed.split("/", 1)[1]
        part = MIMEText(content, _subtype=subtype, _charset="utf-8")
    elif guessed == "application/json" or ext == ".json":
        part = MIMEText(content, _subtype="json", _charset="utf-8")
    else:
        part = MIMEApplication(content.encode("utf-8"), _subtype="octet-stream")

    part.add_header("Content-Disposition", "attachment", filename=filename)
    return part, filename


def build_reply_mime(to_addr: str, subject: str, body: str, in_reply_to: str,
                     references: str = "", attachments=None,
                     from_addr: str = "", cc=None) -> dict:
    """Returns the Gmail API message body dict (without threadId — the
    caller adds it, so this function stays testable on its own)."""
    if not subject:
        subject = "(no subject)"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    if attachments:
        msg = MIMEMultipart()
        msg.attach(MIMEText(body, _charset="utf-8"))
        for filename, content in attachments:
            part, _ = _attachment_part(filename, content)
            msg.attach(part)
    else:
        msg = MIMEText(body, _charset="utf-8")

    msg["To"] = to_addr
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    if from_addr:
        msg["From"] = from_addr

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        # Preserve the existing chain instead of replacing it, so long
        # threads keep threading in every client.
        chain = " ".join(x for x in [references.strip(), in_reply_to.strip()] if x)
        msg["References"] = chain

    # Tell well-behaved autoresponders on the other end not to reply, and
    # leave a marker our own loop guard can recognise.
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-Indi-Agent"] = "1"

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return {"raw": raw}


# ---------------------------------------------------------------------------
# STATUS LABELS  (MIGRATE-GMAIL.md, step 2)
# ---------------------------------------------------------------------------

class StatusLabeler:
    """Puts exactly one of a family of labels on a message.

    Which label means what is the store's business (`sheets_queue` holds
    the status -> label mapping); this class only knows how to make Gmail
    agree. Label ids are looked up once per run with a single
    `labels.list` and cached, so a run that labels ten messages makes one
    lookup, not ten. A label that doesn't exist yet is created the first
    time something needs to be *given* it. One that only needs to be
    *removed* is never created — a label that doesn't exist can't be on
    the message.

    Nothing here swallows errors. Whether a failure matters is the
    caller's decision, and for the store it doesn't: see
    `SheetStore._label_status`.
    """

    def __init__(self, gmail):
        self.gmail = gmail
        self._ids: dict[str, str] | None = None

    def _label_ids(self) -> dict[str, str]:
        if self._ids is None:
            listed = self.gmail.users().labels().list(userId="me").execute()
            self._ids = {lab["name"]: lab["id"] for lab in listed.get("labels", [])}
        return self._ids

    def existing_id(self, name: str) -> str | None:
        """The label's id, or None if the mailbox has no such label. Never
        creates anything, which is what a dry run needs."""
        return self._label_ids().get(name)

    def ensure_id(self, name: str) -> str:
        found = self.existing_id(name)
        if found:
            return found
        try:
            created = self.gmail.users().labels().create(
                userId="me",
                body={"name": name, "labelListVisibility": "labelShow",
                      "messageListVisibility": "show"},
            ).execute()
        except Exception:
            # Most likely another run created it since we listed. Forget
            # the cache so the next call lists again instead of retrying
            # a create that will keep failing.
            self._ids = None
            raise
        self._ids[name] = created["id"]
        return created["id"]

    def apply(self, gmail_msg_id: str, name: str, others) -> None:
        """Add `name` to the message and remove every label in `others`."""
        add = self.ensure_id(name)
        remove = [i for i in (self.existing_id(o) for o in others if o != name) if i]
        self.gmail.users().messages().modify(
            userId="me", id=gmail_msg_id,
            body={"addLabelIds": [add], "removeLabelIds": remove},
        ).execute()
