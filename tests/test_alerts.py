"""
tests/test_alerts.py

Push alerts (alerts.py): what they say (never mail content), how often
(once an hour per kind, then one all-clear), and the systemd hook staying
quiet when the run already reported.

This directory is protected. Indi can read it but not edit it.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import alerts


@pytest.fixture
def sent(monkeypatch, tmp_path):
    out = []
    monkeypatch.setattr(alerts, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(alerts, "_setting",
                        lambda name: "https://ntfy.example/topic" if name == "INDI_ALERT_NTFY_URL" else "")
    monkeypatch.setattr(alerts, "_post",
                        lambda url, body, headers: out.append((headers["Title"], body)) or True)
    return out


def _monitor_with(*records):
    m = alerts.RunMonitor()
    log = logging.getLogger("indi.test")
    log.addHandler(m)
    try:
        for msg, exc in records:
            try:
                if exc:
                    raise exc
                log.error(msg)
            except Exception:
                log.exception(msg)
    finally:
        log.removeHandler(m)
    return m


def test_the_summary_names_what_failed_never_the_message(sent):
    m = _monitor_with(("Failed on message from ann@example.com: 'my password is hunter2'",
                       ValueError("x")), ("reply to bob@example.com failed", None))
    alerts.after_run("mail", m, failures=1)
    [(title, body)] = sent
    assert "problems" in title and "ValueError" in body and "1 message(s) failed" in body
    assert "hunter2" not in body and "@example.com" not in body


def test_a_persisting_problem_alerts_once_an_hour(sent):
    m = _monitor_with(("boom", None))
    t0 = 1_000_000.0
    for dt in (0, 300, 3000):
        alerts.problem("mail:errors", "t", "m", path=alerts.STATE_FILE, now=t0 + dt)
    assert len(sent) == 1
    alerts.problem("mail:errors", "t", "m", path=alerts.STATE_FILE, now=t0 + 3700)
    assert len(sent) == 2


def test_a_clean_run_sends_one_all_clear(sent):
    alerts.after_run("mail", _monitor_with(("boom", None)))
    alerts.after_run("mail", alerts.RunMonitor())
    alerts.after_run("mail", alerts.RunMonitor())
    assert [t for t, _ in sent] == ["Indi mail check: problems", "Indi mail check: back to normal"]


def test_a_crash_is_urgent(sent):
    alerts.after_run("heartbeat", alerts.RunMonitor(), crashed=RuntimeError("secret text"))
    [(title, body)] = sent
    assert "crashed" in title and "RuntimeError" in body and "secret text" not in body


def test_the_systemd_hook_stays_quiet_when_the_run_already_reported(sent):
    alerts.after_run("mail", alerts.RunMonitor(), failures=2)
    alerts.main(["unit-failed", "indi.service"])
    assert len(sent) == 1
    alerts.main(["unit-failed", "indi-heartbeat.service"])     # a different kind: reported
    assert len(sent) == 2 and "indi-heartbeat.service" in sent[-1][0]


def test_no_url_no_alert(monkeypatch, tmp_path):
    monkeypatch.setattr(alerts, "STATE_FILE", tmp_path / "s.json")
    monkeypatch.setattr(alerts, "_setting", lambda name: "")
    assert alerts.send("t", "m") is False


def test_titles_are_kept_ascii(sent):
    alerts.send("Indi — ünïcode", "m")
    assert sent[0][0].isascii()
