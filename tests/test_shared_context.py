"""
tests/test_shared_context.py

The writer is shown recent mail, on other threads, that everyone on the
reply has already seen (check_and_reply.shared_context) — and nothing
they haven't. Plus: a model that refuses a forced tool choice is asked
directly from then on.

This directory is protected. Indi can read it but not edit it.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audience
import check_and_reply
import consult
import ledger
from test_audience import ANN, BOB, CAROL, world  # noqa: F401


def test_shared_mail_is_shown_private_mail_is_not(world):
    _, store = world
    text = check_and_reply.shared_context(store, {ANN}, "t-a-own")
    assert "budget tight" in text and "carol joins" in text      # Ann was copied
    assert "wednesday impossible" not in text                    # Bob's private message
    assert "wednesday is out" not in text                        # Indi's private reply to Bob


def test_the_current_thread_is_left_to_the_thread_context(world):
    _, store = world
    assert "budget tight" not in check_and_reply.shared_context(store, {ANN}, "t-shared")


def test_a_group_sees_only_what_all_of_them_saw(world):
    _, store = world
    text = check_and_reply.shared_context(store, {ANN, CAROL}, "t-other")
    assert "carol joins" in text and "budget tight" not in text


def test_indi_is_told_shared_mail_is_fair_game():
    assert "Ann may hear it from you" in audience.PROMPT


def test_the_keeper_is_told_to_be_sparing():
    assert "standing behaviour" in ledger.KEEPER_SYSTEM and "record nothing" in ledger.KEEPER_SYSTEM


def test_a_refusing_model_is_asked_directly_after_the_first_time(monkeypatch):
    consult._REFUSES_FORCED.discard("m-refuses")
    calls = []

    def create(**kw):
        calls.append(kw["tool_choice"]["type"])
        if kw["tool_choice"]["type"] == "tool":
            raise RuntimeError("tool_choice: type tool not supported for this model")
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="t",
                                                        input={"ok": 1})], usage=None)
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    tool = {"name": "t", "input_schema": {"type": "object"}}
    cfg = SimpleNamespace()
    for _ in range(3):
        assert consult._tool_call(client, cfg, "s", "c", tool, "check", model="m-refuses") == {"ok": 1}
    assert calls == ["tool", "auto", "auto", "auto"]
