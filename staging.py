"""
staging.py

A staging copy of Indi (INDI_STAGING=true) may only send to test accounts.
`guard(gmail)` wraps the Gmail client so that every send — replies, terms,
consultations, follow-ups, reminders, notices, alike — is refused unless
each recipient is listed in INDI_STAGING_ALLOWED (comma-separated). A
refused send is logged and skipped, not raised: the rest of the run goes on.

Found by the hotel scenario, 5 October 2026: a staging copy, seeded like the
real one with the operator as its first newsletter subscriber, emailed the
operator's real address. The newsletter is now off in staging as well
(outbound.py); this is the net under everything else.

In production (no INDI_STAGING) `guard` returns the client untouched. This
module is protected from self-modification.
"""

from __future__ import annotations

import base64
import logging
from email import message_from_bytes
from email.utils import getaddresses
from types import SimpleNamespace

import clock

log = logging.getLogger("indi.staging")


def allowed() -> set[str]:
    try:
        import gmail_reads
        raw = gmail_reads._setting("INDI_STAGING_ALLOWED")
    except Exception:
        raw = ""
    return {a.strip().lower() for a in raw.split(",") if a.strip()}


def recipients(body: dict) -> list[str]:
    raw = (body or {}).get("raw") or ((body or {}).get("message") or {}).get("raw") or ""
    try:
        msg = message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:
        return ["(unreadable)"]
    return [a.lower() for _, a in getaddresses(
        msg.get_all("To", []) + msg.get_all("Cc", []) + msg.get_all("Bcc", [])) if a]


def _refused(kind: str, who: list[str], ok: set[str]):
    log.warning("STAGING: refused to %s mail to %s (not in INDI_STAGING_ALLOWED).",
                kind, ", ".join(sorted(set(who) - ok)) or "nobody")
    return SimpleNamespace(execute=lambda **_: {"id": "", "threadId": "", "message": {}})


class _Delegate:
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _Messages(_Delegate):
    def send(self, userId, body, **k):
        who, ok = recipients(body), allowed()
        if not who or not set(who) <= ok:
            return _refused("send", who, ok)
        return self._inner.send(userId=userId, body=body, **k)


class _Drafts(_Delegate):
    def create(self, userId, body, **k):
        who, ok = recipients(body), allowed()
        if not who or not set(who) <= ok:
            return _refused("draft", who, ok)
        return self._inner.create(userId=userId, body=body, **k)


class _Users(_Delegate):
    def messages(self):
        return _Messages(self._inner.messages())

    def drafts(self):
        return _Drafts(self._inner.drafts())


class _Service(_Delegate):
    def users(self):
        return _Users(self._inner.users())


def guard(gmail):
    """The Gmail client, fenced in when this is a staging copy."""
    if not clock.staging():
        return gmail
    log.warning("STAGING: sending only to %d allowed test address(es).", len(allowed()))
    return _Service(gmail)
