"""H-2 regression: single-day-loss stop defers to the trailing stop once armed.

H-2 (decision-tree deep audit, 2026-06-10): the single-day-loss (SDL) gate
fired on noise gap-downs even on big winners, crystallizing gains the trailing
stop was meant to ride. Live evidence: NVTS exited via `single_day_loss` at
+113%; `single_day_loss` exits averaged +9% pnl — the "stop" was systematically
selling winners.

Fix (opt-in, default off): once a position's peak gain has crossed the
regime's trailing-stop arm threshold, the SDL defers to the trailing stop
(separation of concerns: trailing stop manages winner giveback; SDL caps a
catastrophic gap on a loser/flat). These tests pin the behaviour.
"""
from __future__ import annotations

import datetime as dt

from renquant_pipeline.kernel.exits import HoldingState, check_single_day_loss


def _winner_gapping_down(shares: float = 10.0) -> HoldingState:
    """Up +120% from entry (peak_gain ≫ any trailing trigger), gapping down
    7.1% on the day (breaches a 6% absolute SDL threshold)."""
    return HoldingState(
        entry_price=100.0,
        entry_date=dt.date(2026, 1, 2),
        high_watermark=220.0,   # peak gain = 1.20
        prev_close=210.0,
        shares=shares,
    )


def test_sdl_fires_by_default_even_on_a_winner() -> None:
    """Legacy behaviour preserved: flag off → SDL still fires (the bug)."""
    state = _winner_gapping_down()
    sig = check_single_day_loss(195.0, state, 0.06)  # 7.1% drop ≥ 6%
    assert sig.should_exit
    assert sig.exit_type == "single_day_loss"


def test_sdl_defers_to_trailing_when_armed() -> None:
    """Flag on + peak_gain ≥ trailing trigger → SDL skips (the fix)."""
    state = _winner_gapping_down()
    sig = check_single_day_loss(
        195.0, state, 0.06,
        sdl_skip_if_trailing_armed=True,
        trailing_trigger_pct=0.12,
    )
    assert not sig.should_exit


def test_sdl_still_fires_when_not_yet_armed() -> None:
    """A position whose peak gain has NOT reached the trailing trigger keeps
    its SDL protection even with the flag on (it is not a confirmed winner)."""
    state = HoldingState(
        entry_price=100.0,
        entry_date=dt.date(2026, 1, 2),
        high_watermark=105.0,   # peak gain = 0.05 < 0.12 trigger
        prev_close=104.0,
        shares=10.0,
    )
    sig = check_single_day_loss(
        96.0, state, 0.06,  # (104-96)/104 = 7.7% ≥ 6%
        sdl_skip_if_trailing_armed=True,
        trailing_trigger_pct=0.12,
    )
    assert sig.should_exit
    assert sig.exit_type == "single_day_loss"


def test_short_position_keeps_unconditional_sdl() -> None:
    """The skip is long-only — a short (shares<0) must keep its SDL."""
    state = _winner_gapping_down(shares=-10.0)
    # Short loses on an UP move; build an up gap that breaches the threshold.
    state.prev_close = 100.0
    sig = check_single_day_loss(
        108.0, state, 0.06,  # +8% up move ≥ 6% (short loss)
        sdl_skip_if_trailing_armed=True,
        trailing_trigger_pct=0.12,
    )
    assert sig.should_exit
    assert "SHORT" in sig.reason


def test_disabled_flag_is_a_noop_when_trigger_zero() -> None:
    """No trailing trigger configured → flag cannot skip (nothing to defer to)."""
    state = _winner_gapping_down()
    sig = check_single_day_loss(
        195.0, state, 0.06,
        sdl_skip_if_trailing_armed=True,
        trailing_trigger_pct=0.0,
    )
    assert sig.should_exit


def test_compute_exits_defers_sdl_but_trailing_still_fires() -> None:
    """End-to-end via compute_exits: with the flag set, a winner's noise
    gap-down is NOT an SDL exit, but a real giveback past the trail still
    fires the trailing stop (the correct tool owns the exit)."""
    from renquant_pipeline.kernel.exits import compute_exits

    today = dt.date(2026, 3, 2)
    params = {
        "max_single_day_loss_pct": 0.06,
        "trailing_stop_trigger_pct": 0.12,
        "trailing_stop_trail_pct": 0.25,
        "sdl_skip_if_trailing_armed": True,
    }

    # Noise gap-down on a +120% winner: SDL would have fired; now deferred,
    # and the price is still well above the 25% trail floor (220*0.75=165).
    state = _winner_gapping_down()
    sig, _ = compute_exits(195.0, today, "hold", state, params)
    assert not sig.should_exit  # neither SDL nor trailing — winner rides on

    # A real giveback below the trail floor → trailing stop fires (not SDL).
    state2 = _winner_gapping_down()
    sig2, _ = compute_exits(160.0, today, "hold", state2, params)
    assert sig2.should_exit
    assert sig2.exit_type == "trailing_stop"
