"""E2 horizon-held wrapper + sweep (IC→Sharpe RFC §5/E2).

Pins:
1. HorizonHeldWrapper re-solves only every hold_bars bars; between
   rebalances the held target is returned unchanged.
2. Turnover decreases monotonically as the holding horizon grows on a
   fixed bar sequence (longer holds = fewer re-solves).
3. hold_bars=1 reproduces the unwrapped allocator exactly (same targets
   every bar).
4. Universe-size change mid-hold triggers a safe re-solve instead of an
   index misalignment.
5. run_e2 emits one result per horizon with step = horizon.
"""
from __future__ import annotations

import numpy as np
import pytest

from renquant_pipeline.kernel.portfolio_qp.allocator_replay import AllocatorReplayBar
from renquant_pipeline.kernel.portfolio_qp.alpha_portfolio import (
    alpha_tilt_long_only,
    decile_long_short,
)
from renquant_pipeline.kernel.portfolio_qp.constraint_snapshot import ConstraintSnapshot
from renquant_pipeline.kernel.portfolio_qp.e2_horizon_sweep import (
    HorizonHeldWrapper,
    run_e2,
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


def _bars(n_bars: int = 40, n: int = 20, seed: int = 3):
    rng = np.random.default_rng(seed)
    return [
        AllocatorReplayBar(
            bar_date=f"bar{d:03d}",
            snap=_snap(n),
            mu=rng.normal(0.0, 0.03, n),
            sigma=np.full(n, 0.2),
            fwd_return=rng.normal(0.0, 0.01, n),
            regime="BULL_CALM",
        )
        for d in range(n_bars)
    ]


def test_holds_book_between_rebalances():
    bars = _bars(n_bars=6)
    w = HorizonHeldWrapper(decile_long_short, hold_bars=3)
    targets = []
    for bar in bars:
        res = w(bar.snap, mu=bar.mu, sigma=bar.sigma)
        targets.append(res.target_w.copy())
        w.observe(bar, 0.0)
    # bars 0-2 share the bar-0 book; bars 3-5 share the bar-3 book
    assert np.array_equal(targets[0], targets[1])
    assert np.array_equal(targets[0], targets[2])
    assert np.array_equal(targets[3], targets[4])
    assert not np.array_equal(targets[0], targets[3])


def test_hold_one_equals_unwrapped():
    bars = _bars(n_bars=8)
    wrapped = HorizonHeldWrapper(alpha_tilt_long_only, hold_bars=1)
    for bar in bars:
        a = wrapped(bar.snap, mu=bar.mu, sigma=bar.sigma)
        b = alpha_tilt_long_only(bar.snap, mu=bar.mu, sigma=bar.sigma)
        assert np.allclose(a.target_w, b.target_w)
        wrapped.observe(bar, 0.0)


def test_turnover_monotone_in_horizon():
    bars = _bars(n_bars=40)
    results = run_e2(bars, horizons=(1, 5, 20))
    turnovers = [r.replay.mean_turnover for r in results]
    assert turnovers[0] > turnovers[1] > turnovers[2]


def test_universe_change_projects_by_ticker_not_index():
    w = HorizonHeldWrapper(decile_long_short, hold_bars=10)
    b20 = _bars(n_bars=1, n=20)[0]
    res = w(b20.snap, mu=b20.mu, sigma=b20.sigma)
    assert len(res.target_w) == 20
    w.observe(b20, 0.0)
    # next bar: 30 names, T00..T29 — held T-names keep their weights,
    # new T20..T29 are 0 (held book projected by ticker, not re-solved)
    b30 = _bars(n_bars=1, n=30, seed=9)[0]
    res2 = w(b30.snap, mu=b30.mu, sigma=b30.sigma)
    assert len(res2.target_w) == 30
    held = {t: float(res.target_w[i]) for i, t in enumerate(b20.snap.tickers)}
    for i, t in enumerate(b30.snap.tickers):
        if t in held:
            assert res2.target_w[i] == pytest.approx(held[t])  # ticker-aligned
        else:
            assert res2.target_w[i] == 0.0                      # new name, unheld


def _changing_universe_bars(n_bars=40, base_n=24, seed=3):
    """Bars whose ticker membership rotates each day (same n, shifting names)."""
    rng = np.random.default_rng(seed)
    bars = []
    for d in range(n_bars):
        tickers = tuple(f"T{(d + i) % 60:02d}" for i in range(base_n))  # window slides
        snap = ConstraintSnapshot(
            n=base_n, tickers=tickers, w_current=np.zeros(base_n),
            w_upper_hard=np.full(base_n, 0.2), w_upper=np.full(base_n, 0.2),
            w_lower=0.0, dw_max=np.full(base_n, 1.0), cash_reserve=0.0,
            turnover_max=None, drawdown=0.0, drawdown_limit=0.2, gross_max=None,
            wash_sale_mask=np.zeros(base_n, dtype=bool),
        )
        bars.append(AllocatorReplayBar(
            bar_date=f"b{d:03d}", snap=snap, mu=rng.normal(0, 0.03, base_n),
            sigma=np.full(base_n, 0.2), fwd_return=rng.normal(0, 0.01, base_n),
            regime="BULL_CALM",
        ))
    return bars


def test_horizons_differ_under_changing_universe():
    """Regression for the 2026-06-10 bug: with a rotating universe the old
    index-keyed wrapper degenerated every horizon to the daily result
    (hold=5 ≡ hold=20 ≡ hold=40 byte-identical). By-ticker holding must
    make distinct horizons produce distinct return streams."""
    bars = _changing_universe_bars()
    results = run_e2(bars, horizons=(5, 20))
    r5 = np.asarray(results[0].replay.daily_returns_net)
    r20 = np.asarray(results[1].replay.daily_returns_net)
    assert not np.array_equal(r5, r20)


def test_run_e2_one_result_per_horizon():
    results = run_e2(_bars(), horizons=(20, 40))
    assert [r.step for r in results] == [20, 40]
    assert all(r.replay.bars == 40 for r in results)
    assert all(len(r.tc_per_bar) == 40 for r in results)


def test_invalid_hold_bars_rejected():
    with pytest.raises(ValueError):
        HorizonHeldWrapper(decile_long_short, hold_bars=0)
