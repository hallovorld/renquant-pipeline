"""A trivial deferred-tax amount cannot justify blocking a buy.

`is_wash_sale_blocked_with_cost` has two branches. Branch (a) compares expected
return against NPV cost. Branch (b) is the fallback for callers without mu-hat.

Measured 2026-07-30: **none of the three live call sites passes
`expected_dollar_return`** (task_joint_actions.py, task_rotation.py, the
selection path), so branch (a) never executes in production and every wash-sale
block is unconditional. The docstring's promise that callers with mu-hat should
pass it is fulfilled by no caller — a cost-aware gate whose cost-awareness never
runs. Consequence: buys were zeroed on 3 of 5 sessions to protect $0.04-$13.62 of
NPV across 8 names while $6,868 of cash sat unused.

The floor is arithmetic, not preference: cost_npv = loss * tax_rate *
(1 - (1+r)^-h) = loss * 0.30 * (1 - 1.05^-2) ~= loss * 0.0279, so a $1.00 floor
is a realized loss of about $35.85.
"""
from __future__ import annotations

import datetime

import pytest

from renquant_pipeline.kernel.selection import (
    WASH_SALE_MIN_MATERIAL_NPV,
    is_wash_sale_blocked_with_cost,
    wash_sale_npv_cost,
)

TODAY = datetime.date(2026, 7, 30)
RECENT = {"AAA": datetime.date(2026, 7, 20)}          # 10d ago, inside 30d


def _call(loss: float, **kw):
    """Buy-path semantics: the floor is opt-in, so the tests pass it explicitly,
    exactly as task_joint_actions.py and task_rotation.py now do."""
    kw.setdefault("min_material_npv_cost", WASH_SALE_MIN_MATERIAL_NPV)
    return is_wash_sale_blocked_with_cost(
        "AAA", TODAY, RECENT, {"AAA": loss}, 30, **kw)


def test_the_DEFAULT_is_byte_identical_to_the_previous_behaviour():
    """The invariance guarantee: without opting in, a trivial loss still blocks.
    My first version defaulted the floor ON and broke the parking-sleeve test."""
    assert is_wash_sale_blocked_with_cost(
        "AAA", TODAY, RECENT, {"AAA": -1.43}, 30)[0] is True


def test_the_measured_trivial_case_no_longer_blocks():
    """A $1.43 loss is ~$0.04 of NPV — the amount that zeroed whole sessions."""
    blocked, reason, npv = _call(-1.43)
    assert npv < 0.05
    assert blocked is False, reason
    assert "materiality floor" in reason


def test_a_material_loss_still_blocks_unchanged():
    blocked, reason, npv = _call(-5000.0)
    assert npv >= 1.0
    assert blocked is True
    assert "NPV cost" in reason


def test_behaviour_is_byte_identical_at_and_above_the_floor():
    """The invariance claim, tested rather than asserted: only the sub-floor tail
    changes. At exactly the floor the old decision (block) must be preserved."""
    floor = 1.0
    # Solve for the loss whose NPV equals the floor using the FUNCTION, not a
    # hand-derived coefficient. My first version hardcoded 0.0279 and failed by
    # rounding — a test that re-derives the implementation's own constant is a
    # test of my arithmetic, not of the behaviour.
    coeff = wash_sale_npv_cost(-1.0, tax_rate=0.30, discount_rate=0.05,
                               estimated_hold_years=2.0)
    loss_at_floor = -(floor / coeff)
    _, _, npv = _call(loss_at_floor)
    assert npv >= floor, f"npv {npv} below the floor at the solved loss"
    assert _call(loss_at_floor)[0] is True, "at the floor the old decision must hold"
    # And just below it, the decision flips - the boundary is where it should be.
    assert _call(loss_at_floor * 0.9)[0] is False


def test_the_floor_is_configurable_and_zero_restores_the_old_behaviour():
    """A deployment that wants the previous semantics can have them exactly."""
    assert _call(-1.43, min_material_npv_cost=0.0)[0] is True
    assert _call(-1.43, min_material_npv_cost=1.0)[0] is False


def test_a_gain_sale_is_still_never_blocked():
    assert _call(+500.0)[0] is False


def test_outside_the_window_is_still_never_blocked():
    old = {"AAA": datetime.date(2026, 5, 1)}
    assert is_wash_sale_blocked_with_cost(
        "AAA", TODAY, old, {"AAA": -5000.0}, 30)[0] is False


def test_the_floor_only_affects_the_no_mu_hat_branch():
    """Branch (a) must be untouched: with mu-hat supplied, the cost-vs-return
    test decides, and a large expected return still unblocks a MATERIAL loss."""
    blocked, reason, _ = _call(-5000.0, expected_dollar_return=1_000_000.0)
    assert blocked is False
    assert "expected" in reason and "materiality floor" not in reason


def test_npv_arithmetic_matches_the_documented_derivation():
    """Pins the constant behind the $35.85 figure, so the comment cannot drift
    away from the code."""
    npv = wash_sale_npv_cost(-1000.0, tax_rate=0.30, discount_rate=0.05,
                             estimated_hold_years=2.0)
    assert npv == pytest.approx(1000.0 * 0.30 * (1 - 1.05 ** -2), rel=1e-6)
    assert npv == pytest.approx(27.89, abs=0.05)
