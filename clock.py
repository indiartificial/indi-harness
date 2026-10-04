"""
clock.py

The time Indi believes it is. In production that is simply now. In a
STAGING copy of the harness (INDI_STAGING=true), a test driver can set a
virtual time — "it is 7 January 2027, 09:00 Eastern" — and Indi lives in
it: the date in its instructions, when follow-ups fall due, the 72-hour
waits, permissions, the ledger, memory stamps.

The virtual time is an OFFSET from real time, set in
data/virtual_clock.json as {"now": <virtual ISO time>, "set_at": <real ISO
time when it was set>}. So the clock keeps running between steps, and a
driver moves it forward by writing a later "now".

Deliberately left on real time, whatever the setting:
  * money — the cost ledger and every spending window, because the money
    is real (costs.py, claude_api's cost tools, spend_since);
  * Gmail's own timestamps and the windows compared against them
    (gmail_reads, gmail_store), because Gmail can't be told the time;
  * alert rate limits and branch names (alerts.py, self_code, claude_code).

Without INDI_STAGING=true the file is ignored, so the production harness
can never leave real time — even if the file appears. This module is
protected from self-modification.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("indi.clock")

FILE = Path(__file__).resolve().parent / "data" / "virtual_clock.json"
_UTC = datetime.timezone.utc


def staging() -> bool:
    value = (os.environ.get("INDI_STAGING") or "").strip().lower()
    if not value:
        try:
            import gmail_reads
            value = gmail_reads._setting("INDI_STAGING").lower()
        except Exception:
            value = ""
    return value in ("1", "true", "yes", "on")


def _real() -> datetime.datetime:
    return datetime.datetime.now(_UTC)


def offset(path: Path | None = None) -> datetime.timedelta:
    """Virtual minus real time; zero unless this is a staging copy with a
    clock file."""
    if not staging():
        return datetime.timedelta(0)
    path = path or FILE
    try:
        data = json.loads(path.read_text())
        virtual = datetime.datetime.fromisoformat(data["now"])
        set_at = datetime.datetime.fromisoformat(data["set_at"])
    except FileNotFoundError:
        return datetime.timedelta(0)
    except Exception:
        log.warning("Unreadable %s; using real time.", path)
        return datetime.timedelta(0)
    if virtual.tzinfo is None or set_at.tzinfo is None:
        log.warning("%s needs times with an offset; using real time.", path)
        return datetime.timedelta(0)
    return virtual - set_at


def now(tz: datetime.tzinfo | None = _UTC) -> datetime.datetime:
    """The current (possibly virtual) time, in `tz` (UTC by default; None
    for the server's local zone)."""
    current = _real() + offset()
    return current.astimezone(tz) if tz is not None else current.astimezone()


def now_iso() -> str:
    return now().isoformat()


def virtual() -> bool:
    return offset() != datetime.timedelta(0)


def set_virtual(when: datetime.datetime, path: Path | None = None) -> None:
    """Make it `when` now (staging only; used by the test driver)."""
    if when.tzinfo is None:
        raise ValueError("give the virtual time with an offset")
    path = path or FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"now": when.isoformat(), "set_at": _real().isoformat()}))


def clear(path: Path | None = None) -> None:
    (path or FILE).unlink(missing_ok=True)
