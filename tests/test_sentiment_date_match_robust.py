"""R2 audit (MED): sentiment as-of match must be robust to time/tz drift.

The prior `pd.to_datetime(sdf["date"]) == today_ts` compared FULL timestamps, so
a tz-aware or time-bearing sentiment date that is the same calendar day as
ctx.today would miss → the whole universe median-fills. The fix matches at DATE
granularity (tz/time stripped) while keeping the exact-DATE semantics training
used (not an as-of fallback).
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
    _apply_sentiment_features,
)

_COLS = ["mean_sentiment", "sentiment_pos_share", "n_articles_log"]


def _ctx():
    return SimpleNamespace(config={"ranking": {"panel_scoring": {}}},
                           regime="BULL_CALM")


def test_matches_tz_aware_same_day(tmp_path) -> None:
    """A tz-aware NY-time sentiment row on the same calendar day still matches
    a naive midnight today_ts (would MISS under the old == compare)."""
    pd.DataFrame({
        "date": [pd.Timestamp("2026-06-10 13:30:00", tz="America/New_York")],
        "mean_sentiment": [0.4], "sentiment_pos_share": [0.6], "n_articles": [10],
    }).to_parquet(tmp_path / "AAA.parquet")
    rows = {"AAA": {}}
    n_hit, n_miss, _ = _apply_sentiment_features(
        _ctx(), SimpleNamespace(metadata={}), rows, tmp_path,
        pd.Timestamp("2026-06-10"), ["AAA"], _COLS)
    assert n_hit == 1 and n_miss == 0
    assert rows["AAA"]["mean_sentiment"] == 0.4
    assert rows["AAA"]["sentiment_pos_share"] == 0.6


def test_matches_intraday_time_component(tmp_path) -> None:
    """A naive but time-bearing timestamp on the same day still matches."""
    pd.DataFrame({
        "date": [pd.Timestamp("2026-06-10 09:31:00")],
        "mean_sentiment": [0.2], "sentiment_pos_share": [0.5], "n_articles": [3],
    }).to_parquet(tmp_path / "BBB.parquet")
    rows = {"BBB": {}}
    n_hit, n_miss, _ = _apply_sentiment_features(
        _ctx(), SimpleNamespace(metadata={}), rows, tmp_path,
        pd.Timestamp("2026-06-10"), ["BBB"], _COLS)
    assert n_hit == 1 and n_miss == 0


def test_different_day_still_misses(tmp_path) -> None:
    """A genuinely different day is still a miss (no as-of fallback)."""
    pd.DataFrame({
        "date": [pd.Timestamp("2026-06-09")],
        "mean_sentiment": [0.9], "sentiment_pos_share": [0.9], "n_articles": [5],
    }).to_parquet(tmp_path / "CCC.parquet")
    rows = {"CCC": {}}
    n_hit, n_miss, _ = _apply_sentiment_features(
        _ctx(), SimpleNamespace(metadata={}), rows, tmp_path,
        pd.Timestamp("2026-06-10"), ["CCC"], _COLS)
    assert n_hit == 0 and n_miss == 1
