"""BullCalmMomentumGuardTask: one-sided tail veto of bottom-momentum names in BULL_CALM.

Momentum = -ROC60 (qlib ROC = past/current → higher ROC = lower momentum). The
guard vetoes candidates below the `percentile`-th momentum percentile of the full
scored universe, only in BULL_CALM, OFF by default. Evidence: #187 (bottom decile
realized fwd_60d median -0.107).
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
    BullCalmMomentumGuardTask,
)


def _matrix():
    # 10 tickers; LOSER has the highest ROC60 → the LOWEST momentum (bottom tail).
    roc60 = {"A": -0.20, "B": -0.15, "C": -0.10, "D": -0.05, "E": 0.0,
             "F": 0.05, "G": 0.10, "H": 0.15, "I": 0.20, "LOSER": 0.60}
    return pd.DataFrame({"ROC60": roc60})


def _ctx(cands, regime="BULL_CALM", **guard):
    cfg = {"ranking": {"bull_calm_momentum_guard": guard}} if guard else {"ranking": {}}
    return SimpleNamespace(
        candidates=[SimpleNamespace(ticker=t) for t in cands],
        config=cfg, counters={}, regime=regime, _panel_matrix=_matrix(),
    )


def test_off_by_default_is_noop():
    ctx = _ctx(["C", "LOSER"])  # no guard config
    assert BullCalmMomentumGuardTask().run(ctx) is None
    assert {c.ticker for c in ctx.candidates} == {"C", "LOSER"}


def test_disabled_is_noop():
    ctx = _ctx(["C", "LOSER"], enabled=False, percentile=0.10)
    BullCalmMomentumGuardTask().run(ctx)
    assert {c.ticker for c in ctx.candidates} == {"C", "LOSER"}


def test_only_acts_in_bull_calm():
    ctx = _ctx(["C", "LOSER"], regime="BEAR", enabled=True, percentile=0.10)
    BullCalmMomentumGuardTask().run(ctx)
    assert {c.ticker for c in ctx.candidates} == {"C", "LOSER"}  # untouched in BEAR


def test_vetoes_bottom_momentum_in_bull_calm():
    ctx = _ctx(["C", "LOSER"], enabled=True, percentile=0.10)
    BullCalmMomentumGuardTask().run(ctx)
    assert {c.ticker for c in ctx.candidates} == {"C"}  # LOSER (lowest momentum) dropped
    assert ctx._blocked_by_ticker["LOSER"] == "bull_calm_momentum:below_pctile"
    assert ctx.counters["bull_calm_momentum_vetoed"] == 1


def test_warn_mode_flags_but_keeps():
    ctx = _ctx(["C", "LOSER"], enabled=True, percentile=0.10, enforce=False)
    BullCalmMomentumGuardTask().run(ctx)
    assert {c.ticker for c in ctx.candidates} == {"C", "LOSER"}  # nothing dropped
    assert ctx._blocked_by_ticker["LOSER"] == "bull_calm_momentum:flagged_warn"
    assert ctx.counters["bull_calm_momentum_vetoed"] == 0


def test_missing_feature_is_safe_noop():
    ctx = _ctx(["C", "LOSER"], enabled=True, percentile=0.10, feature="NOPE")
    BullCalmMomentumGuardTask().run(ctx)
    assert {c.ticker for c in ctx.candidates} == {"C", "LOSER"}
