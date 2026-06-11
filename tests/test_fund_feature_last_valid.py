"""R2 audit fixes for _apply_fund_features (2026-06-11).

(1) HIGH: use the last VALID (finite) value per (ticker, col) as-of today, not
    the last-DATED row even when its value is NaN — derived fundamentals go NaN
    on some latest dates while an earlier date carries a valid value.
(2) BLOCKER guard: warn loudly when the fundamentals feed is stale (frozen).
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
    _apply_fund_features,
)


def test_uses_last_valid_value_not_last_dated_nan() -> None:
    """AAA's latest row is NaN but an earlier row is valid → use the valid
    value, do NOT fall back to the cross-sectional median."""
    panel = pd.DataFrame({
        "ticker": ["AAA", "AAA", "BBB", "CCC"],
        "date": pd.to_datetime(["2026-06-08", "2026-06-10",  # AAA: valid then NaN
                                "2026-06-10", "2026-06-10"]),
        "earnings_yield": [0.05, np.nan, 0.20, 0.30],
    })
    rows = {"AAA": {}}
    ctx = ["AAA", "BBB", "CCC"]
    n_real, n_imputed, medians = _apply_fund_features(
        rows, panel, pd.Timestamp("2026-06-11"), ctx, ["earnings_yield"])
    # AAA must recover its 06-08 value, not be median-imputed.
    assert rows["AAA"]["earnings_yield"] == 0.05
    assert n_real == 1 and n_imputed == 0


def test_all_nan_ticker_falls_back_to_median() -> None:
    panel = pd.DataFrame({
        "ticker": ["AAA", "BBB", "CCC"],
        "date": pd.to_datetime(["2026-06-10"] * 3),
        "earnings_yield": [np.nan, 0.20, 0.30],
    })
    rows = {"AAA": {}}
    ctx = ["AAA", "BBB", "CCC"]
    _apply_fund_features(rows, panel, pd.Timestamp("2026-06-11"), ctx,
                         ["earnings_yield"])
    assert rows["AAA"]["earnings_yield"] == 0.25  # median(0.20, 0.30)


def test_stale_feed_warns(caplog) -> None:
    panel = pd.DataFrame({
        "ticker": ["AAA", "BBB"],
        "date": pd.to_datetime(["2026-02-10", "2026-02-10"]),  # 121d stale
        "earnings_yield": [0.05, 0.20],
    })
    rows = {"AAA": {}}
    with caplog.at_level(logging.WARNING):
        _apply_fund_features(rows, panel, pd.Timestamp("2026-06-11"),
                             ["AAA", "BBB"], ["earnings_yield"])
    assert any("fundamentals feed STALE" in r.message for r in caplog.records)


def test_fresh_feed_does_not_warn(caplog) -> None:
    panel = pd.DataFrame({
        "ticker": ["AAA", "BBB"],
        "date": pd.to_datetime(["2026-06-10", "2026-06-10"]),
        "earnings_yield": [0.05, 0.20],
    })
    rows = {"AAA": {}}
    with caplog.at_level(logging.WARNING):
        _apply_fund_features(rows, panel, pd.Timestamp("2026-06-11"),
                             ["AAA", "BBB"], ["earnings_yield"])
    assert not any("STALE" in r.message for r in caplog.records)
