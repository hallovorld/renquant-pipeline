"""E3 breadth + E4 short-sleeve drivers (IC→Sharpe RFC §5)."""
from __future__ import annotations

import numpy as np
import pytest

from renquant_pipeline.kernel.portfolio_qp.allocator_replay import AllocatorReplayBar
from renquant_pipeline.kernel.portfolio_qp.constraint_snapshot import ConstraintSnapshot
from renquant_pipeline.kernel.portfolio_qp.e3_e4_breadth_shortsleeve import (
    participation_ratio,
    run_e3,
    run_e4,
    _z_to_130_30,
)


def _snap(n: int):
    return ConstraintSnapshot(
        n=n,
        tickers=tuple(f"T{i:02d}" for i in range(n)),
        w_current=np.zeros(n),
        w_upper_hard=np.full(n, 0.20),
        w_upper=np.full(n, 0.20),
        w_lower=-0.20,
        dw_max=np.full(n, 1.0),
        cash_reserve=0.0,
        turnover_max=None,
        drawdown=0.0,
        drawdown_limit=0.2,
        gross_max=None,
        wash_sale_mask=np.zeros(n, dtype=bool),
    )


def _bars(n_bars: int = 30, n: int = 20, seed: int = 5):
    rng = np.random.default_rng(seed)
    return [
        AllocatorReplayBar(
            bar_date=f"b{d:03d}", snap=_snap(n),
            mu=rng.normal(0.0, 0.03, n), sigma=np.full(n, 0.2),
            fwd_return=rng.normal(0.0003, 0.01, n), regime="BULL_CALM",
        )
        for d in range(n_bars)
    ]


def test_participation_ratio_counts_effective_bets():
    # 5 equal-weight names → PR = 5
    w = np.array([0.2, 0.2, 0.2, 0.2, 0.2])
    assert participation_ratio(w) == pytest.approx(5.0)
    # one dominant name → PR → ~1
    w2 = np.array([0.96, 0.01, 0.01, 0.01, 0.01])
    assert participation_ratio(w2) < 1.2
    assert participation_ratio(np.zeros(5)) is None


def test_e3_floor_reduces_effective_breadth():
    rows = run_e3(_bars(), floor_quantile=0.55)
    no_floor = next(r for r in rows if r["allocator"] == "A2_no_floor")
    with_floor = next(r for r in rows if r["allocator"] == "A2_with_floor")
    # the admission floor must hold strictly fewer effective bets
    assert (with_floor["effective_breadth_participation"]
            < no_floor["effective_breadth_participation"])
    assert with_floor["floor_quantile"] == 0.55
    assert no_floor["floor_quantile"] is None


def test_e3_rows_are_long_only():
    rows = run_e3(_bars())
    for r in rows:
        # long-only books have net == gross exposure (no shorts)
        assert r["mean_net_exposure"] == pytest.approx(r["mean_gross_exposure"], abs=1e-9)
        assert r["mean_borrow_drag_daily"] == 0.0


def test_130_30_book_shape():
    snap = _snap(20)
    mu = np.linspace(-0.05, 0.05, 20)
    res = _z_to_130_30(snap, mu)
    w = res.target_w
    assert float(w[w > 0].sum()) == pytest.approx(1.30, abs=1e-9)   # long 130%
    assert float(np.abs(w[w < 0]).sum()) == pytest.approx(0.30, abs=1e-9)  # short 30%
    assert float(w.sum()) == pytest.approx(1.00, abs=1e-9)          # net 100%


def test_e4_borrow_only_charged_on_short_books():
    rows = run_e4(_bars(), borrow_bps_annual=300.0)
    by = {r["book"]: r for r in rows}
    assert by["long_only"]["mean_borrow_drag_daily"] == 0.0
    assert by["dollar_neutral_ls"]["mean_borrow_drag_daily"] > 0.0
    assert by["130_30"]["mean_borrow_drag_daily"] > 0.0
    # long-only net exposure ~ gross; dollar-neutral net ~ 0
    assert abs(by["dollar_neutral_ls"]["mean_net_exposure"]) < 1e-6
    assert by["130_30"]["mean_net_exposure"] == pytest.approx(1.0, abs=1e-6)


def test_e4_higher_borrow_lowers_short_book_return():
    cheap = run_e4(_bars(), borrow_bps_annual=50.0)
    dear = run_e4(_bars(), borrow_bps_annual=500.0)
    cheap_dn = next(r for r in cheap if r["book"] == "dollar_neutral_ls")
    dear_dn = next(r for r in dear if r["book"] == "dollar_neutral_ls")
    assert dear_dn["cumulative_return"] < cheap_dn["cumulative_return"]
    # long-only book is identical regardless of borrow assumption
    cheap_lo = next(r for r in cheap if r["book"] == "long_only")
    dear_lo = next(r for r in dear if r["book"] == "long_only")
    assert cheap_lo["cumulative_return"] == pytest.approx(dear_lo["cumulative_return"])
