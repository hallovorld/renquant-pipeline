"""Stage-A allocators + TC metric (IC→Sharpe RFC, orchestrator PR #65).

Pins:
1. A0 decile L/S: dollar-neutral, equal-weight legs, gross as configured,
   longs are the top-μ̂ names and shorts the bottom.
2. A1 α-proportional: dollar-neutral, monotone in μ̂, gross normalised.
3. A2 long-only tilt: w ≥ 0, Σw ≤ budget, per-name hard caps respected,
   zero weight at/below the cross-sectional mean signal.
4. transfer_coefficient: +1 on identical books, −1 on reversed, None on
   degenerate inputs; matches scipy.stats.spearmanr where available.
5. The TC-instrumented replay wrapper preserves the harness metrics and
   produces a per-bar TC series with A1 ≈ 1.0 against itself.
6. NaN-μ̂ names never receive weight.
"""
from __future__ import annotations

import numpy as np
import pytest

from renquant_pipeline.kernel.portfolio_qp.allocator_replay import AllocatorReplayBar
from renquant_pipeline.kernel.portfolio_qp.alpha_portfolio import (
    MEASUREMENT_PREFIX,
    alpha_proportional_long_short,
    alpha_tilt_long_only,
    cross_sectional_zscore,
    decile_long_short,
    replay_one_allocator_with_tc,
    stage_a_allocators,
    transfer_coefficient,
)
from renquant_pipeline.kernel.portfolio_qp.constraint_snapshot import ConstraintSnapshot


def _snap(n: int, *, w_current=None, cap: float = 0.20, cash_reserve: float = 0.0):
    return ConstraintSnapshot(
        n=n,
        tickers=tuple(f"T{i:02d}" for i in range(n)),
        w_current=np.zeros(n) if w_current is None else np.asarray(w_current, float),
        w_upper_hard=np.full(n, cap),
        w_upper=np.full(n, cap),
        w_lower=0.0,
        dw_max=np.full(n, 1.0),
        cash_reserve=cash_reserve,
        turnover_max=None,
        drawdown=0.0,
        drawdown_limit=0.2,
        gross_max=None,
        wash_sale_mask=np.zeros(n, dtype=bool),
    )


MU20 = np.linspace(-0.05, 0.05, 20)  # strictly increasing μ̂


def test_a0_decile_long_short_shape():
    snap = _snap(20)
    res = decile_long_short(snap, mu=MU20, fraction=0.10, gross=1.0)
    assert res.status == "optimal"
    t = res.target_w
    assert t.sum() == pytest.approx(0.0, abs=1e-12)          # dollar-neutral
    assert np.abs(t).sum() == pytest.approx(1.0, abs=1e-12)  # gross
    assert t[19] == pytest.approx(0.25) and t[18] == pytest.approx(0.25)
    assert t[0] == pytest.approx(-0.25) and t[1] == pytest.approx(-0.25)
    assert np.count_nonzero(t) == 4


def test_a1_alpha_proportional_monotone_dollar_neutral():
    snap = _snap(20)
    res = alpha_proportional_long_short(snap, mu=MU20, gross=1.0)
    t = res.target_w
    assert res.status == "optimal"
    assert t.sum() == pytest.approx(0.0, abs=1e-12)
    assert np.abs(t).sum() == pytest.approx(1.0, abs=1e-12)
    assert (np.diff(t) > -1e-15).all()          # monotone in μ̂
    assert t[-1] > 0 > t[0]


def test_a2_long_only_budget_and_caps():
    snap = _snap(20, cap=0.08, cash_reserve=0.10)
    res = alpha_tilt_long_only(snap, mu=MU20)
    t = res.target_w
    assert res.status == "optimal"
    assert (t >= 0).all()
    assert t.sum() <= 0.90 + 1e-9               # budget
    assert (t <= 0.08 + 1e-12).all()            # hard caps
    z = cross_sectional_zscore(MU20)
    assert (t[z <= 0] == 0).all()               # no weight at/below mean signal
    # monotone within the un-clipped region
    pos = t[t > 0]
    assert (np.diff(pos) >= -1e-15).all()


def test_nan_mu_names_get_no_weight():
    mu = MU20.copy()
    mu[[3, 7]] = np.nan
    snap = _snap(20)
    for fn in (decile_long_short, alpha_proportional_long_short, alpha_tilt_long_only):
        res = fn(snap, mu=mu)
        assert res.target_w[3] == 0.0 and res.target_w[7] == 0.0


def test_transfer_coefficient_extremes_and_degenerate():
    w = np.array([0.1, 0.05, 0.0, -0.05, -0.1])
    assert transfer_coefficient(w, w) == pytest.approx(1.0)
    assert transfer_coefficient(w, -w) == pytest.approx(-1.0)
    assert transfer_coefficient(np.zeros(5), w) is None      # all-cash
    assert transfer_coefficient(w[:2], w[:2]) is None        # too short
    scipy = pytest.importorskip("scipy.stats")
    a = np.array([0.3, -0.1, 0.05, 0.2, -0.4, 0.0, 0.07])
    b = np.array([0.1, 0.2, -0.3, 0.4, -0.2, 0.05, -0.01])
    assert transfer_coefficient(a, b) == pytest.approx(
        float(scipy.spearmanr(a, b).statistic), abs=1e-12,
    )


def test_tc_replay_wrapper_preserves_metrics_and_records_tc():
    rng = np.random.default_rng(7)
    bars = []
    for d in range(10):
        mu = rng.normal(0.0, 0.03, 20)
        bars.append(AllocatorReplayBar(
            bar_date=f"2026-01-{d+1:02d}",
            snap=_snap(20),
            mu=mu,
            sigma=np.full(20, 0.2),
            fwd_return=rng.normal(0.0, 0.01, 20),
            regime="BULL_CALM",
        ))
    out = replay_one_allocator_with_tc(
        f"{MEASUREMENT_PREFIX}A1_alpha_prop_ls",
        alpha_proportional_long_short,
        bars,
    )
    assert out.replay.bars == 10
    assert len(out.replay.daily_returns_net) == 10
    assert len(out.tc_per_bar) == 10
    # A1 measured against the A1 signal book: TC ≡ 1
    assert out.tc_mean == pytest.approx(1.0, abs=1e-9)
    # A2 against the signal book: positive but < 1 (short leg discarded)
    out2 = replay_one_allocator_with_tc(
        "A2_alpha_tilt_long_only", alpha_tilt_long_only, bars,
    )
    assert out2.tc_mean is not None and 0.0 < out2.tc_mean < 1.0


def test_stage_a_registry_naming():
    reg = stage_a_allocators()
    assert set(reg) == {
        f"{MEASUREMENT_PREFIX}A0_decile_ls",
        f"{MEASUREMENT_PREFIX}A1_alpha_prop_ls",
        "A2_alpha_tilt_long_only",
    }
    # measurement instruments are the ONLY ones allowed short legs
    snap = _snap(20)
    a0 = reg[f"{MEASUREMENT_PREFIX}A0_decile_ls"](snap, mu=MU20)
    a2 = reg["A2_alpha_tilt_long_only"](snap, mu=MU20)
    assert (a0.target_w < 0).any()
    assert (a2.target_w >= 0).all()
