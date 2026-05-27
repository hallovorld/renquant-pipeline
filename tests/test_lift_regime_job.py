"""Parity test for the regime Job lift (functional-lift slice 7).

First decision-tree Job lifted into the pipeline repo. Proves:

1. The Job + its Tasks import cleanly (the `from .context import` relative
   import resolves through the new re-export shim; the rewritten
   `renquant_pipeline.kernel.{regime,regime_hmm,config}` lazy imports resolve).
2. The full RegimeJob actually runs end-to-end on a synthetic SPY series and
   commits a valid regime label — i.e. the lifted decision unit executes, not
   just imports.

Fixture mirrors the umbrella's tests/test_regime_detector_5day_and_chop.py
(SimpleNamespace ctx carrying exactly the fields the regime tasks read/write).
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import numpy as np
import pandas as pd

job_regime = importlib.import_module("renquant_pipeline.kernel.pipeline.job_regime")
regime = importlib.import_module("renquant_pipeline.kernel.regime")
config = importlib.import_module("renquant_pipeline.kernel.config")


def _calm_bull_ctx(n: int = 250, seed: int = 0) -> SimpleNamespace:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0005, 0.008, n)  # low vol, positive drift
    closes = 100.0 * np.cumprod(1.0 + rets)
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    spy_df = pd.DataFrame({"close": closes}, index=idx)
    return SimpleNamespace(
        spy_returns=rets,
        regime_state=regime.RegimeState(),
        config={"regime": {}, "regime_params": {}},
        ohlcv={"SPY": spy_df},
        today=None,
        regime=None,
        confidence=0.0,
        regime_counts={},
        gmm=None,
        _regime_evidence={},
        spy_regime=None,
    )


def test_regime_job_imports() -> None:
    assert hasattr(job_regime, "RegimeJob")


def test_regime_job_runs_and_commits_valid_label() -> None:
    ctx = _calm_bull_ctx()
    job_regime.RegimeJob().run(ctx)
    valid = set(config.REGIMES)
    assert ctx.regime in valid, f"committed regime {ctx.regime!r} not in {valid}"
    assert isinstance(ctx.regime_state.hard_bear, bool)


def _crash_ctx(n: int = 250) -> SimpleNamespace:
    """Strong sustained downtrend: benign, then -1.5%/day for 30 days."""
    rets = np.concatenate([np.full(n - 30, 0.0003), np.full(30, -0.015)])
    closes = 100.0 * np.cumprod(1.0 + rets)
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    spy_df = pd.DataFrame({"close": closes}, index=idx)
    return SimpleNamespace(
        spy_returns=rets,
        regime_state=regime.RegimeState(),
        config={"regime": {}, "regime_params": {}},
        ohlcv={"SPY": spy_df},
        today=None,
        regime=None,
        confidence=0.0,
        regime_counts={},
        gmm=None,
        _regime_evidence={},
        spy_regime=None,
    )


def test_regime_job_detects_bear_on_sustained_crash() -> None:
    """A sustained -1.5%/day decline must trip the BEAR carve-out and label.

    Exercises BEAROverrideTask + RegimeFinalizeTask end-to-end through the
    lifted Job — the decision logic, not just the import wiring.
    """
    ctx = _crash_ctx()
    job_regime.RegimeJob().run(ctx)
    assert ctx.regime_state.hard_bear is True
    assert ctx.regime == config.BEAR


def test_regime_job_is_deterministic() -> None:
    """Lifted detector is a pure function of inputs — same ctx, same label."""
    r1, r2 = _calm_bull_ctx(), _calm_bull_ctx()
    job_regime.RegimeJob().run(r1)
    job_regime.RegimeJob().run(r2)
    assert r1.regime == r2.regime
