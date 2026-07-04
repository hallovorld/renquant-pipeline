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


# ── S-FRAC v2 stage 2 additions (2026-07-03) ────────────────────────────────


def test_fractional_floor_6dp_never_rounds_up() -> None:
    """The 6dp quantization must FLOOR — realized notional may never round UP
    past the cap / available cash (stage-2 contract; a round() here would)."""
    import math

    from renquant_pipeline.kernel.sizing import compute_position_size

    cases = [
        # (pv, cash, max_pct, price) — chosen so target/price has a long tail
        (10_000.0, 5_000.0, 0.0381, 1_100.0),   # BLK: 346363.63…e-6 → 0.346363
        (10_000.0, 10_000.0, 0.02, 3.0),        # 66.66… → 66.666666
        (10_345.0, 10_000.0, 0.04, 950.0),      # 0.43557767… → 0.435577
        (9_999.0, 9_999.0, 0.0777, 777.77),     # awkward decimals
        (10_000.0, 10_000.0, 0.019999999, 100.0),
    ]
    for pv, cash, max_pct, price in cases:
        pct, qty = compute_position_size(
            pv, cash, max_pct, 0.0, price, fractional=True, min_notional=0.0,
        )
        assert qty > 0
        # 6dp-quantized exactly…
        assert abs(qty * 1e6 - round(qty * 1e6)) < 1e-6
        # …and never above the un-quantized quotient (floor, not round):
        target = min(max_pct * pv, cash)
        assert qty <= target / price + 1e-15
        # realized notional never exceeds the pre-quantization target
        assert qty * price <= target + 1e-9
        # a straight round() would have EXCEEDED the quotient for at least
        # the 66.66… case — pin that flooring actually differs from rounding
        assert math.floor((target / price) * 1e6) / 1e6 == qty


def test_fractional_rounding_case_would_round_up() -> None:
    """Explicit pin: a quotient whose 7th decimal ≥ 5 must truncate, not round."""
    from renquant_pipeline.kernel.sizing import compute_position_size

    # target 200 / price 3 → 66.6666666… ; round-to-6dp gives 66.666667 (UP)
    _, qty = compute_position_size(
        10_000.0, 10_000.0, 0.02, 0.0, 3.0, fractional=True, min_notional=0.0,
    )
    assert qty == 66.666666
    assert qty * 3.0 <= 200.0


def test_fractional_dust_floor_usd_reader() -> None:
    from renquant_pipeline.kernel.sizing import (
        DEFAULT_MIN_FRACTIONAL_TRADE_NOTIONAL_USD,
        MIN_FRACTIONAL_NOTIONAL_USD,
        fractional_dust_floor_usd,
    )

    # Defaults: max($1 broker floor, $1 min_notional, $25 anti-churn) = $25.
    assert DEFAULT_MIN_FRACTIONAL_TRADE_NOTIONAL_USD == 25.0
    assert fractional_dust_floor_usd(None) == 25.0
    assert fractional_dust_floor_usd({}) == 25.0
    # Operator override respected but never below the broker floor.
    assert fractional_dust_floor_usd(
        {"execution": {"fractional_shares": {"min_fractional_trade_notional": 5.0}}}
    ) == 5.0
    assert fractional_dust_floor_usd(
        {"execution": {"fractional_shares": {"min_fractional_trade_notional": 0.0}}}
    ) == MIN_FRACTIONAL_NOTIONAL_USD
    # Malformed values fall back to the $25 default (fail-safe: higher floor).
    assert fractional_dust_floor_usd(
        {"execution": {"fractional_shares": {"min_fractional_trade_notional": "x"}}}
    ) == 25.0
    assert fractional_dust_floor_usd(
        {"execution": {"fractional_shares": {"min_fractional_trade_notional": True}}}
    ) == 25.0
    # min_notional above the anti-churn floor raises the effective floor.
    assert fractional_dust_floor_usd(
        {"execution": {"fractional_shares": {"min_notional": 40.0}}}
    ) == 40.0


def test_fractional_eligible_blocklist_and_ctx_map() -> None:
    from renquant_pipeline.kernel.sizing import fractional_eligible

    assert fractional_eligible("BLK", None) is True
    assert fractional_eligible("BLK", {}) is True
    cfg = {"execution": {"fractional_shares": {"non_fractionable_tickers": ["blk"]}}}
    assert fractional_eligible("BLK", cfg) is False       # case-insensitive
    assert fractional_eligible("AVGO", cfg) is True
    # Malformed blocklist fails CLOSED for every name (whole-share fallback).
    bad = {"execution": {"fractional_shares": {"non_fractionable_tickers": "oops"}}}
    assert fractional_eligible("AVGO", bad) is False
    # Explicit broker-metadata False wins; missing/True stays eligible.
    assert fractional_eligible("BLK", {}, {"BLK": False}) is False
    assert fractional_eligible("BLK", {}, {"BLK": True}) is True
    assert fractional_eligible("BLK", {}, {}) is True


def test_sizing_target_notional_matches_compute_position_size() -> None:
    """Single-source §7.4 target_notional: the helper must agree bit-for-bit
    with what compute_position_size quantizes (no hand-copied math drift)."""
    from renquant_pipeline.kernel.sizing import (
        compute_position_size,
        sizing_target_notional,
    )

    pv, cash, max_pct, reserve, price = 10_000.0, 5_000.0, 0.0381, 0.1, 1_100.0
    target, investable = sizing_target_notional(pv, cash, max_pct, reserve)
    assert investable == cash - pv * reserve
    _, qty = compute_position_size(
        pv, cash, max_pct, reserve, price, fractional=True, min_notional=0.0,
    )
    import math
    assert qty == math.floor((min(target, investable) / price) * 1e6) / 1e6
    # Override branch (BEAR defensive): investable == cash, pct == override.
    t2, inv2 = sizing_target_notional(pv, cash, 0.9, reserve, override_pct=0.05)
    assert inv2 == cash
    assert t2 == 0.05 * pv
    # Zero/negative budget → 0.0 target.
    assert sizing_target_notional(pv, cash, 0.0, 0.0)[0] == 0.0
    assert sizing_target_notional(0.0, cash, 0.1, 0.0)[0] == 0.0
