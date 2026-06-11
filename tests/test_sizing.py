from __future__ import annotations

from renquant_pipeline.kernel.sizing import compute_position_size


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
