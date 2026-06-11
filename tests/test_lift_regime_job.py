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
task_regime = importlib.import_module("renquant_pipeline.kernel.pipeline.task_regime")


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


# ── 2026-06-11 false-BEAR audit (P0): BEAROverrideTask short-route guards ──────
#
# The 5-day route was OR(vol, ret): a lone vol spike with no real drop flipped
# the whole book to hard_bear on routine pullback chop. These tests pin the two
# config-gated guards (confirmed-vol + trend filter) and prove the 20-day GFC
# and acute 5-day return-loss routes stay unconditional.

# Reproduces 2026-06-11: elevated 5d vol (~0.30 > 0.25) but only a ~-0.5%
# cumulative move (NOT < -4%), sitting on a calm prior window so the 20d route
# is quiet.
_VOL_SPIKE_5D = [0.02, -0.02, 0.018, -0.018, -0.004]


def _run_bear(rets, regime_cfg=None):
    ctx = SimpleNamespace(
        spy_returns=np.asarray(rets, dtype=float),
        regime_state=regime.RegimeState(),
        config={"regime": regime_cfg or {}, "regime_params": {}},
    )
    task_regime.BEAROverrideTask().run(ctx)
    return ctx.regime_state


def test_5d_vol_spike_alone_triggers_under_legacy_or_default() -> None:
    """Backward-compat: with no config, the legacy OR still fires on vol alone."""
    rets = np.concatenate([np.full(40, 0.0003), _VOL_SPIKE_5D])
    st = _run_bear(rets, {})
    assert st.vol_5d > 0.25 and st.ret_5d > -0.04   # vol breached, ret did not
    assert st.hard_bear is True                      # legacy OR ⇒ fires


def test_require_both_suppresses_vol_only_false_bear() -> None:
    """P0: confirmed-vol demands vol AND a real drop — the 2026-06-11 case is
    no longer a false BEAR (vol breached but the -0.5% move did not)."""
    rets = np.concatenate([np.full(40, 0.0003), _VOL_SPIKE_5D])
    st = _run_bear(rets, {"bear_short_route_require_both": True})
    assert st.vol_5d > 0.25 and st.ret_5d > -0.04
    assert st.hard_bear is False


def test_require_both_still_fires_on_genuine_both_breach() -> None:
    """A real sharp week (high vol AND < -4% drop) still trips the 5d route."""
    crash5 = [-0.04, 0.01, -0.05, -0.01, -0.03]            # vol high, cumret ~-11%
    rets = np.concatenate([np.full(40, 0.0003), crash5])
    st = _run_bear(rets, {"bear_short_route_require_both": True})
    assert st.vol_5d > 0.25 and st.ret_5d < -0.04
    assert st.hard_bear is True


def test_require_both_does_not_disable_acute_return_loss_route() -> None:
    """A 5-day return shock remains fail-safe even when vol confirmation is on."""
    ret_shock = [-0.01, -0.01, -0.01, -0.01, -0.01]       # cumret ~-4.9%, low vol
    rets = np.concatenate([np.full(240, 0.0015), ret_shock])
    st = _run_bear(rets, {"bear_short_route_require_both": True})
    assert st.vol_5d < 0.25 and st.ret_5d < -0.04
    assert st.hard_bear is True


def test_20d_gfc_route_stays_unconditional_under_both_guards() -> None:
    """A sustained crash must still label BEAR even with both 5d guards on —
    the 20-day GFC routes are never gated."""
    rets = np.concatenate([np.full(220, 0.0003), np.full(30, -0.015)])
    st = _run_bear(rets, {"bear_short_route_require_both": True,
                          "bear_trend_filter": {"enabled": True, "ma_window": 200}})
    assert st.hard_bear is True


def test_trend_filter_suppresses_5d_route_above_ma() -> None:
    """A vol spike in a confirmed uptrend (price > 200d MA) is suppressed."""
    rets = np.concatenate([np.full(240, 0.0015), _VOL_SPIKE_5D])  # strong uptrend
    st = _run_bear(rets, {"bear_trend_filter": {"enabled": True, "ma_window": 200}})
    assert st.vol_5d > 0.25
    assert st.hard_bear is False


def test_trend_filter_does_not_suppress_acute_return_loss_above_ma() -> None:
    """Trend confirmation gates vol-only false positives, not 5d return shocks."""
    ret_shock = [-0.01, -0.01, -0.01, -0.01, -0.01]       # cumret ~-4.9%, low vol
    rets = np.concatenate([np.full(240, 0.0015), ret_shock])
    st = _run_bear(rets, {"bear_trend_filter": {"enabled": True, "ma_window": 200}})
    assert st.vol_5d < 0.25 and st.ret_5d < -0.04
    assert st.hard_bear is True


def test_trend_filter_does_not_suppress_5d_route_below_ma() -> None:
    """The same vol spike in a downtrend (price < 200d MA) is NOT suppressed."""
    rets = np.concatenate([np.full(240, -0.001), _VOL_SPIKE_5D])  # downtrend
    st = _run_bear(rets, {"bear_trend_filter": {"enabled": True, "ma_window": 200}})
    assert st.vol_5d > 0.25
    assert st.hard_bear is True
