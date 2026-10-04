"""
tests/test_clock.py

The virtual clock (clock.py): ignored in production, followed in a staging
copy — by the date Indi is told, follow-ups and the other time-based rules
— while money stays on real time.

This directory is protected. Indi can read it but not edit it.
"""

import datetime
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import check_and_reply
import clock
import costs
import followups

JAN7 = datetime.datetime(2027, 1, 7, 9, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=-5)))


@pytest.fixture
def clockfile(tmp_path, monkeypatch):
    path = tmp_path / "virtual_clock.json"
    monkeypatch.setattr(clock, "FILE", path)
    return path


def test_production_ignores_the_clock_file(clockfile, monkeypatch):
    monkeypatch.setenv("INDI_STAGING", "false")
    clock.set_virtual(JAN7)
    assert abs((clock.now() - datetime.datetime.now(datetime.timezone.utc)).total_seconds()) < 5
    assert not clock.virtual()


def test_staging_follows_it_and_the_clock_keeps_running(clockfile, monkeypatch):
    monkeypatch.setenv("INDI_STAGING", "true")
    clock.set_virtual(JAN7)
    assert clock.virtual()
    first = clock.now()
    assert abs((first - JAN7).total_seconds()) < 5
    assert clock.now() >= first


def test_an_unreadable_file_means_real_time(clockfile, monkeypatch):
    monkeypatch.setenv("INDI_STAGING", "true")
    clockfile.write_text("not json")
    assert not clock.virtual()


def test_indi_is_told_the_virtual_date(clockfile, monkeypatch, tmp_path):
    monkeypatch.setenv("INDI_STAGING", "true")
    clock.set_virtual(JAN7)
    prompt_file = tmp_path / "system_prompt.txt"
    prompt_file.write_text("You are Indi.")
    cfg = SimpleNamespace(system_prompt_path=prompt_file, send_automatically=True,
                          enable_outbound=True)
    text = check_and_reply.load_system_prompt(cfg, "indi@example.org")
    assert "2027" in text and "January" in text


def test_follow_ups_live_in_virtual_time(clockfile, monkeypatch):
    from test_drive_tables import _records_store
    monkeypatch.setenv("INDI_STAGING", "true")
    clock.set_virtual(JAN7)
    store, _ = _records_store()
    msg = {"thread_id": "t", "gmail_msg_id": "m", "from_addr": "ann@example.com"}
    # Friday 15 January 2027: eight virtual days ahead, months ahead in reality.
    text, err = followups.schedule(store, msg, {"ann@example.com"},
                                   {"due_at": "2027-01-15T09:00:00-05:00", "note": "LOI?"})
    assert not err, text


def test_money_stays_on_real_time(clockfile, monkeypatch):
    monkeypatch.setenv("INDI_STAGING", "true")
    clock.set_virtual(JAN7)
    stamp = datetime.datetime.fromisoformat(costs._now_iso())
    assert abs((stamp - datetime.datetime.now(datetime.timezone.utc)).total_seconds()) < 5
