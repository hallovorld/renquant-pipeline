"""Regression guard for BL-1: PatchTST CSRankNorm cross-section contamination.

BL-1 (decision-tree deep audit, 2026-06-10): the live sequence panel was built
over only the post-gate candidate subset (often 1–3 survivors), so the
per-day CSRankNorm inside ``score_with_history`` ranked each feature over a
handful of names — wildly out-of-distribution vs training, which ranked over
the full 142-name watchlist. The model then emitted uniformly negative /
degenerate scores. The XGB extra-feature path already guards this via
``_stable_feature_context_tickers``; PatchTST did not.

Fix: ``_build_live_panel_history`` builds the sequence panel over the STABLE
context (watchlist / training universe / holdings), and the caller extracts
scores for ``target_tickers`` only. These tests pin:

  1. With many watchlist names but few candidates, the built panel spans the
     full stable cross-section (not the candidate subset).
  2. ``csranknorm_context_mode="candidates"`` restores the legacy (degenerate)
     behaviour for sims that pin a golden snapshot.
  3. The broad cross-section makes the per-day CSRankNorm rank a target among
     the whole universe, not among 1–2 peers.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from renquant_pipeline.kernel.panel_pipeline import job_panel_scoring as J
from renquant_pipeline.kernel.panel_pipeline.alpha158_features import (
    alpha158_feature_names,
)
from renquant_pipeline.kernel.panel_pipeline.hf_patchtst_scorer import (
    _csrank_norm_per_day,
)


def _synthetic_ohlcv(seed: int, n: int = 300) -> pd.DataFrame:
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
    requires_history = True

    def __init__(self, feature_cols, seq_len):
        self.feature_cols = list(feature_cols)
        self.seq_len = int(seq_len)
        self.metadata = {"kind": "hf_patchtst"}


def _make_ctx(ohlcv, watchlist, today, context_mode=None):
    ctx = type("Ctx", (), {})()
    ctx.ohlcv = ohlcv
    panel_cfg = {"kind": "hf_patchtst"}
    if context_mode is not None:
        panel_cfg["csranknorm_context_mode"] = context_mode
    ctx.config = {"ranking": {"panel_scoring": panel_cfg},
                  "watchlist": list(watchlist)}
    ctx.holdings = {}
    ctx.models = {}
    ctx.today = pd.Timestamp(today)
    return ctx


def test_panel_spans_stable_context_not_candidate_subset():
    """20-name watchlist, 2 candidates → panel covers the full cross-section."""
    watchlist = [f"T{i:02d}" for i in range(20)]
    candidates = ["T03", "T11"]  # the post-gate survivors
    ohlcv = {t: _synthetic_ohlcv(seed=i) for i, t in enumerate(watchlist)}
    feature_cols = alpha158_feature_names()[:30]
    scorer = _FakeHistoryScorer(feature_cols, seq_len=24)

    ctx = _make_ctx(ohlcv, watchlist, "2026-05-29")
    panel = J._build_live_panel_history(ctx, scorer, candidates, ctx.today)

    assert panel is not None
    built = set(panel["ticker"].unique())
    # The whole watchlist with live OHLCV is present — not just the 2 candidates.
    assert built == set(watchlist)
    assert set(candidates).issubset(built)


def test_candidates_mode_restores_legacy_narrow_panel():
    """Opt-out flag keeps only the candidate subset (legacy degenerate path)."""
    watchlist = [f"T{i:02d}" for i in range(20)]
    candidates = ["T03", "T11"]
    ohlcv = {t: _synthetic_ohlcv(seed=i) for i, t in enumerate(watchlist)}
    feature_cols = alpha158_feature_names()[:30]
    scorer = _FakeHistoryScorer(feature_cols, seq_len=24)

    ctx = _make_ctx(ohlcv, watchlist, "2026-05-29", context_mode="candidates")
    panel = J._build_live_panel_history(ctx, scorer, candidates, ctx.today)

    assert panel is not None
    assert set(panel["ticker"].unique()) == set(candidates)


def test_broad_panel_yields_non_degenerate_csranknorm_rank():
    """The per-day CSRankNorm over the broad panel ranks a target among the
    whole universe; over the candidate-only panel the rank collapses."""
    watchlist = [f"T{i:02d}" for i in range(20)]
    candidates = ["T03", "T11"]
    ohlcv = {t: _synthetic_ohlcv(seed=100 + i) for i, t in enumerate(watchlist)}
    feature_cols = alpha158_feature_names()[:30]
    scorer = _FakeHistoryScorer(feature_cols, seq_len=24)
    today = pd.Timestamp("2026-05-29")

    broad = J._build_live_panel_history(
        _make_ctx(ohlcv, watchlist, today), scorer, candidates, today)
    narrow = J._build_live_panel_history(
        _make_ctx(ohlcv, watchlist, today, context_mode="candidates"),
        scorer, candidates, today)

    broad_ranked = _csrank_norm_per_day(broad.copy(), scorer.feature_cols)
    narrow_ranked = _csrank_norm_per_day(narrow.copy(), scorer.feature_cols)

    last_date = broad["date"].max()

    def _rank_row(ranked, tkr):
        g = ranked[(ranked["ticker"] == tkr) & (ranked["date"] == last_date)]
        return g[scorer.feature_cols].to_numpy(dtype=float).ravel()

    broad_rank = _rank_row(broad_ranked, "T03")
    narrow_rank = _rank_row(narrow_ranked, "T03")

    # In the narrow (2-name) panel every per-day rank-pct is one of just two
    # values (0.5 or 1.0, recentred to {0, 0.5}) — a tiny, degenerate set.
    # In the broad (20-name) panel ranks spread across the cross-section.
    assert np.unique(np.round(narrow_rank, 6)).size <= 2
    assert np.unique(np.round(broad_rank, 6)).size > 2
