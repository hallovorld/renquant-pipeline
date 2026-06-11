"""P2 (2026-06-11 false-BEAR audit): `adaptive_quantile` buy-floor mode.

mean+kσ on a Platt-compressed calibrator is shape-unstable and structurally
caps breadth (~16%) regardless of edge. The quantile mode admits a deliberate
top fraction of the cross-section, stable under score compression, with
buy_floor_min kept as the absolute fail-safe.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import VetoWeakBuysTask


def _ctx(scores, raw_floor="adaptive_quantile", **panel_cfg):
    cands = [SimpleNamespace(ticker=f"T{i:03d}", rank_score=s)
             for i, s in enumerate(scores)]
    cfg = {"buy_floor": raw_floor, **panel_cfg}
    return SimpleNamespace(
        candidates=cands,
        config={"ranking": {"panel_scoring": cfg}},
        counters={},
    )


def test_quantile_mode_admits_top_fraction_under_compression() -> None:
    """A compressed cross-section (like the 2026-06-11 IQR=0.039 one) must
    still admit ~ the configured top fraction — not be shape-starved."""
    rng = np.random.default_rng(7)
    scores = list(0.52 + 0.02 * rng.standard_normal(83))  # tight cluster
    ctx = _ctx(scores, buy_floor_quantile=0.80, buy_floor_min=0.20)
    VetoWeakBuysTask().run(ctx)
    kept = len(ctx.candidates)
    assert 12 <= kept <= 22, f"top-20% of 83 should keep ~17, kept {kept}"


def test_quantile_mode_keeps_same_fraction_regardless_of_scale() -> None:
    """Breadth is the control variable: wide and narrow distributions admit
    the same fraction (unlike mean+kσ, which drifts with shape)."""
    rng = np.random.default_rng(11)
    wide   = list(0.50 + 0.20 * rng.standard_normal(100))
    narrow = list(0.50 + 0.01 * rng.standard_normal(100))
    kept = []
    for scores in (wide, narrow):
        ctx = _ctx(scores, buy_floor_quantile=0.75, buy_floor_min=0.0)
        VetoWeakBuysTask().run(ctx)
        kept.append(len(ctx.candidates))
    assert abs(kept[0] - kept[1]) <= 3, f"fractions diverged: {kept}"


def test_min_floor_failsafe_still_applies() -> None:
    """A degenerate cross-section clustered below buy_floor_min admits nothing
    — the absolute fail-safe outranks the quantile."""
    scores = [0.05, 0.06, 0.07, 0.08, 0.09]
    ctx = _ctx(scores, buy_floor_quantile=0.50, buy_floor_min=0.20)
    VetoWeakBuysTask().run(ctx)
    assert len(ctx.candidates) == 0


def test_nan_scores_still_dropped_and_none_kept() -> None:
    """NaN handling is mode-independent: NaN drops, None passes through."""
    scores = [0.6, 0.7, 0.8, 0.9, 1.0]
    ctx = _ctx(scores, buy_floor_quantile=0.40, buy_floor_min=0.0)
    ctx.candidates.append(SimpleNamespace(ticker="NAN1", rank_score=float("nan")))
    ctx.candidates.append(SimpleNamespace(ticker="NONE1", rank_score=None))
    VetoWeakBuysTask().run(ctx)
    tickers = {c.ticker for c in ctx.candidates}
    assert "NAN1" not in tickers
    assert "NONE1" in tickers   # unscored kept (rs_score ranks it downstream)


def test_legacy_adaptive_mean_std_unchanged() -> None:
    """Back-compat: the existing prod mode still applies mean+1σ."""
    scores = [0.50, 0.52, 0.54, 0.56, 0.58, 0.60]
    ctx = _ctx(scores, raw_floor="adaptive_mean_std", buy_floor_min=0.20)
    VetoWeakBuysTask().run(ctx)
    # mean=0.55 std≈0.0374 → floor≈0.587 → only 0.58? no: 0.58<0.587 → only 0.60
    assert {c.rank_score for c in ctx.candidates} == {0.60}


def test_two_candidate_fallback_uses_min_floor() -> None:
    ctx = _ctx([0.5], buy_floor_quantile=0.8, buy_floor_min=0.2)
    VetoWeakBuysTask().run(ctx)
    assert len(ctx.candidates) == 1   # n<2 → floor=min_fl=0.2; 0.5 passes
