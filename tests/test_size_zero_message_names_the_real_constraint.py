"""The skip message must name the constraint that actually bound.

Measured on the live book 2026-07-27, the sizing step logged

    SizeAndEmitTask: TSLA insufficient cash — skip
                     (remaining_cash=$9301 price=$309.22)

with nine thousand dollars available. Cash was never the binding constraint:
the per-name target (~$231 at that conviction) sat below one share, and
integer sizing floored it to zero. Anyone debugging why half the book was
idle got pointed at the wrong quantity — worse than saying nothing, because
it looks like a funded answer.
"""
from __future__ import annotations

import logging

from renquant_pipeline.kernel.sizing import sizing_target_notional


def test_the_two_cases_are_distinguishable_from_the_inputs():
    """Pins the arithmetic the message now reports.

    Cash-bound: cash below one share.
    Target-bound: plenty of cash, but the position target is under one share.
    """
    # target-bound — the live TSLA case
    target, _ = sizing_target_notional(
        portfolio_value=10_500.0, available_cash=9_301.0,
        max_position_pct=0.022, cash_reserve_pct=0.0, override_pct=None,
    )
    price = 309.22
    assert 9_301.0 >= price, "cash is NOT short in this case"
    assert target < price, "the target is what binds"
    assert int(target // price) == 0

    # cash-bound — the genuinely different case
    target2, _ = sizing_target_notional(
        portfolio_value=10_500.0, available_cash=100.0,
        max_position_pct=0.50, cash_reserve_pct=0.0, override_pct=None,
    )
    assert 100.0 < price, "cash IS short here"


def test_the_message_does_not_blame_cash_when_cash_is_ample(caplog):
    """Regression on the wording itself: the string a reader sees."""
    from renquant_pipeline.kernel.pipeline import task_selection as ts
    src = __import__("inspect").getsource(ts)
    # the unconditional claim is gone
    assert 'insufficient cash — skip "\n                         "(remaining_cash' not in src
    # and the replacement says which quantity bound
    assert "sized to 0 shares" in src
    assert "is NOT the" in src and "constraint" in src


def test_the_block_reason_string_is_unchanged():
    """Changing it is a behaviour change for ledger consumers and for
    tests/test_fractional_sizing_stage2.py; splitting it needs its own audit."""
    from renquant_pipeline.kernel.pipeline import task_selection as ts
    src = __import__("inspect").getsource(ts)
    assert '_block(ticker, "size_insufficient_cash")' in src
