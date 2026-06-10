"""E1 TC-decomposition ladder (IC→Sharpe RFC §5/E1).

Pins:
1. The ladder runs all 7 steps over synthetic bars and produces one
   E1StepResult per step with harness-identical metric arithmetic.
2. Step 0 (zero-cost A0) Sharpe ≥ step 1 (costed A0) on the same bars —
   costs can only hurt a fixed book.
3. The overlay is a uniform positive scalar: same-date TC of step 3
   equals step 2's wherever both are defined (H-B by construction on
   stateless dates).
4. The admission floor zeroes below-quantile names; the stop wrapper
   flattens a name after a prior-bar breach and releases it after
   reentry_bars.
5. write_results emits manifest + tidy CSV + per-step traces with
   sha256s, and the CSV carries basis='replay_net_of_cost'.
"""
from __future__ import annotations

import csv
import json

import numpy as np
import pytest

from renquant_pipeline.kernel.portfolio_qp.allocator_replay import AllocatorReplayBar
from renquant_pipeline.kernel.portfolio_qp.alpha_portfolio import alpha_tilt_long_only
from renquant_pipeline.kernel.portfolio_qp.constraint_snapshot import ConstraintSnapshot
from renquant_pipeline.kernel.portfolio_qp.e1_tc_decomposition import (
    AdmissionFloorWrapper,
    SingleDayLossStopWrapper,
    VolTargetDrawdownOverlay,
    run_e1,
    write_results,
)


def _snap(n: int):
    return ConstraintSnapshot(
        n=n,
        tickers=tuple(f"T{i:02d}" for i in range(n)),
        w_current=np.zeros(n),
        w_upper_hard=np.full(n, 0.20),
        w_upper=np.full(n, 0.20),
        w_lower=0.0,
        dw_max=np.full(n, 1.0),
        cash_reserve=0.0,
        turnover_max=None,
        drawdown=0.0,
        drawdown_limit=0.2,
        gross_max=None,
        wash_sale_mask=np.zeros(n, dtype=bool),
    )


def _bars(n_bars: int = 30, n: int = 20, seed: int = 11):
    rng = np.random.default_rng(seed)
    bars = []
    for d in range(n_bars):
        bars.append(AllocatorReplayBar(
            bar_date=f"2026-02-{(d % 28) + 1:02d}",
            snap=_snap(n),
            mu=rng.normal(0.0, 0.03, n),
            sigma=np.full(n, 0.2),
            fwd_return=rng.normal(0.0005, 0.01, n),
            regime="BULL_CALM",
            cost_per_trade_bps=5.0,
        ))
    return bars


def test_ladder_runs_all_steps():
    results = run_e1(_bars(), windows_label="synthetic")
    assert [r.step for r in results] == [0, 1, 2, 3, 4, 5, 6]
    for r in results:
        assert r.replay.bars == 30
        assert len(r.tc_per_bar) == 30


def test_zero_cost_dominates_costed_a0():
    results = run_e1(_bars(), windows_label="synthetic")
    s0, s1 = results[0], results[1]
    # identical book, only costs differ → cumulative return strictly ordered
    assert s0.replay.cumulative_return > s1.replay.cumulative_return
    # and the per-bar TC series are identical (same decisions)
    assert s0.tc_per_bar == s1.tc_per_bar


def test_overlay_is_uniform_scalar_tc_invariant():
    bars = _bars()
    results = run_e1(bars, windows_label="synthetic")
    a2, overlaid = results[2], results[3]
    for t_base, t_over in zip(a2.tc_per_bar, overlaid.tc_per_bar):
        if t_base is not None and t_over is not None:
            assert t_over == pytest.approx(t_base, abs=1e-9)


def test_admission_floor_zeroes_below_quantile():
    snap = _snap(20)
    mu = np.linspace(-0.05, 0.05, 20)
    gated = AdmissionFloorWrapper(alpha_tilt_long_only, floor_quantile=0.55)
    res = gated(snap, mu=mu)
    base = alpha_tilt_long_only(snap, mu=mu)
    # floor keeps strictly fewer names than the base tilt
    assert np.count_nonzero(res.target_w) < np.count_nonzero(base.target_w)
    # the lowest-μ̂ names never get weight under the floor
    assert (res.target_w[:10] == 0).all()


def test_stop_wrapper_flattens_after_breach_and_releases():
    snap = _snap(5)
    mu = np.array([0.05, 0.04, 0.03, 0.02, 0.01])
    stop = SingleDayLossStopWrapper(
        alpha_tilt_long_only, max_single_day_loss=0.03, reentry_bars=2,
    )
    bar = AllocatorReplayBar(
        bar_date="2026-02-01", snap=snap, mu=mu,
        sigma=np.full(5, 0.2),
        fwd_return=np.array([-0.05, 0.0, 0.0, 0.0, 0.0]),  # name 0 breaches
    )
    before = stop(snap, mu=mu)
    assert before.target_w[0] > 0
    stop.observe(bar, 0.0)
    blocked = stop(snap, mu=mu)
    assert blocked.target_w[0] == 0.0
    # release after reentry_bars observes without breach
    calm = AllocatorReplayBar(
        bar_date="2026-02-02", snap=snap, mu=mu,
        sigma=np.full(5, 0.2), fwd_return=np.zeros(5),
    )
    stop.observe(calm, 0.0)
    stop.observe(calm, 0.0)
    released = stop(snap, mu=mu)
    assert released.target_w[0] > 0


def test_overlay_throttles_after_drawdown():
    ov = VolTargetDrawdownOverlay(alpha_tilt_long_only, dd_max=0.2, dd_floor=0.1)
    assert ov.current_scale() == pytest.approx(1.0)
    for _ in range(10):
        ov.observe(None, -0.03)  # bar unused by observe
    assert ov.current_scale() < 1.0


def test_write_results_manifest_and_csv(tmp_path):
    results = run_e1(_bars(n_bars=10), windows_label="synthetic")
    paths = write_results(
        tmp_path, results,
        windows_label="synthetic",
        params={"floor_quantile": 0.55},
        input_descriptor={"source": "synthetic", "n_bars": 10},
        repo_pins={"renquant-pipeline": "deadbeef"},
    )
    manifest = json.loads(paths["manifest"].read_text())
    assert manifest["experiment"] == "E1"
    assert manifest["basis"] == "replay_net_of_cost"
    assert manifest["repo_pins"]["renquant-pipeline"] == "deadbeef"
    # every trace/csv output is fingerprinted (manifest itself excluded —
    # it is written last and cannot contain its own hash)
    files = {p.name for p in paths["run_dir"].iterdir()} - {"manifest.json"}
    assert set(manifest["outputs"]) == files
    assert all(v.startswith("sha256:") for v in manifest["outputs"].values())
    rows = list(csv.DictReader(paths["csv"].open()))
    assert len(rows) == 7
    assert all(r["basis"] == "replay_net_of_cost" for r in rows)
    assert rows[0]["is_measurement"] == "True" and rows[2]["is_measurement"] == "False"
