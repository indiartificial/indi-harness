"""
tests/test_heartbeat_per_recipient.py

Phase 2, third piece: the heartbeat and the newsletter draw only on what
their recipients have seen, and the reply model is told how each message
was addressed.

The heartbeat is two steps: the first chooses what to do over everyone's
recent mail; if that is a message or a reminder, its draft is thrown away
and a second call writes it from `_context_for(recipient)` alone.

This directory is protected. Indi can read it but not edit it.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import check_and_reply
import newsletter
import outbound
import terms_gate
from test_audience import ANN, BOB, CAROL, INDI, world  # noqa: F401


def _call(name, **inp):
    return SimpleNamespace(type="tool_use", name=name, input=inp)


class Client:
    """Scripted responses, and a record of what each call was shown."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.seen = []
        self.messages = self

    def stream(self, **kw):
        self.seen.append(kw)
        content = self.responses.pop(0)
        final = SimpleNamespace(content=content, usage=None)

        class Ctx:
            def __enter__(s):
                return SimpleNamespace(get_final_message=lambda: final)

            def __exit__(s, *a):
                return False
        return Ctx()


CFG = SimpleNamespace(max_tokens=1000, heartbeat_journal_entries=5,
                      outbound_max_per_day=5, journal_max_per_day=5,
                      outbound_allow_new_recipients=False, dry_run=False,
                      model="m", heartbeat_model="h")


# --------------------------------------------------------------------------

def test_context_for_ann_holds_only_what_ann_has_seen(world):
    _, store = world
    text = outbound._context_for(CFG, store, {ANN})
    assert "budget tight" in text and "thursday maybe" in text
    assert "wednesday impossible" not in text          # Bob's private mail
    assert "wednesday is out" not in text              # Indi's private reply to Bob
    assert "JOURNAL" not in text


def test_context_for_a_group_is_what_all_of_them_have_seen(world):
    _, store = world
    text = outbound._context_for(CFG, store, {ANN, CAROL})
    assert "carol joins" in text and "budget tight" not in text


def test_the_second_step_writes_from_the_recipients_context(world, monkeypatch):
    _, store = world
    client = Client([_call(outbound.SEND_TOOL, to=ANN, subject="hi",
                           body="Thursday then?", rationale="follow up")])
    call = outbound._compose_for(client, CFG, store, "sys", "h", outbound.SEND_TOOL,
                                 ANN, "")
    assert call.input["body"] == "Thursday then?"
    shown = client.seen[0]["messages"][0]["content"]
    assert "wednesday impossible" not in shown
    assert {t["name"] for t in client.seen[0]["tools"]} == {outbound.SEND_TOOL,
                                                            outbound.NOTHING_TOOL}


def test_the_second_step_cannot_change_the_recipient(world):
    _, store = world
    client = Client([_call(outbound.SEND_TOOL, to=BOB, subject="x", body="y",
                           rationale="z")])
    assert outbound._compose_for(client, CFG, store, "sys", "h", outbound.SEND_TOOL,
                                 ANN, "") is None


def test_the_heartbeat_sends_the_second_draft_not_the_first(monkeypatch):
    first = _call(outbound.SEND_TOOL, to=ANN, subject="news",
                  body="Bob can't make Wednesday.", rationale="tell Ann")
    second = _call(outbound.SEND_TOOL, to=ANN, subject="news",
                   body="Shall we fix a day?", rationale="plan")
    client = Client([first], [second])
    sent = []
    store = SimpleNamespace(outbound_today=lambda: 0, journal_today=lambda: 0,
                            known_correspondents=lambda: {ANN},
                            spend_since=lambda h: (0.0, 0))
    monkeypatch.setattr(outbound, "deliver_due_reminders", lambda *a, **k: [])
    monkeypatch.setattr(newsletter, "due", lambda *a, **k: (False, "not today"))
    monkeypatch.setattr(outbound, "_context", lambda cfg, st: "everyone's mail")
    monkeypatch.setattr(outbound, "_context_for", lambda cfg, st, r: "ann's mail")
    monkeypatch.setattr(terms_gate, "may_send_unsolicited", lambda a, p=None: True)
    monkeypatch.setattr(outbound, "_send",
                        lambda client, cfg, gmail, st, payload, *a: sent.append(payload) or "sent")
    outbound.run_heartbeat(client, CFG, None, store, "sys", {INDI})
    assert sent == [second.input]
    assert client.seen[1]["messages"][0]["content"].startswith("ann's mail")


def test_the_heartbeat_can_decide_against_on_reflection(monkeypatch):
    client = Client([_call(outbound.SEND_TOOL, to=ANN, subject="s", body="b",
                           rationale="r")],
                    [_call(outbound.NOTHING_TOOL, reason="nothing she's seen calls for it")])
    store = SimpleNamespace(outbound_today=lambda: 0, journal_today=lambda: 0,
                            known_correspondents=lambda: {ANN},
                            spend_since=lambda h: (0.0, 0))
    monkeypatch.setattr(outbound, "deliver_due_reminders", lambda *a, **k: [])
    monkeypatch.setattr(newsletter, "due", lambda *a, **k: (False, ""))
    monkeypatch.setattr(outbound, "_context", lambda cfg, st: "x")
    monkeypatch.setattr(outbound, "_context_for", lambda cfg, st, r: "y")
    monkeypatch.setattr(terms_gate, "may_send_unsolicited", lambda a, p=None: True)
    monkeypatch.setattr(outbound, "_send", lambda *a: pytest.fail("sent anyway"))
    assert "on reflection" in outbound.run_heartbeat(client, CFG, None, store, "sys", {INDI})


# --------------------------------------------------------------------------
# how a message was addressed
# --------------------------------------------------------------------------

def _raw(to, cc=""):
    headers = [{"name": "From", "value": ANN}, {"name": "To", "value": to}]
    if cc:
        headers.append({"name": "Cc", "value": cc})
    return {"payload": {"headers": headers}}


def test_the_model_is_told_who_else_is_on_the_message():
    text = check_and_reply._addressed(_raw(INDI, CAROL), {INDI})
    assert f"To: {INDI}" in text and f"Cc: {CAROL}" in text and "Bcc" not in text


def test_a_bcc_is_explained():
    text = check_and_reply._addressed(_raw(BOB), {INDI})
    assert "not on To or Cc" in text and "sender alone" in text
