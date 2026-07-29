"""The skip message must name the constraint that actually bound.

Measured on the live book 2026-07-27, the sizing step logged

    SizeAndEmitTask: TSLA insufficient cash — skip
                     (remaining_cash=$9301 price=$309.22)

with nine thousand dollars available. Cash was never the binding constraint:
the per-name target (~$231 at that conviction) sat below one share, and
integer sizing floored it to zero. Anyone debugging why half the book was
idle got pointed at the wrong quantity — worse than saying nothing, because
it looks like a funded answer.

Round-2 fix (codex review): a first revision keyed the message off raw
`remaining_cash`, but `compute_position_size`'s own skip condition is
`investable < price` where `investable = max(remaining_cash - portfolio_value
* cash_reserve_pct, 0)`. A large reserve can leave ample raw cash but far
less investable, in which case cash genuinely IS the constraint even though
`remaining_cash >= price` — the exact diagnostic ambiguity this PR exists to
remove, now reproduced with a reserve instead of a small target.
"""
from __future__ import annotations

import datetime as dt

import pytest

from renquant_pipeline.context import InferenceContext
from renquant_pipeline.kernel.selection import CandidateResult
from renquant_pipeline.kernel.pipeline.task_selection import SizeAndEmitTask
from renquant_pipeline.kernel.sizing import sizing_target_notional

PRICE = 309.22


def test_the_two_cases_are_distinguishable_from_the_inputs():
    """Pins the arithmetic the message now reports.

    Target-bound: plenty of INVESTABLE cash, but the position target is
    under one share (the live TSLA case, no reserve).
    Reserve-limited: raw cash exceeds one share, but the reservation leaves
    investable cash below it — cash genuinely IS the constraint.
    """
    target, investable = sizing_target_notional(
        portfolio_value=10_500.0, available_cash=9_301.0,
        max_position_pct=0.022, cash_reserve_pct=0.0, override_pct=None,
    )
    assert investable >= PRICE, "investable cash is NOT short in this case"
    assert target < PRICE, "the target is what binds"
    assert int(target // PRICE) == 0

    target2, investable2 = sizing_target_notional(
        portfolio_value=10_000.0, available_cash=600.0,
        max_position_pct=0.5, cash_reserve_pct=0.05, override_pct=None,
    )
    assert 600.0 >= PRICE, "raw cash is NOT short in this case"
    assert investable2 < PRICE, "investable cash IS short — the reserve did it"


def _cand(ticker, panel_score=0.001, *, expected_return=0.04, mu=0.04, sigma=0.2):
    return CandidateResult(
        ticker=ticker, raw_score=panel_score, rank_score=0.6, rs_score=0.0,
        detail="", expected_return=expected_return,
        expected_return_horizon_days=60,
        panel_score=panel_score, mu=mu, mu_horizon_days=60, sigma=sigma,
    )


def _config(*, max_position_pct, cash_reserve_pct, conv_min_mult=1.0):
    return {
        "regime_params": {"BULL_CALM": {
            "max_position_pct": max_position_pct,
            "cash_reserve_pct": cash_reserve_pct,
            "max_concurrent_positions": 8,
        }},
        "ranking": {"panel_scoring": {
            "enabled": True,
            "sizing": {"enabled": True, "floor": 0.5, "ceiling": 1.0,
                       "min_mult": conv_min_mult},
            "sigma_sizing": {},
        }, "kelly_sizing": {"enabled": False}},
        "regime": {},
    }


def _run(config, *, ticker="ZZZ", price, cash, pv):
    ranked = [_cand(ticker)]
    ctx = InferenceContext(
        config=config, today=dt.date(2026, 7, 29), regime="BULL_CALM",
        confidence=1.0, bear_only=False, portfolio_value=pv, cash=cash,
        prices={ticker: price}, ranked=ranked, models={},
    )
    ctx._selected = [ticker]  # noqa: SLF001
    SizeAndEmitTask().run(ctx)
    return ctx


def test_target_bound_skip_says_position_target_not_cash(caplog):
    """Ample investable cash, tiny target: must NOT blame cash."""
    with caplog.at_level("INFO", logger="kernel.pipeline.selection"):
        ctx = _run(_config(max_position_pct=0.022, cash_reserve_pct=0.0),
                   price=PRICE, cash=9_301.0, pv=10_500.0)
    assert ctx.orders == []
    assert ctx._blocked_by_ticker["ZZZ"] == "size_insufficient_cash"  # noqa: SLF001
    assert "ZZZ" in caplog.text
    assert "position target" in caplog.text
    assert "sufficient" in caplog.text  # investable cash was sufficient
    assert "investable cash $9301" in caplog.text
    # must not lead with a bare cash-shortage claim
    assert "investable cash $9301 < one share" not in caplog.text


def test_reserve_limited_skip_says_cash_not_target(caplog):
    """Raw cash exceeds one share, but the reserve leaves investable cash
    below it — cash genuinely IS the constraint, and must be named as such,
    not routed into the 'position target' branch just because raw cash is
    ample."""
    with caplog.at_level("INFO", logger="kernel.pipeline.selection"):
        ctx = _run(_config(max_position_pct=0.5, cash_reserve_pct=0.05),
                   price=PRICE, cash=600.0, pv=10_000.0)
    assert ctx.orders == []
    assert ctx._blocked_by_ticker["ZZZ"] == "size_insufficient_cash"  # noqa: SLF001
    assert "ZZZ" in caplog.text
    assert "investable cash $100 < one share" in caplog.text
    assert "raw cash $600" in caplog.text
    assert "position target" not in caplog.text


def test_the_block_reason_string_is_unchanged():
    """Changing it is a behaviour change for ledger consumers and for
    tests/test_fractional_sizing_stage2.py; splitting it needs its own audit."""
    ctx = _run(_config(max_position_pct=0.022, cash_reserve_pct=0.0),
              price=PRICE, cash=9_301.0, pv=10_500.0)
    assert ctx._blocked_by_ticker["ZZZ"] == "size_insufficient_cash"  # noqa: SLF001
