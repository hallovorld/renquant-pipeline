from __future__ import annotations

from types import SimpleNamespace

import pytest

from renquant_pipeline.kernel.sizing import (
    compute_position_size,
    conviction_multiplier,
    conviction_score_for_object,
    conviction_score_percentiles,
    fractional_sizing_cfg,
)


def test_compute_position_size_does_not_oversize_high_priced_fallback() -> None:
    actual_pct, shares = compute_position_size(
        portfolio_value=10345.0,
        available_cash=10000.0,
        max_position_pct=0.0823,
        cash_reserve_pct=0.0,
        price=1137.88,
    )

    assert (actual_pct, shares) == (0.0, 0)


# ── Fractional shares (strategy-104 #35 cash-drag follow-up) ────────────────


def test_fractional_high_priced_sub_share_target_deploys_not_skipped() -> None:
    """High-priced name (BLK ~$950) with a ~4% / ~$413 target buys a FRACTIONAL
    quantity when fractional=True, instead of rounding to 0 whole shares."""
    # Whole-share mode (default) skips: $413 cap / $950 price = 0 whole shares.
    pct_whole, shares_whole = compute_position_size(
        portfolio_value=10345.0,
        available_cash=10000.0,
        max_position_pct=0.04,
        cash_reserve_pct=0.0,
        price=950.0,
    )
    assert (pct_whole, shares_whole) == (0.0, 0)
    assert isinstance(shares_whole, int)

    # Fractional mode deploys the capped target as a float quantity.
    pct_frac, shares_frac = compute_position_size(
        portfolio_value=10345.0,
        available_cash=10000.0,
        max_position_pct=0.04,
        cash_reserve_pct=0.0,
        price=950.0,
        fractional=True,
    )
    assert isinstance(shares_frac, float)
    assert shares_frac > 0.0
    # ~$413.8 target / $950 ≈ 0.4356 shares (floored to 6 dp).
    assert abs(shares_frac - 0.435578) < 1e-4
    # Still bounded by the SAME 4% cap — does NOT oversize the high-priced name.
    assert pct_frac <= 0.04 + 1e-9
    assert abs(shares_frac * 950.0 - pct_frac * 10345.0) < 1e-6


def test_fractional_disabled_matches_legacy_whole_share_skip() -> None:
    """fractional=False is byte-for-byte the existing whole-share behaviour."""
    legacy = compute_position_size(
        portfolio_value=10345.0,
        available_cash=10000.0,
        max_position_pct=0.0823,
        cash_reserve_pct=0.0,
        price=1137.88,
    )
    explicit_off = compute_position_size(
        portfolio_value=10345.0,
        available_cash=10000.0,
        max_position_pct=0.0823,
        cash_reserve_pct=0.0,
        price=1137.88,
        fractional=False,
    )
    assert legacy == explicit_off == (0.0, 0)


def test_fractional_dust_below_min_notional_is_skipped() -> None:
    """A sub-min_notional target is dropped (no sub-$1 odd-lot order)."""
    # Cap is 0.005% of a $10k book = $0.50 target → below the $1 floor.
    pct, shares = compute_position_size(
        portfolio_value=10000.0,
        available_cash=10000.0,
        max_position_pct=0.00005,
        cash_reserve_pct=0.0,
        price=950.0,
        fractional=True,
        min_notional=1.0,
    )
    assert (pct, shares) == (0.0, 0.0)

    # Lowering min_notional below the target lets the same dust through.
    pct2, shares2 = compute_position_size(
        portfolio_value=10000.0,
        available_cash=10000.0,
        max_position_pct=0.00005,
        cash_reserve_pct=0.0,
        price=950.0,
        fractional=True,
        min_notional=0.10,
    )
    assert shares2 > 0.0
    assert pytest.approx(shares2 * 950.0, abs=1e-6) == pct2 * 10000.0


def test_fractional_cheap_stock_unchanged_notional() -> None:
    """For a cheap name the fractional notional matches the whole-share target
    closely (fractional just removes the round-down-to-whole remainder)."""
    pct_whole, shares_whole = compute_position_size(
        portfolio_value=10345.0,
        available_cash=10000.0,
        max_position_pct=0.0823,
        cash_reserve_pct=0.0,
        price=50.0,
    )
    assert shares_whole == 17  # $851 cap / $50 → 17 whole shares = $850

    pct_frac, shares_frac = compute_position_size(
        portfolio_value=10345.0,
        available_cash=10000.0,
        max_position_pct=0.0823,
        cash_reserve_pct=0.0,
        price=50.0,
        fractional=True,
    )
    # Fractional captures the full capped target (~17.03 shares) without the
    # whole-share round-down, and stays under the same cap.
    assert shares_frac >= 17.0
    assert pct_frac <= 0.0823 + 1e-9


def test_fractional_sizing_cfg_reads_execution_block() -> None:
    assert fractional_sizing_cfg(None) == (False, 1.0)
    assert fractional_sizing_cfg({}) == (False, 1.0)
    assert fractional_sizing_cfg(
        {"execution": {"fractional_shares": {"enabled": True, "min_notional": 5.0}}}
    ) == (True, 5.0)
    # Malformed min_notional falls back to the $1 default; flag still honoured.
    assert fractional_sizing_cfg(
        {"execution": {"fractional_shares": {"enabled": True, "min_notional": "oops"}}}
    ) == (True, 1.0)
    # Negative min_notional is rejected → default.
    assert fractional_sizing_cfg(
        {"execution": {"fractional_shares": {"enabled": False, "min_notional": -3.0}}}
    ) == (False, 1.0)


def test_compute_position_size_keeps_cheap_stock_under_same_cap() -> None:
    actual_pct, shares = compute_position_size(
        portfolio_value=10345.0,
        available_cash=10000.0,
        max_position_pct=0.0823,
        cash_reserve_pct=0.0,
        price=50.0,
    )

    assert shares == 17
    assert actual_pct <= 0.0823


def test_compute_position_size_does_not_fallback_when_cap_is_non_positive() -> None:
    assert compute_position_size(
        portfolio_value=10000.0,
        available_cash=10000.0,
        max_position_pct=0.0,
        cash_reserve_pct=0.0,
        price=100.0,
    ) == (0.0, 0)


def test_conviction_rank_percentile_preserves_negative_score_dispersion() -> None:
    cands = [
        SimpleNamespace(ticker="LOW", panel_score=-0.13),
        SimpleNamespace(ticker="MID", panel_score=-0.12),
        SimpleNamespace(ticker="HIGH", panel_score=-0.10),
    ]
    cfg = {"enabled": True, "score_mode": "rank_percentile"}
    percentiles = conviction_score_percentiles(cands)

    multipliers = [
        conviction_multiplier(
            conviction_score_for_object(cand, cfg, percentiles),
            cfg,
        )
        for cand in cands
    ]

    assert multipliers[0] < multipliers[1] < multipliers[2]
    assert multipliers[0] > 0.5
    assert multipliers[2] == 1.0


def test_conviction_raw_negative_scores_still_use_legacy_floor() -> None:
    cfg = {"enabled": True}

    assert conviction_multiplier(-0.13, cfg) == 0.5
    assert conviction_multiplier(-0.10, cfg) == 0.5


def test_conviction_rank_percentile_averages_ties() -> None:
    cands = [
        SimpleNamespace(ticker="A", panel_score=-0.1),
        SimpleNamespace(ticker="B", panel_score=-0.1),
        SimpleNamespace(ticker="C", panel_score=0.2),
    ]
    percentiles = conviction_score_percentiles(cands)

    assert percentiles["A"] == percentiles["B"]
    assert percentiles["A"] == 0.5
    assert percentiles["C"] == 1.0
