"""
costs.py

What each Claude call costs, priced as it happens.

The API does not return a dollar figure — it returns token counts. This
module turns those into an estimate and keeps a per-run tally that
`check_and_reply.py` writes out at the end of the run.

Three things worth knowing about the numbers this produces:

  * **They are estimates, not the invoice.** Prices below are the
    published Anthropic first-party rates, hardcoded. If a rate changes,
    or the account is on partner pricing (Bedrock/Vertex), these drift
    until `PRICING` is updated. The authoritative figure is the Usage and
    Cost Admin API, which needs an admin key this harness doesn't have.

  * **An unknown model prices as `None`, not as zero.** Silently
    reporting $0.00 after a model change would be worse than reporting
    nothing, so an unpriced model is recorded with a blank cost and a
    warning in the log.

  * **Server-side tools bill separately from tokens.** Web search is
    charged per call, not per token, so it's counted and priced on its
    own. Its tokens are already inside the usage figures.

Single process per run — the run lock guarantees it — so the tally is
module state rather than an object threaded through every call site.
"""

from __future__ import annotations

import datetime
import logging

log = logging.getLogger("indi.costs")

# US dollars per million tokens, Anthropic first-party rates.
#
# cache_write is the 5-minute TTL rate (1.25x input); this harness never
# asks for the 1-hour TTL, which is priced at 2x. cache_read is 0.1x
# input except where a model sets its own rate.
PRICING: dict[str, dict[str, float]] = {
    # Opus 5.5 (22 Sep 2026): 20% under Opus 5; cache reads at 5% of input.
    "claude-opus-5-5":   {"input":  4.00, "output": 20.00, "cache_write":  5.00, "cache_read": 0.20},
    "claude-opus-5":     {"input":  5.00, "output": 25.00, "cache_write":  6.25, "cache_read": 0.50},
    "claude-opus-4-8":   {"input":  5.00, "output": 25.00, "cache_write":  6.25, "cache_read": 0.50},
    "claude-opus-4-7":   {"input":  5.00, "output": 25.00, "cache_write":  6.25, "cache_read": 0.50},
    "claude-sonnet-5":   {"input":  2.00, "output": 10.00, "cache_write":  2.50, "cache_read": 0.20},
    "claude-haiku-4-5":  {"input":  1.00, "output":  5.00, "cache_write":  1.25, "cache_read": 0.10},
    "claude-fable-5-1":  {"input": 10.00, "output": 50.00, "cache_write": 12.50, "cache_read": 0.25},
}

# Web search is billed per call. Tokens it returns are already counted in
# the usage totals; this is the search fee on top.
WEB_SEARCH_USD_PER_CALL = 0.01

TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write")

_unpriced_models: set[str] = set()
_run: list[dict] = []


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def tokens_from_usage(usage) -> dict[str, int]:
    """Pull the four billable token counts off a response's `usage`.

    Defensive by design: a response without usage, or an SDK that grows a
    field, must not break a reply that was otherwise fine.
    """
    if usage is None:
        return {f: 0 for f in TOKEN_FIELDS}
    return {
        "input": getattr(usage, "input_tokens", 0) or 0,
        "output": getattr(usage, "output_tokens", 0) or 0,
        "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


def add_usage(into: dict[str, int], usage) -> dict[str, int]:
    """Accumulate one response's usage into a running token dict."""
    for field, value in tokens_from_usage(usage).items():
        into[field] = into.get(field, 0) + value
    return into


def price(model: str, tokens: dict[str, int], web_searches: int = 0) -> float | None:
    """Dollars for these tokens on this model, or None if unpriced.

    Returning None rather than 0.0 for an unknown model is deliberate: a
    zero would look like a free run instead of a missing price.
    """
    rates = PRICING.get(model)
    if rates is None:
        if model not in _unpriced_models:
            _unpriced_models.add(model)
            log.warning(
                "No price on file for model %r — usage is being recorded "
                "but not costed. Add it to costs.PRICING.", model,
            )
        return None
    total = sum(
        (tokens.get(field, 0) / 1_000_000.0) * rates[field] for field in TOKEN_FIELDS
    )
    return total + web_searches * WEB_SEARCH_USD_PER_CALL


def record(kind: str, model: str, tokens: dict[str, int],
           web_searches: int = 0, note: str = "") -> float | None:
    """Add one call (or one message's worth of calls) to the run tally."""
    usd = price(model, tokens, web_searches)
    _run.append({
        "at": _now_iso(),
        "kind": kind,
        "model": model,
        "input_tokens": tokens.get("input", 0),
        "output_tokens": tokens.get("output", 0),
        "cache_read_tokens": tokens.get("cache_read", 0),
        "cache_write_tokens": tokens.get("cache_write", 0),
        "web_searches": web_searches,
        "usd": "" if usd is None else round(usd, 6),
        "note": note[:200],
    })
    return usd


def record_usd(kind: str, model: str, usd: float, note: str = "") -> float:
    """Record a cost that arrives already priced in dollars.

    `record` above turns token counts into an estimate. Some things bill
    us a figure directly instead — a Claude Code session reports its own
    `total_cost_usd` and never tells us its tokens — and those belong in
    the same ledger. The token columns are zero for these rows, which is
    honest: we genuinely don't know them, and writing a guess in would
    corrupt the per-token totals that `run_summary` reports.
    """
    _run.append({
        "at": _now_iso(),
        "kind": kind,
        "model": model,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "web_searches": 0,
        "usd": round(float(usd), 6),
        "note": note[:200],
    })
    return float(usd)


def run_rows() -> list[dict]:
    return list(_run)


def run_total() -> float:
    """Dollars for this run. Unpriced rows contribute nothing — read it
    alongside `run_unpriced()` before trusting it."""
    return sum(row["usd"] for row in _run if row["usd"] != "")


def run_unpriced() -> int:
    return sum(1 for row in _run if row["usd"] == "")


def run_summary() -> str:
    """One log line for the end of a run."""
    if not _run:
        return "no Claude calls"
    tokens = {f: 0 for f in TOKEN_FIELDS}
    for row in _run:
        tokens["input"] += row["input_tokens"]
        tokens["output"] += row["output_tokens"]
        tokens["cache_read"] += row["cache_read_tokens"]
        tokens["cache_write"] += row["cache_write_tokens"]
    searches = sum(row["web_searches"] for row in _run)
    parts = [
        f"{len(_run)} call(s)",
        f"in {tokens['input']}",
        f"out {tokens['output']}",
        f"cache {tokens['cache_read']}r/{tokens['cache_write']}w",
    ]
    if searches:
        parts.append(f"{searches} web search(es)")
    unpriced = run_unpriced()
    if unpriced:
        parts.append(f"${run_total():.4f} (+{unpriced} unpriced)")
    else:
        parts.append(f"${run_total():.4f}")
    return ", ".join(parts)


def reset() -> None:
    _run.clear()
