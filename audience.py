"""
audience.py

Phase 2, groundwork: who may see what. See the design doc "Indi Phase 2 —
Revised Design: Shared Memory with Consent".

The rule, enforced here in code rather than asked of the model:

    Indi may show a message to a set of people only if every one of them
    was in that message's audience, or its owner has granted it to them.

AUDIENCE
--------
A message's audience is fixed when it is sent and never changes:

  * everyone on its From, To and Cc lines, less Indi's own addresses —
    writing to them was the sharing, and whatever the message quotes
    they have seen too;
  * except when Indi is on none of To or Cc — a Bcc, or delivery through
    a list or a forward — in which case the visible recipients don't know
    Indi has it, and the audience is the sender alone.

Because it is fixed per message, a thread whose Cc grows or shrinks needs
no special handling: Carol added on the fifth message is in the audience
of the fifth and later, not of the four before; someone dropped keeps
what they already had and gets nothing after.

Indi's own messages have an audience too — their To and Cc — which is
what stops a private reply to Bob, which may repeat Bob's news, from
turning up in a search on Ann's behalf.

GRANTS
------
A grant widens one owner's message, or all of that owner's messages, to
more people. Grants are records in the `Grants` table (a Drive CSV, or a
tab while records are still in Sheets), last until withdrawn, and in this
phase are created only by the operator with grants.py; consultation will
create them later. Every change is a new row or a filled-in `revoked_at`,
so the history of who could see what is never lost.

This module holds the boundary Phase 2 relies on, and is protected from
self-modification like the terms gate.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from dataclasses import dataclass
from email.utils import getaddresses

log = logging.getLogger("indi.audience")

GRANTS_TAB = "Grants"
GRANTS_HEADERS = ["grant_id", "owner", "scope", "to", "granted_at",
                  "evidence", "revoked_at", "note"]
ALL_ITEMS = "*"


def addrs(value: str) -> set[str]:
    return {a.strip().lower() for _, a in getaddresses([value or ""]) if a and "@" in a}


def single(value: str) -> str:
    found = addrs(value)
    return next(iter(found)) if len(found) == 1 else ""


def incoming_audience(from_raw: str, to_raw: str, cc_raw: str,
                      selves) -> frozenset[str]:
    """Everyone a message someone sent to Indi was shared with.

    With Indi's own address unknown, nothing can be told about Bcc, so the
    answer is the narrowest one: the sender alone.
    """
    selves = {s.lower() for s in (selves or ()) if s}
    sender = single(from_raw)
    if not sender:
        return frozenset()
    visible = addrs(to_raw) | addrs(cc_raw)
    if not selves or not (visible & selves):
        return frozenset({sender})
    return frozenset(({sender} | visible) - selves)


def outgoing_audience(to_raw: str, cc_raw: str, selves) -> frozenset[str]:
    """Everyone a message Indi sent went to."""
    selves = {s.lower() for s in (selves or ()) if s}
    return frozenset((addrs(to_raw) | addrs(cc_raw)) - selves)


def audience_of(meta: dict, selves) -> frozenset[str]:
    """For a cached header entry (gmail_store.MetaCache): Indi's own
    messages by To/Cc, anyone else's by the incoming rule."""
    selves = {s.lower() for s in (selves or ()) if s}
    if single(meta.get("from", "")) in selves:
        return outgoing_audience(meta.get("to", ""), meta.get("cc", ""), selves)
    return incoming_audience(meta.get("from", ""), meta.get("to", ""),
                             meta.get("cc", ""), selves)


def owner_of(meta: dict) -> str:
    return single(meta.get("from", ""))


@dataclass(frozen=True)
class Grant:
    grant_id: str
    owner: str
    scope: str          # a Gmail id, or ALL_ITEMS
    to: str
    granted_at: str
    evidence: str
    revoked_at: str
    row: int = -1
    note: str = ""              # e.g. the owner's condition, in their words

    @property
    def active(self) -> bool:
        return not self.revoked_at


class GrantBook:
    """The Grants table, read once per run, over any store with tables."""

    def __init__(self, store):
        self.store = store
        self._rows: list[Grant] | None = None

    def _table_ready(self) -> None:
        self.store._ensure_tab(self.store.responses_id, GRANTS_TAB, GRANTS_HEADERS)

    def all(self) -> list[Grant]:
        if self._rows is None:
            try:
                self._table_ready()
                raw = self.store._load(self.store.responses_id, GRANTS_TAB,
                                       declared=GRANTS_HEADERS)
            except Exception:
                # Failing closed: no grants means nothing is widened.
                log.warning("Could not read the Grants table; treating it as empty.",
                            exc_info=True)
                raw = []
            self._rows = [Grant(
                grant_id=(r.get("grant_id") or "").strip(),
                owner=(r.get("owner") or "").strip().lower(),
                scope=(r.get("scope") or "").strip() or ALL_ITEMS,
                to=(r.get("to") or "").strip().lower(),
                granted_at=r.get("granted_at", ""), evidence=r.get("evidence", ""),
                revoked_at=(r.get("revoked_at") or "").strip(),
                row=r.get("_row", -1), note=(r.get("note") or "").strip())
                for r in raw if (r.get("owner") or "").strip()]
        return list(self._rows)

    def active(self) -> list[Grant]:
        return [g for g in self.all() if g.active]

    def granted_to(self, owner: str, item_id: str) -> set[str]:
        owner = (owner or "").lower()
        return {g.to for g in self.active()
                if g.owner == owner and g.scope in (ALL_ITEMS, item_id)}

    def add(self, owner: str, to: str, scope: str = ALL_ITEMS,
            evidence: str = "", note: str = "") -> Grant:
        owner, to = owner.strip().lower(), to.strip().lower()
        if "@" not in owner or "@" not in to:
            raise ValueError("A grant needs an owner address and a recipient address.")
        record = {
            "grant_id": f"g-{uuid.uuid4().hex[:10]}", "owner": owner,
            "scope": scope or ALL_ITEMS, "to": to,
            "granted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "evidence": evidence, "revoked_at": "", "note": note[:300],
        }
        self._table_ready()
        row = self.store._append(self.store.responses_id, GRANTS_TAB, GRANTS_HEADERS, record)
        self._rows = None
        return Grant(record["grant_id"], owner, record["scope"], to,
                     record["granted_at"], evidence, "", row if isinstance(row, int) else -1,
                     record["note"])

    def revoke(self, grant_id: str) -> bool:
        """Fill in `revoked_at` on the grant's own row; the row stays."""
        from sheets_queue import _col_letter
        for g in self.all():
            if g.grant_id == grant_id and g.active and g.row > 0:
                col = _col_letter(GRANTS_HEADERS.index("revoked_at"))
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                self.store.sheets.spreadsheets().values().update(
                    spreadsheetId=self.store.responses_id,
                    range=f"{GRANTS_TAB}!{col}{g.row}:{col}{g.row}",
                    valueInputOption="RAW", body={"values": [[now]]},
                ).execute()
                # The store caches the tab for the run; keep it truthful.
                for raw in self.store._load(self.store.responses_id, GRANTS_TAB,
                                            declared=GRANTS_HEADERS):
                    if (raw.get("grant_id") or "").strip() == grant_id:
                        raw["revoked_at"] = now
                self._rows = None
                return True
        return False


def cleared(item_id: str, meta: dict, recipients, selves,
            grants: GrantBook | None) -> bool:
    """May this message be shown to every one of `recipients`?"""
    wanted = {r.lower() for r in recipients if r}
    if not wanted:
        return False
    audience = set(audience_of(meta, selves))
    if wanted <= audience:
        return True
    if grants is None:
        return False
    owner = owner_of(meta)
    if not owner or owner in {s.lower() for s in selves or ()}:
        # Indi's own messages are widened only through the message they
        # answer, never by a grant made out to Indi.
        return False
    return wanted <= audience | grants.granted_to(owner, item_id)


# What Indi is told about how sharing works, so that it describes its own
# behaviour truthfully. Kept here, beside the rule it describes, in a
# protected file: Indi can't rewrite what it is told about its own limits.
PROMPT = """
HOW SHARING WORKS (the system enforces this; it is not up to you)
- When you write, you are shown only what everyone the message goes to has already seen: mail they sent or were on the To or Cc of, plus anything its owner has agreed to share with them. Anything else is kept out of view, and you cannot pass it on, not even a hint or the bare fact of it.
- Sharing beyond that happens only by consultation: after your reply, the system may ask the owner whether something may be shared, and if they agree, you get a chance to write a follow-up. You don't decide this and can't promise it.
- The other side of the rule: what everyone the message goes to has already seen is theirs. If Bob told you something with Ann copied in, Ann may hear it from you; use it and answer questions about it freely. Being careful with private information doesn't mean refusing to discuss what was shared.
- You only ever see part of what people have told you. So never claim that someone hasn't said, told you or mentioned something ("he didn't say when he's busy", "that's the only thing he's told me"): you can't know. Say what you can see instead ("In what you've seen, Bob mentioned preferring mornings").
- So describe it plainly and never promise more or less than this. Say, for example: "What you tell me privately isn't used when I write to others, unless you agree when I ask." Don't offer to share part of someone's private information ("I'd only say you're unavailable"), and don't hint that you know something you can't say.

ACCURACY ABOUT WHO SAID WHAT
- Only say that someone said, knows or was told something if you can see where: in this thread, in your notes about this person, or in a search result. If you can't point to it, don't claim it. Ask, or say you're not sure, rather than filling a gap from other conversations.
"""
