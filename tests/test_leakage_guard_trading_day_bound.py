"""The leakage guard must not under-purge a TRADING-day label.

`pd.offsets.BDay(n)` counts business days and does NOT skip market holidays.
Measured on SPY's real trading dates 2016-01-04 -> 2026-07-29 (2,597 cutoffs):
`BDay(60)` falls before the true 60th trading day on **99.8%** of cutoffs, short
by mean +3.17 / max +10 calendar days = mean 2.23 / max 6 TRADING days.

A trap this pins: `BDay(60)` spans exactly 12 weeks = 84 calendar days and
`ceil(60*7/5)` is also 84, so switching the unit alone fixes nothing.
"""
from __future__ import annotations

import pytest

from renquant_pipeline.kernel.walk_forward.leakage_guard import (
    trading_days_to_calendar_bound as bound,
)


def test_zero_and_negative_are_zero():
    assert bound(0) == 0 and bound(-5) == 0


def test_the_unit_switch_alone_would_NOT_have_fixed_it():
    """ceil(60*7/5) == 84 == BDay(60) in calendar days. The bound must exceed it,
    or this change is cosmetic."""
    assert -(-60 * 7 // 5) == 84
    assert bound(60) > 84, "bound adds no holiday allowance — same defect"


def test_bound_covers_the_measured_worst_case_for_60():
    """Measured max requirement: the true 60th trading day sits up to +10
    calendar days past BDay(60)=84, i.e. 94 days."""
    assert bound(60) >= 94, f"bound(60)={bound(60)} under the measured 94"


def test_bound_is_monotone_and_superlinear_in_the_horizon():
    vals = [bound(n) for n in (5, 20, 60, 120, 250)]
    assert vals == sorted(vals)
    for n in (5, 20, 60, 120, 250):
        assert bound(n) >= -(-n * 7 // 5), n


def test_bound_does_not_explode():
    """Over-purging costs power, so the bound must stay near the true span."""
    assert bound(60) <= 110
    assert bound(250) <= 420


def test_the_guard_is_still_on_the_KNOWN_SHORT_bound_and_that_is_deliberate():
    """The primitive lands WITHOUT being wired in, because switching it moves
    walk-forward fold selection and therefore which model is promoted --
    tests/test_wf_fold_selection_parity.py::
    test_newest_fold_inside_embargo_window_older_wins fails under the corrected
    bound, correctly detecting that. #228 requires an A/B for that step.

    This test pins the CURRENT behaviour so the switch cannot happen silently:
    a trained_date 90 calendar days back still PASSES, because BDay(60) is 84.
    When someone wires the bound in, this test must fail and be replaced -- that
    failure is the intended tripwire."""
    import pandas as pd

    from renquant_pipeline.kernel.walk_forward.leakage_guard import (
        assert_no_leakage,
        trading_days_to_calendar_bound,
    )

    trained = pd.Timestamp("2024-01-01")
    today = trained + pd.Timedelta(days=90)
    assert (trained + pd.tseries.offsets.BDay(60)) < today
    assert trading_days_to_calendar_bound(60) > 90, (
        "the corrected bound WOULD reject this fold")
    assert_no_leakage(trained, today, lookahead_days=60)  # still admitted
