"""σ-horizon fix (opt-in): the σ-aware stops can use the unambiguously-daily
realized_sigma_daily instead of the annualized-in-prod state.sigma/√5.

In prod state.sigma is an ANNUALIZED realized vol (persisted), so the legacy
path (state.sigma/√5) overstates daily σ ~7.1× and the σ-aware single-day-loss
stop is effectively off. The flag flips to the correct daily σ and re-activates
it. Default OFF preserves legacy behaviour. See orchestrator
doc/audit/2026-06-11-sigma-horizon-contract.md.
"""
from __future__ import annotations

import datetime as dt

import pytest

from renquant_pipeline.kernel.exits import (
    HoldingState,
    _resolve_daily_sigma,
    check_single_day_loss,
    compute_exits,
)


def _state(sigma, realized_daily):
    return HoldingState(
        entry_price=100.0, entry_date=dt.date(2026, 1, 2),
        high_watermark=110.0, prev_close=100.0, shares=10.0,
        sigma=sigma, realized_sigma_daily=realized_daily,
    )


def test_default_uses_state_sigma_over_sqrt5() -> None:
    s = _state(0.30, 0.019)  # annualized state.sigma, true daily realized
    assert _resolve_daily_sigma(s) == pytest.approx(0.30 / 5 ** 0.5)  # ≈0.134


def test_flag_prefers_realized_daily() -> None:
    s = _state(0.30, 0.019)
    assert _resolve_daily_sigma(s, prefer_realized_daily=True) == pytest.approx(0.019)


def test_flag_falls_back_to_state_sigma_when_no_daily() -> None:
    s = _state(0.30, None)
    assert _resolve_daily_sigma(s, prefer_realized_daily=True) == pytest.approx(0.30 / 5 ** 0.5)


def test_sdl_dormant_by_default_active_with_flag() -> None:
    """A 7% single-day drop, sdl_n_sigma=3, absolute cap off (BULL_CALM shape).
    Legacy: threshold 3·(0.30/√5)=0.40 → no fire. Flag: 3·0.019=0.057 → fires."""
    sig_legacy = check_single_day_loss(93.0, _state(0.30, 0.019), 0.0, sdl_n_sigma=3)
    assert not sig_legacy.should_exit

    sig_fixed = check_single_day_loss(
        93.0, _state(0.30, 0.019), 0.0, sdl_n_sigma=3,
        prefer_realized_daily_sigma=True)
    assert sig_fixed.should_exit
    assert sig_fixed.exit_type == "single_day_loss"


def test_compute_exits_threads_the_flag() -> None:
    params = {"max_single_day_loss_pct": 0.0, "sdl_n_sigma": 3,
              "prefer_realized_daily_sigma": True}
    sig, _ = compute_exits(93.0, dt.date(2026, 3, 2), "hold",
                           _state(0.30, 0.019), params)
    assert sig.should_exit and sig.exit_type == "single_day_loss"
    # without the flag the same drop is NOT an SDL exit
    params_off = dict(params, prefer_realized_daily_sigma=False)
    sig2, _ = compute_exits(93.0, dt.date(2026, 3, 2), "hold",
                            _state(0.30, 0.019), params_off)
    assert not (sig2.should_exit and sig2.exit_type == "single_day_loss")
