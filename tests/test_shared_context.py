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

import pytest

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


def test_indi_is_told_not_to_claim_what_it_wasnt_told():
    assert "never claim that someone hasn't said" in audience.PROMPT


def test_the_consultation_cap_can_be_raised(monkeypatch):
    monkeypatch.delenv("INDI_CONSULT_PER_OWNER_PER_DAY", raising=False)
    assert consult.per_owner_per_day() == consult.MAX_PER_OWNER_PER_DAY
    monkeypatch.setenv("INDI_CONSULT_PER_OWNER_PER_DAY", "20")
    assert consult.per_owner_per_day() == 20


def test_attachments_on_shared_mail_come_with_it(world, tmp_path):
    _, store = world
    folder = tmp_path / "b-shared"
    folder.mkdir()
    (folder / "statements.txt").write_text("Mortgage, long-term 1,886,000")
    (tmp_path / "b-private").mkdir()
    (tmp_path / "b-private" / "secret.txt").write_text("private attachment")
    cfg = SimpleNamespace(attachments_dir=tmp_path)
    text = check_and_reply.shared_context(store, {ANN}, "t-a-own", cfg)
    assert "Mortgage, long-term 1,886,000" in text and "statements.txt" in text
    assert "private attachment" not in text                  # Bob's private message


def test_consultations_may_follow_changed_circumstances_but_not_family_plans():
    assert '"for now"' in consult.SELECT_SYSTEM and "family plans" in consult.SELECT_SYSTEM
    assert "family plans" in consult.DRAFT_SYSTEM and "easy to say no" in consult.DRAFT_SYSTEM


def test_no_second_message_to_someone_who_just_got_a_follow_up(monkeypatch):
    import followups
    import newsletter
    import outbound
    import terms_gate
    from test_heartbeat_per_recipient import CFG, Client, _call
    monkeypatch.setattr(followups, "enabled", lambda: True)
    monkeypatch.setattr(followups, "run_due", lambda *a, **k: (
        followups.SENT_THIS_RUN.add(ANN), ["follow-up f-1 sent"])[1])
    monkeypatch.setattr(outbound, "deliver_due_reminders", lambda *a, **k: [])
    monkeypatch.setattr(newsletter, "due", lambda *a, **k: (False, ""))
    monkeypatch.setattr(outbound, "_context", lambda cfg, st: "x")
    monkeypatch.setattr(terms_gate, "may_send_unsolicited", lambda a, p=None: True)
    monkeypatch.setattr(outbound, "_send", lambda *a: pytest.fail("wrote again"))
    store = SimpleNamespace(outbound_today=lambda: 0, journal_today=lambda: 0,
                            known_correspondents=lambda: {ANN}, spend_since=lambda h: (0.0, 0))
    client = Client([_call(outbound.SEND_TOOL, to=ANN, subject="s", body="b", rationale="r")])
    out = outbound.run_heartbeat(client, CFG, None, store, "sys", {"indi@example.org"})
    assert "a follow-up just went" in out
