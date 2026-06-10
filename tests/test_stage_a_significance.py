"""Stage-A significance hardening (IC→Sharpe RFC §7.3)."""
from __future__ import annotations

import numpy as np

from renquant_pipeline.kernel.portfolio_qp.allocator_replay import AllocatorReplayBar
from renquant_pipeline.kernel.portfolio_qp.constraint_snapshot import ConstraintSnapshot
from renquant_pipeline.kernel.portfolio_qp.stage_a_significance import (
    INCUMBENT,
    build_candidate_results,
    run_significance,
)


def _snap(n: int):
    return ConstraintSnapshot(
        n=n, tickers=tuple(f"T{i:02d}" for i in range(n)),
        w_current=np.zeros(n), w_upper_hard=np.full(n, 0.2),
        w_upper=np.full(n, 0.2), w_lower=0.0, dw_max=np.full(n, 1.0),
        cash_reserve=0.0, turnover_max=None, drawdown=0.0,
        drawdown_limit=0.2, gross_max=None, wash_sale_mask=np.zeros(n, dtype=bool),
    )


def _bars(n_bars=60, n=25, seed=2):
    rng = np.random.default_rng(seed)
    return [
        AllocatorReplayBar(
            bar_date=f"b{d:03d}", snap=_snap(n), mu=rng.normal(0, 0.03, n),
            sigma=np.full(n, 0.2), fwd_return=rng.normal(0.0003, 0.01, n),
            regime="BULL_CALM" if d % 2 else "BEAR",
        )
        for d in range(n_bars)
    ]


def test_candidate_set_shares_bar_count():
    results = build_candidate_results(_bars(), a2_hold_bars=3)
    assert "A2_long_only_hold3" in results
    assert INCUMBENT in results
    counts = {r.bars for r in results.values()}
    assert len(counts) == 1  # shared bar count → paired/PBO well-defined


def test_run_significance_has_all_blocks():
    out = run_significance(_bars(), a2_hold_bars=3, pbo_n_slices=8)
    assert out["incumbent"] == INCUMBENT
    assert out["promotion_decision_grade"] is False
    assert out["pbo_n_slices"] == 8
    assert set(out) >= {
        "per_allocator", "paired_vs_incumbent", "significance_dsr_pbo",
        "per_regime", "caveats",
    }
    # paired comparisons are keyed vs the incumbent
    assert any(k.startswith(f"{INCUMBENT}_vs_") for k in out["paired_vs_incumbent"])
    # DSR present per allocator (>=30 bars), PBO shared across the matrix
    sig = out["significance_dsr_pbo"]
    assert "A2_long_only_hold3" in sig
    pbo_values = {v.get("pbo") for v in sig.values()}
    assert len(pbo_values) == 1  # one shared PBO number
    assert all(v["diagnostic_only"] is True for v in sig.values())
    assert all(v["live_promotable_per_section_8"] is False for v in sig.values())


def test_paired_block_reports_hac_and_delta_sharpe():
    out = run_significance(_bars(), a2_hold_bars=3, pbo_n_slices=8)
    key = f"{INCUMBENT}_vs_A2_long_only_hold3"
    pc = out["paired_vs_incumbent"][key]
    assert "delta_sharpe_annual" in pc
    assert "hac_t_stat" in pc  # may be None if renquant_common.metrics absent
    assert "win_rate_a_beats_b_z_score" in pc


def test_per_regime_block_present():
    out = run_significance(_bars(), a2_hold_bars=3, pbo_n_slices=8)
    # both regimes appear (PRIME DIRECTIVE: by-regime first)
    assert set(out["per_regime"]) & {"BULL_CALM", "BEAR"}
    assert out["per_regime_available"] is True


def test_missing_regimes_are_explicitly_marked_unavailable():
    bars = [
        AllocatorReplayBar(
            bar_date=b.bar_date, snap=b.snap, mu=b.mu, sigma=b.sigma,
            fwd_return=b.fwd_return, regime=None,
        )
        for b in _bars()
    ]
    out = run_significance(bars, a2_hold_bars=3, pbo_n_slices=8)
    assert out["per_regime"] == {}
    assert out["per_regime_available"] is False
    assert "do not carry regime labels" in out["per_regime_unavailable_reason"]
