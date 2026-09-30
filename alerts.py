#!/usr/bin/env python3
"""
alerts.py

Push alerts to a phone when something fails, and check-ins to a
dead-man's-switch service so that runs *stopping* is noticed too.

THREE KINDS OF FAILURE, THREE WAYS OF HEARING ABOUT IT
------------------------------------------------------
1. A run finishes but logged an ERROR (a message that failed, the API
   refusing a call, the pre-send check giving up): the harness counts them
   while it runs and sends one summary at the end (`RunMonitor`).
2. A run crashes, or is killed, before it can say anything: systemd runs
   `alerts.py unit-failed <unit>` (deploy/indi-alert@.service, hooked in
   with OnFailure=). If the run already sent its own alert in the last few
   minutes, this one stays quiet.
3. Runs stop happening at all — a stopped timer, a dead server: nothing
   fails, so nothing is sent. For that each mail run pings a check URL
   (healthchecks.io or similar), and that service alerts when the pings
   stop.

WHERE ALERTS GO
---------------
INDI_ALERT_NTFY_URL, e.g. https://ntfy.sh/indi-<long random words>. The
public ntfy server has no passwords on topics: the topic name is the only
secret, so it must be long and unguessable — and alerts never carry mail
content, only what failed (logger names, exception types, counts, unit
names). INDI_HEALTHCHECK_URL (and INDI_HEALTHCHECK_HEARTBEAT_URL) are the
check-in URLs. Unset means off.

NOT A FLOOD
-----------
A problem that persists alerts at most once an hour per kind (mail run,
heartbeat, each unit), and the first clean run afterwards sends one
"back to normal". State lives in data/alert_state.json, machine-local.

Standard library only. Never raises: an alert that can't be sent is logged
and dropped, and must not become a failure of its own.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

log = logging.getLogger("indi.alerts")

REPEAT_SECONDS = 3600
STATE_FILE = Path(__file__).resolve().parent / "data" / "alert_state.json"
TIMEOUT = 10


def _setting(name: str) -> str:
    try:
        import gmail_reads
        return gmail_reads._setting(name)
    except Exception:
        return (os.environ.get(name) or "").strip()


# ---------------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------------

def _post(url: str, body: str, headers: dict) -> bool:
    try:
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers,
                                     method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        log.warning("Alert not sent: %s", exc.__class__.__name__)
        return False


def send(title: str, message: str, priority: int = 4, tags: str = "warning") -> bool:
    """One push notification. Title is kept ASCII (it travels in a header)."""
    url = _setting("INDI_ALERT_NTFY_URL")
    if not url:
        return False
    safe_title = title.encode("ascii", "replace").decode("ascii")[:120]
    return _post(url, message[:1500], {"Title": safe_title, "Priority": str(priority),
                                       "Tags": tags})


def ping(setting: str = "INDI_HEALTHCHECK_URL") -> None:
    """Check in with the dead-man's switch, if one is configured."""
    url = _setting(setting)
    if not url:
        return
    try:
        urllib.request.urlopen(url, timeout=TIMEOUT).close()
    except Exception as exc:
        log.warning("Health check ping failed: %s", exc.__class__.__name__)


# ---------------------------------------------------------------------------
# once an hour, and an all-clear
# ---------------------------------------------------------------------------

def _load(path: Path | None = None) -> dict:
    path = path or STATE_FILE
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save(state: dict, path: Path | None = None) -> None:
    path = path or STATE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(path)
    except Exception:
        log.warning("Could not save alert state.", exc_info=True)


def problem(key: str, title: str, message: str, priority: int = 4,
            path: Path | None = None, now: float | None = None) -> bool:
    """Alert about `key`, unless it was alerted in the last hour."""
    now = now or time.time()
    state = _load(path)
    failing = state.setdefault("failing", {})
    last = failing.get(key, 0)
    failing[key] = last                  # failing from now on, alerted or not
    sent = False
    if now - last >= REPEAT_SECONDS:
        sent = send(title, message, priority)
        if sent:
            failing[key] = now
            state["last_sent"] = now
    state.setdefault("episode", {})[key.split(":", 1)[0]] = now
    _save(state, path)
    return sent


def recovered(prefix: str, title: str, path: Path | None = None) -> bool:
    """A clean run: if anything starting with `prefix` was failing, say
    it's back to normal, once, and forget it."""
    state = _load(path)
    failing = state.get("failing", {})
    keys = [k for k in failing if k.startswith(prefix)]
    if not keys:
        return False
    for k in keys:
        del failing[k]
    _save(state, path)
    return send(title, "Runs are completing without errors again.", priority=2,
                tags="white_check_mark")


def in_episode(mode: str, path: Path | None = None) -> bool:
    """Has a run of this kind reported a problem in the last hour? Then a
    unit failure is the same problem, already reported."""
    return time.time() - _load(path).get("episode", {}).get(mode, 0) < REPEAT_SECONDS


def mode_of(unit: str) -> str:
    if "heartbeat" in unit:
        return "heartbeat"
    if "logview" in unit:
        return "logview"
    return "mail"


# ---------------------------------------------------------------------------
# watching a run
# ---------------------------------------------------------------------------

class RunMonitor(logging.Handler):
    """Counts ERROR records while a run is going, by what failed — the
    logger's name and, where there is one, the exception's type. Never the
    message text: that can quote someone's email."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.kinds: Counter = Counter()

    def emit(self, record: logging.LogRecord) -> None:
        kind = record.name
        if record.exc_info and record.exc_info[0] is not None:
            kind += f" ({record.exc_info[0].__name__})"
        self.kinds[kind] += 1

    def summary(self) -> str:
        return ", ".join(f"{k} x{n}" if n > 1 else k for k, n in self.kinds.most_common(8))


def after_run(mode: str, monitor: RunMonitor, failures: int = 0,
              crashed: BaseException | None = None) -> None:
    """At the end of a run: alert, or all-clear. `mode` is "mail" or
    "heartbeat"."""
    label = "mail check" if mode == "mail" else "heartbeat"
    if crashed is not None:
        problem(f"{mode}:crash", f"Indi {label} crashed",
                f"{crashed.__class__.__name__}. It will try again on the next run. "
                f"Errors logged: {monitor.summary() or 'none'}. See the log viewer.",
                priority=5)
        return
    count = sum(monitor.kinds.values())
    if count or failures:
        parts = []
        if failures:
            parts.append(f"{failures} message(s) failed")
        if count:
            parts.append(f"{count} error(s): {monitor.summary()}")
        problem(f"{mode}:errors", f"Indi {label}: problems",
                "; ".join(parts) + ". See the log viewer.")
        return
    recovered(f"{mode}:", f"Indi {label}: back to normal")


# ---------------------------------------------------------------------------
# command line: systemd's OnFailure hook, and a test
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["test"]:
        ok = send("Indi: test alert", "If you can read this, alerts reach your phone.",
                  priority=3, tags="bell")
        print("sent" if ok else "not sent (is INDI_ALERT_NTFY_URL set?)")
        return 0 if ok else 1
    if argv[:1] == ["unit-failed"] and len(argv) > 1:
        unit = argv[1]
        mode = mode_of(unit)
        if in_episode(mode):
            return 0                         # the run already said what went wrong
        problem(f"{mode}:unit", f"Indi: {unit} failed",
                f"systemd reports {unit} exited with a failure. Check the log viewer, "
                f"or: systemctl status {unit}", priority=5)
        return 0
    print("usage: alerts.py test | unit-failed <unit>")
    return 2


if __name__ == "__main__":
    sys.exit(main())
