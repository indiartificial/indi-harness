"""
tests/test_calibration.py

Indi's claim record: recording within limits, settling only claims made to
the same people (or by Steve), the daily review settling only with
evidence and never twice a day, expiry, the summary going back into the
prompt as numbers only, and forgetting erasing words but keeping numbers.
"""

import datetime
from types import SimpleNamespace

import pytest

import calibration
import costs

ALICE, BOB = "alice@example.com", "bob@example.com"
NOW = datetime.datetime(2026, 11, 1, 12, tzinfo=datetime.timezone.utc)


@pytest.fixture
def book(tmp_path, monkeypatch):
    path = tmp_path / "calibration.json"
    monkeypatch.setattr(calibration, "FILE", path)
    monkeypatch.setattr(calibration.clock, "now", lambda *a: NOW)
    monkeypatch.setattr(costs, "record", lambda *a, **k: None)
    monkeypatch.delenv("INDI_CALIBRATION", raising=False)
    return path


def _cfg(**over):
    values = dict(dry_run=False, model="claude-opus-5", enable_web_search=False)
    values.update(over)
    return SimpleNamespace(**values)


def _record(text="It will rain in Leeds on 2026-10-20.", confidence=70,
            check_by="2026-10-21", to=(ALICE,), counter=None):
    return calibration.handle(_cfg(), calibration.RECORD_TOOL,
                              {"claim": text, "confidence": confidence, "check_by": check_by},
                              to, operator=False, counter=counter if counter is not None else {})


def test_recording_and_its_limits(book):
    text, err = _record()
    assert not err and "c1" in text
    c = calibration.claims()[0]
    assert c["audience"] == [ALICE] and c["outcome"] == "" and c["confidence"] == 70
    for bad in ({"confidence": 140}, {"check_by": "soon"}, {"text": ""}):
        args = {"text": "x", "confidence": 50, "check_by": "2026-11-02"}
        args.update(bad)
        _, err = _record(**args)
        assert err
    counter = {}
    results = [_record(counter=counter)[1] for _ in range(4)]
    assert results == [False, False, False, True]


def test_settling_in_a_reply_needs_the_same_people(book):
    _record(to=(ALICE,))
    args = {"claim_id": "c1", "outcome": "right", "evidence": "Alice said it rained."}
    _, err = calibration.handle(_cfg(), calibration.RESOLVE_TOOL, args, (BOB,),
                                operator=False, counter={})
    assert err
    _, err = calibration.handle(_cfg(), calibration.RESOLVE_TOOL,
                                {"claim_id": "c1", "outcome": "right", "evidence": ""},
                                (ALICE,), operator=False, counter={})
    assert err
    _, err = calibration.handle(_cfg(), calibration.RESOLVE_TOOL, args, (ALICE, BOB),
                                operator=False, counter={})
    assert not err and calibration.claims()[0]["outcome"] == "right"
    # Once settled, only Steve can change it.
    _, err = calibration.handle(_cfg(), calibration.RESOLVE_TOOL,
                                dict(args, outcome="wrong"), (ALICE,), operator=False,
                                counter={})
    assert err
    _, err = calibration.handle(_cfg(), calibration.RESOLVE_TOOL,
                                dict(args, outcome="wrong"), (), operator=True, counter={})
    assert not err and calibration.claims()[0]["outcome"] == "wrong"


def test_open_claims_shown_to_the_same_audience_only(book):
    _record(to=(ALICE,))
    _record(text="Bob's parcel arrives Friday.", to=(BOB,))
    shown = calibration.render_open(calibration.open_for({ALICE}))
    assert "Leeds" in shown and "parcel" not in shown
    assert calibration.open_for({"carol@example.com"}) == []


def test_listing_is_for_steve(book):
    _record()
    _, err = calibration.handle(_cfg(), calibration.LIST_TOOL, {}, (ALICE,),
                                operator=False, counter={})
    assert err
    text, err = calibration.handle(_cfg(), calibration.LIST_TOOL, {"which": "all"}, (),
                                   operator=True, counter={})
    assert not err and "c1" in text


def test_summary_and_prompt_carry_numbers_not_words(book):
    for i in range(6):
        _record(text=f"secret claim {i}", confidence=90)
        calibration.handle(_cfg(), calibration.RESOLVE_TOOL,
                           {"claim_id": f"c{i + 1}", "outcome": "right" if i < 3 else "wrong",
                            "evidence": "e"}, (ALICE,), operator=False, counter={})
    s = calibration.summary()
    assert s["settled"] == 6
    band = s["bands"][0]
    assert band["band"] == "90-100%" and band["actual"] == 50 and band["stated"] == 90
    prompt = calibration.prompt_section()
    assert "right 50% (3 of 6)" in prompt and "secret" not in prompt


def test_too_few_settled_says_so(book):
    _record()
    assert "Too few claims settled" in calibration.prompt_section()


class ReviewClient:
    def __init__(self, verdicts):
        self.messages = self
        self.calls = []
        self.verdicts = verdicts

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        block = SimpleNamespace(type="tool_use", name=calibration.REVIEW_TOOL,
                                input={"verdicts": self.verdicts})
        response = SimpleNamespace(content=[block], stop_reason="tool_use", usage=None)

        class Ctx:
            def __enter__(self_inner):
                return SimpleNamespace(get_final_message=lambda: response)

            def __exit__(self_inner, *a):
                return False
        return Ctx()


def test_review_settles_only_with_evidence_and_once_a_day(book):
    _record(check_by="2026-10-21")                       # c1 due
    _record(text="Later thing.", check_by="2026-12-01")  # c2 not due
    _record(text="Third.", check_by="2026-10-25")        # c3 due
    client = ReviewClient([
        {"claim_id": "c1", "outcome": "wrong", "evidence": "Met Office: dry all day."},
        {"claim_id": "c3", "outcome": "right", "evidence": ""},
        {"claim_id": "c2", "outcome": "right", "evidence": "sneaky"},
    ])
    out = calibration.review(client, _cfg(), context="mail")
    assert out.startswith("claims reviewed: 1 settled")
    by_id = {c["id"]: c for c in calibration.claims()}
    assert by_id["c1"]["outcome"] == "wrong" and by_id["c1"]["settled_by"] == "review"
    assert by_id["c3"]["outcome"] == "" and by_id["c2"]["outcome"] == ""
    listing = client.calls[0]["messages"][0]["content"]
    assert "Leeds" in listing and "Later thing" not in listing
    assert calibration.review(client, _cfg()) == "claims already reviewed today"
    assert len(client.calls) == 1


def test_review_offers_search_when_on(book):
    _record(check_by="2026-10-21")
    client = ReviewClient([])
    calibration.review(client, _cfg(enable_web_search=True))
    assert "web_search" in {t["name"] for t in client.calls[0]["tools"]}


def test_old_unsettled_claims_expire(book):
    _record(check_by="2026-08-01")
    out = calibration.review(ReviewClient([]), _cfg())
    assert "expired" in out
    assert calibration.claims()[0]["outcome"] == "expired"
    assert calibration.summary()["expired"] == 1


def test_forgetting_erases_words_keeps_numbers(book):
    _record(to=(ALICE,))
    _record(text="Group claim.", to=(ALICE, BOB))
    assert calibration.forget(ALICE) == 1
    rows = calibration.claims()
    assert rows[0]["text"] == calibration.REMOVED_TEXT and rows[0]["audience"] == []
    assert rows[0]["outcome"] == "expired" and rows[0]["confidence"] == 70
    assert rows[1]["text"] == "Group claim."


def test_switched_off(book, monkeypatch):
    monkeypatch.setenv("INDI_CALIBRATION", "false")
    assert calibration.schemas(operator=True) == []
    assert calibration.prompt_section() == ""


def test_calibration_is_protected():
    import self_code
    assert "calibration.py" in self_code.PROTECTED_FILES


def test_reply_tools_and_open_claims_block(book):
    import claude_api
    _record(to=(ALICE,))
    cfg = SimpleNamespace(enable_web_search=False, enable_web_fetch=False,
                          enable_code_execution=False, enable_history_search=False,
                          enable_outbound=False, betas=lambda **k: [], model="m",
                          max_tokens=100, max_tool_loop_iterations=1)
    names = {t["name"] for t in claude_api.build_tools(cfg, False, False)[0]}
    assert {calibration.RECORD_TOOL, calibration.RESOLVE_TOOL} <= names
    assert calibration.LIST_TOOL not in names
    seen = []

    class C:
        def __init__(self):
            self.messages = self

        def stream(self, **kw):
            seen.extend(kw["messages"][0]["content"])
            response = SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")],
                                       stop_reason="end_turn", usage=None)

            class Ctx:
                def __enter__(self_inner):
                    return SimpleNamespace(get_final_message=lambda: response)

                def __exit__(self_inner, *a):
                    return False
            return Ctx()
    msg = {"from_addr": ALICE, "reply_to_addr": "", "subject": "s", "body": "hi",
           "from_raw": ALICE, "gmail_msg_id": "m", "thread_id": "t", "attachment_refs": []}
    claude_api.generate_reply(C(), cfg, "SYS", msg)
    assert any("Leeds" in b.get("text", "") for b in seen if isinstance(b, dict))
