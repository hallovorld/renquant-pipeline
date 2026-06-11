from __future__ import annotations

from types import SimpleNamespace

from renquant_pipeline.kernel.sizing import (
    compute_position_size,
    conviction_multiplier,
    conviction_score_for_object,
    conviction_score_percentiles,
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
