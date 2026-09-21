"""
correspondents.py

Who has been told about the Terms of Service, and who has accepted them.

One row per email address, lowercased, in a SQLite file under `data/`.
This is the only state in the harness that has to survive being wrong:
an acceptance that gets lost means asking a person to sign the same
agreement twice, and a decline that gets lost means writing to someone
who asked to be left alone. Sheets — where the rest of the harness keeps
its records — is a fine log and a poor database: it is remote, it is
rate-limited, it has no primary key, and a run that cannot reach the
network would have to choose between blocking and failing open. SQLite
is local, transactional, and fails closed by simply not being there.

The path is resolved here rather than in `config.py` on purpose. Every
other path in the harness is a setting because the operator might
reasonably move it; this one is the gate's own bookkeeping, and the gate
is not something a setting should be able to relocate or disable.
INDI_CORRESPONDENTS_DB is honoured because the tests need somewhere to
write, but there is no default outside the harness directory.

Status values, and what the gate does with each, are in `terms_gate.py`:

    unknown     never written to; has not been sent the terms
    terms_sent  has been sent the terms, has not answered
    accepted    has replied "I AGREE"
    declined    has replied "DECLINE"
"""

from __future__ import annotations

import datetime
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "correspondents.db"

STATUS_UNKNOWN = "unknown"
STATUS_TERMS_SENT = "terms_sent"
STATUS_ACCEPTED = "accepted"
STATUS_DECLINED = "declined"

ALL_STATUSES = (STATUS_UNKNOWN, STATUS_TERMS_SENT, STATUS_ACCEPTED,
                STATUS_DECLINED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS correspondents (
    address                TEXT PRIMARY KEY,
    status                 TEXT NOT NULL DEFAULT 'unknown',
    first_seen             TEXT NOT NULL DEFAULT '',
    terms_sent_at          TEXT NOT NULL DEFAULT '',
    terms_version_sent     TEXT NOT NULL DEFAULT '',
    terms_sent_count       INTEGER NOT NULL DEFAULT 0,
    accepted_at            TEXT NOT NULL DEFAULT '',
    terms_version_accepted TEXT NOT NULL DEFAULT '',
    declined_at            TEXT NOT NULL DEFAULT '',
    last_seen              TEXT NOT NULL DEFAULT '',
    message_count          INTEGER NOT NULL DEFAULT 0
)
"""

log = logging.getLogger("indi.correspondents")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def normalise_address(address: str) -> str:
    return (address or "").strip().lower()


@dataclass(frozen=True)
class Correspondent:
    """One address's standing with the Service.

    Frozen: callers read it and call a store method to change it, rather
    than mutating a copy and hoping someone writes it back.
    """

    address: str
    status: str = STATUS_UNKNOWN
    first_seen: str = ""
    terms_sent_at: str = ""
    terms_version_sent: str = ""
    terms_sent_count: int = 0
    accepted_at: str = ""
    terms_version_accepted: str = ""
    declined_at: str = ""
    last_seen: str = ""
    message_count: int = 0

    @property
    def has_accepted(self) -> bool:
        return self.status == STATUS_ACCEPTED

    def accepted_current_version(self, version: str) -> bool:
        """Accepted, and accepted the version now in force.

        An unknown current version (the terms failed to load) counts as
        current for someone who has already accepted: the harness cannot
        tell them about a new version it cannot read, and cutting off a
        correspondent who did everything asked of them would be the
        wrong kind of failing closed.
        """
        if not self.has_accepted:
            return False
        if not version:
            return True
        return self.terms_version_accepted == version


class CorrespondentStore:
    """Read/write access to the correspondent table.

    `read_only` is for --dry-run: every write becomes a logged no-op, so
    a dry run can walk the whole gate without recording an acceptance
    that never actually went out.
    """

    def __init__(self, path: Path | str | None = None, read_only: bool = False):
        if path is None:
            override = (os.environ.get("INDI_CORRESPONDENTS_DB") or "").strip()
            path = Path(override).expanduser() if override else DEFAULT_DB_PATH
        self.path = Path(path)
        self.read_only = read_only
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    # --- reading ---------------------------------------------------------

    def get(self, address: str) -> Correspondent:
        """This address's record. An address never seen before is
        returned as `unknown` rather than None, so callers never have to
        write the same "no row means unknown" branch twice."""
        addr = normalise_address(address)
        row = self._conn.execute(
            "SELECT * FROM correspondents WHERE address = ?", (addr,)
        ).fetchone()
        if row is None:
            return Correspondent(address=addr)
        return Correspondent(
            address=row["address"],
            status=row["status"],
            first_seen=row["first_seen"],
            terms_sent_at=row["terms_sent_at"],
            terms_version_sent=row["terms_version_sent"],
            terms_sent_count=row["terms_sent_count"],
            accepted_at=row["accepted_at"],
            terms_version_accepted=row["terms_version_accepted"],
            declined_at=row["declined_at"],
            last_seen=row["last_seen"],
            message_count=row["message_count"],
        )

    def all(self) -> list[Correspondent]:
        rows = self._conn.execute(
            "SELECT address FROM correspondents ORDER BY address"
        ).fetchall()
        return [self.get(row["address"]) for row in rows]

    # --- writing ---------------------------------------------------------

    def _update(self, address: str, **fields) -> Correspondent:
        addr = normalise_address(address)
        if not addr:
            raise ValueError("Cannot record a correspondent with no address.")
        if self.read_only:
            log.debug("[read-only] would set %s on %s",
                      ", ".join(sorted(fields)), addr)
            return self.get(addr)

        self._conn.execute(
            "INSERT OR IGNORE INTO correspondents (address, first_seen) "
            "VALUES (?, ?)", (addr, _now_iso()),
        )
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            self._conn.execute(
                f"UPDATE correspondents SET {assignments} WHERE address = ?",
                (*fields.values(), addr),
            )
        self._conn.commit()
        return self.get(addr)

    def touch(self, address: str) -> Correspondent:
        """Record that this address just wrote in, and return the record
        as it stood *before* this message was counted — the gate decides
        on the standing they arrived with, not the one they leave with."""
        addr = normalise_address(address)
        before = self.get(addr)
        if self.read_only:
            return before
        now = _now_iso()
        self._conn.execute(
            "INSERT OR IGNORE INTO correspondents (address, first_seen) "
            "VALUES (?, ?)", (addr, now),
        )
        self._conn.execute(
            "UPDATE correspondents SET last_seen = ?, "
            "message_count = message_count + 1 WHERE address = ?", (now, addr),
        )
        self._conn.commit()
        return before

    def record_terms_sent(self, address: str, version: str) -> Correspondent:
        """The terms have gone out. Called only after the send succeeded.

        `terms_sent_count` is what stops the re-send in clause (f) of the
        gate becoming a loop: it counts sends, not attempts, and the gate
        refuses to send once it reaches its ceiling.
        """
        current = self.get(address)
        status = current.status
        # Someone who has already accepted and is being re-papered with a
        # newer version keeps their accepted standing; clause 8.2 of the
        # terms makes continued use the acceptance.
        if status != STATUS_ACCEPTED:
            status = STATUS_TERMS_SENT
        return self._update(
            address,
            status=status,
            terms_sent_at=_now_iso(),
            terms_version_sent=version,
            terms_sent_count=current.terms_sent_count + 1,
        )

    def record_acceptance(self, address: str, version: str) -> Correspondent:
        return self._update(
            address,
            status=STATUS_ACCEPTED,
            accepted_at=_now_iso(),
            terms_version_accepted=version,
            declined_at="",
        )

    def record_decline(self, address: str) -> Correspondent:
        return self._update(
            address,
            status=STATUS_DECLINED,
            declined_at=_now_iso(),
        )

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            log.debug("Closing the correspondent database failed", exc_info=True)
