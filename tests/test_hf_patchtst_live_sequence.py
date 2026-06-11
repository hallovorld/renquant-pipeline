"""Regression guard for the hf_patchtst FROZEN-SCORE bug (2026-06-10).

A live PatchTST model produced byte-identical cross-sectional scores every
trading day. Root cause: ``ApplyScoresTask`` (and the legacy dispatch below
it) built the sequence ``panel_history`` from the STATIC training parquet
``data/alpha158_291_fundamental_dataset.parquet`` (max date 2026-02-10) via
``full_panel[date < today]``. For any live ``today`` past the parquet's last
bar this re-selected the SAME final ``seq_len`` dates every run → identical
input sequences → identical panel_score for every ticker, every day.

Fix: ``_build_live_panel_history`` computes the alpha158 sequence from live
``ctx.ohlcv`` ending at ``today``. These tests pin two invariants:

  1. The builder slices to ``today`` (advancing the as-of date advances the
     window) and the per-bar alpha158 features differ across two as-of dates.
  2. A history scorer fed those two windows produces DATE-VARYING scores
     (the freeze is gone).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from renquant_pipeline.kernel.panel_pipeline import job_panel_scoring as J


# ── Fixtures ────────────────────────────────────────────────────────────────

def _synthetic_ohlcv(seed: int, n: int = 300) -> pd.DataFrame:
    """Random-walk OHLCV indexed by business day.

    Starts in 2025 so the alpha158 rolling warmup (~70 bars) clears well
    before the as-of dates used by the tests (late-April / late-May 2026),
    leaving ≥ seq_len causal bars at each as-of date.
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-05-01", periods=n)
    close = 100.0 + np.cumsum(rng.normal(0, 1.5, size=n))
    close = np.maximum(close, 1.0)
    open_ = close * (1 + rng.normal(0, 0.005, size=n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.01, size=n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.01, size=n)))
    vol = rng.integers(1_000_000, 5_000_000, size=n).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=dates,
    )


class _FakeHistoryScorer:
    """Minimal history-requiring scorer — alpha158-only, no torch dependency.

    ``score_with_history`` returns the per-ticker mean of the last bar's
    features so the test can detect when the input window changes by date.
    """

    requires_history = True

    def __init__(self, feature_cols, seq_len):
        self.feature_cols = list(feature_cols)
        self.seq_len = int(seq_len)
        self.metadata = {"kind": "hf_patchtst"}

    def score_with_history(self, panel_history, target_tickers):
        out = {}
        for tkr in target_tickers:
            g = panel_history[panel_history["ticker"] == tkr].sort_values("date")
            if len(g) < self.seq_len:
                continue
            last = g[self.feature_cols].iloc[-1]
            out[tkr] = float(np.nanmean(last.to_numpy(dtype=float)))
        return pd.Series(out, name="panel_score")


class _Ctx:
    pass


def _make_ctx(ohlcv, tickers, today):
    ctx = _Ctx()
    ctx.ohlcv = ohlcv
    ctx.config = {"ranking": {"panel_scoring": {"kind": "hf_patchtst"}},
                  "watchlist": list(tickers)}
    ctx.holdings = {}
    ctx.models = {}
    ctx.today = pd.Timestamp(today)
    return ctx


# ── Tests ───────────────────────────────────────────────────────────────────

def test_live_builder_window_advances_with_today():
    """The sequence window must end at `today`; a later date yields a later
    last bar (proving the slice is date-relative, not a fixed tail)."""
    from renquant_pipeline.kernel.panel_pipeline.alpha158_features import (
        alpha158_feature_names,
    )

    tickers = ["AAA", "BBB", "CCC"]
    ohlcv = {t: _synthetic_ohlcv(seed=i) for i, t in enumerate(tickers)}
    feature_cols = alpha158_feature_names()[:30]
    scorer = _FakeHistoryScorer(feature_cols, seq_len=24)

    d1, d2 = "2026-04-30", "2026-05-29"
    ph1 = J._build_live_panel_history(_make_ctx(ohlcv, tickers, d1), scorer,
                                      tickers, pd.Timestamp(d1))
    ph2 = J._build_live_panel_history(_make_ctx(ohlcv, tickers, d2), scorer,
                                      tickers, pd.Timestamp(d2))
    assert ph1 is not None and ph2 is not None

    # Window length is seq_len per ticker, and the last bar advanced.
    for tkr in tickers:
        g1 = ph1[ph1["ticker"] == tkr]
        g2 = ph2[ph2["ticker"] == tkr]
        assert len(g1) == 24
        assert len(g2) == 24
        assert g1["date"].max() <= pd.Timestamp(d1)
        assert g2["date"].max() <= pd.Timestamp(d2)
        assert g2["date"].max() > g1["date"].max()


def test_scores_vary_by_date_via_live_builder():
    """End-to-end: identical scorer + same tickers, two distinct as-of dates
    → DIFFERENT scores. This is the exact freeze the fix eliminates."""
    from renquant_pipeline.kernel.panel_pipeline.alpha158_features import (
        alpha158_feature_names,
    )

    tickers = ["AAA", "BBB", "CCC", "DDD"]
    ohlcv = {t: _synthetic_ohlcv(seed=10 + i) for i, t in enumerate(tickers)}
    feature_cols = alpha158_feature_names()[:40]
    scorer = _FakeHistoryScorer(feature_cols, seq_len=24)

    d1, d2 = "2026-04-30", "2026-05-29"
    ph1 = J._build_live_panel_history(_make_ctx(ohlcv, tickers, d1), scorer,
                                      tickers, pd.Timestamp(d1))
    ph2 = J._build_live_panel_history(_make_ctx(ohlcv, tickers, d2), scorer,
                                      tickers, pd.Timestamp(d2))
    s1 = scorer.score_with_history(ph1, tickers)
    s2 = scorer.score_with_history(ph2, tickers)

    aligned = pd.DataFrame({"d1": s1, "d2": s2}).dropna()
    assert not aligned.empty
    # At least one score must differ between the two dates — pre-fix every
    # value was byte-identical.
    assert (aligned["d1"] != aligned["d2"]).any(), (
        "scores frozen across dates — regression of the 2026-06-10 bug"
    )


def test_builder_returns_none_without_ohlcv():
    """No live OHLCV → builder returns None so the caller can fail closed
    (it must NOT silently fall back to a stale window for live dates)."""
    scorer = _FakeHistoryScorer(["KMID", "KLEN"], seq_len=24)
    ctx = _make_ctx({}, ["AAA"], "2026-05-01")
    assert J._build_live_panel_history(ctx, scorer, ["AAA"], ctx.today) is None
