"""
tests/test_costs.py

Properties the spend ledger must hold. The point of cost tracking is that
the number is either right or visibly absent — a quietly wrong figure is
worse than none, because it gets believed.

This directory is protected. Indi can read it but not edit it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import costs


class _Usage:
    """Stands in for the SDK's usage object."""

    def __init__(self, **kw):
        for key, value in kw.items():
            setattr(self, key, value)


@pytest.fixture(autouse=True)
def clean_tally():
    costs.reset()
    yield
    costs.reset()


# --------------------------------------------------------------------------
# PRICING
# --------------------------------------------------------------------------

def test_a_known_model_prices_every_token_class():
    # 1M of each, so the answer is just the rate card.
    tokens = {"input": 1_000_000, "output": 1_000_000,
              "cache_read": 1_000_000, "cache_write": 1_000_000}
    usd = costs.price("claude-opus-5", tokens)
    assert usd == pytest.approx(5.00 + 25.00 + 0.50 + 6.25)


def test_an_unknown_model_prices_as_none_not_zero():
    # The failure this guards against: a model change silently reporting
    # a free run instead of an unpriced one.
    assert costs.price("claude-something-unreleased", {"input": 1_000_000}) is None


def test_web_searches_are_billed_per_call_on_top_of_tokens():
    tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    assert costs.price("claude-opus-5", tokens, web_searches=3) == pytest.approx(0.03)


def test_cache_reads_are_cheaper_than_fresh_input():
    tokens_fresh = {"input": 100_000}
    tokens_cached = {"cache_read": 100_000}
    assert costs.price("claude-opus-5", tokens_cached) < costs.price(
        "claude-opus-5", tokens_fresh
    )


def test_every_priced_model_has_every_rate():
    for model, rates in costs.PRICING.items():
        missing = set(costs.TOKEN_FIELDS) - set(rates)
        assert not missing, f"{model} is missing rates for {missing}"


# --------------------------------------------------------------------------
# READING USAGE OFF A RESPONSE
# --------------------------------------------------------------------------

def test_usage_fields_are_read_by_their_api_names():
    usage = _Usage(input_tokens=10, output_tokens=20,
                   cache_read_input_tokens=30, cache_creation_input_tokens=40)
    assert costs.tokens_from_usage(usage) == {
        "input": 10, "output": 20, "cache_read": 30, "cache_write": 40,
    }


def test_a_response_without_usage_costs_nothing_rather_than_raising():
    # A missing usage block must not take down a reply that was fine.
    assert costs.tokens_from_usage(None) == {f: 0 for f in costs.TOKEN_FIELDS}


def test_partial_usage_does_not_raise():
    assert costs.tokens_from_usage(_Usage(input_tokens=5))["input"] == 5


def test_add_usage_accumulates_across_loop_iterations():
    # Each tool-loop iteration is a separately billed request; the cost of
    # a message is all of them, not just the last.
    running = {f: 0 for f in costs.TOKEN_FIELDS}
    costs.add_usage(running, _Usage(input_tokens=100, output_tokens=10))
    costs.add_usage(running, _Usage(input_tokens=200, output_tokens=20))
    assert running["input"] == 300
    assert running["output"] == 30


# --------------------------------------------------------------------------
# THE RUN TALLY
# --------------------------------------------------------------------------

def test_recorded_calls_sum_into_the_run_total():
    costs.record("reply", "claude-opus-5", {"output": 1_000_000})
    costs.record("heartbeat", "claude-opus-5", {"output": 1_000_000})
    assert costs.run_total() == pytest.approx(50.00)
    assert len(costs.run_rows()) == 2


def test_unpriced_calls_are_recorded_and_counted_separately():
    costs.record("reply", "claude-mystery", {"output": 1_000_000})
    assert costs.run_unpriced() == 1
    assert costs.run_total() == 0.0          # contributes no dollars
    assert len(costs.run_rows()) == 1        # but is not lost
    assert costs.run_rows()[0]["usd"] == ""  # blank, not 0


def test_the_summary_flags_unpriced_calls_rather_than_hiding_them():
    costs.record("reply", "claude-mystery", {"output": 1_000})
    assert "unpriced" in costs.run_summary()


def test_every_row_carries_the_ledger_columns():
    import sheets_queue

    costs.record("reply", "claude-opus-5", {"input": 1}, note="someone@example.com")
    row = costs.run_rows()[0]
    for column in sheets_queue.COSTS_HEADERS:
        assert column in row, f"ledger column {column!r} missing from recorded row"
