"""Regression coverage for the 2026-06-09 QP new-buy starvation deadlock.

Root cause (doc/2026-06-09-qp-new-buy-sizing-bug.md): the solver PLANNED
sells that the emission-stage soft-sell horizon guard then suppressed.
Live case: one over-cap holding (ORCL 19.4% vs 8.3% cap) forced an 11.1%
sell-down that consumed most of the 20% ``qp_turnover_max`` budget, so
every admitted new buy solved to ≈1.5% < 2% ``qp_min_dw_pct`` and was
skipped — insensitive to μ, γ (12×), and per-name caps, because the
binding constraint was the L1 turnover budget, not the mean-variance
tradeoff surface.

Two opt-in fixes, each pinned here:

1. ``turnover_exempt_forced_trims`` (solver kwarg, config
   ``qp_turnover_exempt_forced_trims``): the mandatory sell-down to the
   per-asset cap is a risk-constraint trade, not discretionary alpha
   trading — exempt exactly that component from the turnover budget.
2. ``no_sell_mask`` (ConstraintSnapshot field, produced by
   ``ApplySoftSellGuardMaskTask`` under ``qp_soft_sell_guard.align_solver``):
   within-hard-cap holdings whose sells emission would suppress are held
   flat (Δw ≥ 0 + soft cap raised to w_current) so their planned trims
   cannot spend turnover either. Over-cap holdings are NEVER masked
   (#123 cap-compliance contract).
"""
from __future__ import annotations

import numpy as np
import pytest

from renquant_pipeline.kernel.portfolio_qp.constraint_snapshot import (
    ConstraintSnapshot,
)
from renquant_pipeline.kernel.portfolio_qp.qp_solver import solve_portfolio_qp
from renquant_pipeline.kernel.portfolio_qp.tasks import ApplySoftSellGuardMaskTask


# ── 2026-06-09 live shape: 3 holdings (one over-cap) + 4 new names ──────────
# ORCL 19.4% (over the 8.3% cap), MU 8.8%, EQIX 10.0% → forced trims
# |Δw| = 11.1 + 0.5 + 1.7 = 13.3% of the 20% turnover budget; 4 buys share
# the remaining 6.7% ≈ 1.7% each < 2% min Δw. Matches the live logs.
N = 7
W_CURRENT = np.array([0.194, 0.088, 0.100, 0.0, 0.0, 0.0, 0.0])
MU        = np.array([0.005, 0.010, 0.010, 0.040, 0.040, 0.040, 0.040])
SIGMA     = np.full(N, 0.15)
W_UPPER   = np.full(N, 0.083)                        # 0.12 cap × 0.69 conf
TURNOVER  = 0.20
MIN_DW    = 0.02


def _solve(**overrides):
    kwargs = dict(
        w_current=W_CURRENT,
        mu=MU,
        sigma=SIGMA,
        risk_aversion=3.0,
        cost_kappa=0.0,
        w_upper=W_UPPER,
        w_lower=0.0,
        dw_max=0.50,
        turnover_max=TURNOVER,
        min_invested_pct=0.5,
        cash_drag_lambda=0.5,
    )
    kwargs.update(overrides)
    return solve_portfolio_qp(**kwargs)


def test_deadlock_reproduced_without_exemption():
    """Pin the bug: forced over-cap trim starves all new buys below 2%."""
    sol = _solve()
    assert sol.status.startswith("optimal"), sol.status
    new_buys = sol.target_w[3:]
    # forced trims (−13.3%) leave ≈6.7% of turnover for 4 buys
    assert float(np.max(new_buys)) < MIN_DW, (
        f"expected starved buys (<{MIN_DW}); got {new_buys}"
    )


def test_turnover_exemption_unblocks_new_buys():
    """The fix: exempting the forced trim lets buys reach real size."""
    sol = _solve(turnover_exempt_forced_trims=True)
    assert sol.status.startswith("optimal"), sol.status
    new_buys = sol.target_w[3:]
    assert float(np.min(new_buys)) >= MIN_DW, (
        f"buys still starved with exemption on: {new_buys}"
    )
    # over-cap holding still trimmed to its hard cap (cap contract intact)
    assert sol.target_w[0] == pytest.approx(0.083, abs=1e-6)


def test_no_sell_mask_blocks_planned_sells():
    """Δw ≥ 0 for masked names; the held name stays held."""
    w_current = np.array([0.083, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])  # within cap
    mask = np.zeros(N, dtype=bool)
    mask[0] = True
    mu = MU.copy()
    mu[0] = -0.05
    sol = _solve(w_current=w_current, mu=mu, no_sell_mask=mask)
    assert sol.status.startswith("optimal"), sol.status
    # μ is deeply negative → unmasked solver would sell; mask forbids it
    assert sol.target_w[0] >= 0.083 - 1e-6


def test_symmetric_holding_and_new_name():
    """Acceptance criterion #2 from the handoff doc: a holding at cap and a
    new name with identical (μ, σ) must size symmetrically (no new-name
    pinning) once the planned-sell starvation is removed."""
    w_current = np.array([0.083, 0.0])
    sol = solve_portfolio_qp(
        w_current=w_current,
        mu=np.array([0.04, 0.04]),
        sigma=np.array([0.15, 0.15]),
        risk_aversion=3.0,
        cost_kappa=0.0,
        w_upper=np.full(2, 0.083),
        w_lower=0.0,
        dw_max=0.50,
        turnover_max=0.20,
        min_invested_pct=0.5,
        cash_drag_lambda=0.5,
        turnover_exempt_forced_trims=True,
    )
    assert sol.status.startswith("optimal"), sol.status
    assert sol.target_w[0] == pytest.approx(sol.target_w[1], abs=5e-3)


# ── ConstraintSnapshot contract ──────────────────────────────────────────────

def _snap_kwargs(**overrides):
    n = 2
    kwargs = dict(
        n=n,
        tickers=("HELD", "NEW"),
        w_current=np.array([0.05, 0.0]),
        w_upper_hard=np.full(n, 0.083),
        w_upper=np.array([0.05, 0.083]),
        w_lower=0.0,
        dw_max=np.full(n, 0.5),
        cash_reserve=0.0,
        turnover_max=0.2,
        drawdown=0.0,
        drawdown_limit=0.2,
        gross_max=None,
        wash_sale_mask=np.zeros(n, dtype=bool),
    )
    kwargs.update(overrides)
    return kwargs


def test_snapshot_accepts_valid_no_sell_mask():
    snap = ConstraintSnapshot(
        **_snap_kwargs(no_sell_mask=np.array([True, False])),
    )
    assert snap.no_sell_mask is not None
    assert bool(snap.no_sell_mask[0]) is True


def test_snapshot_rejects_mask_with_infeasible_hold_flat():
    # masked name with w_current > w_upper → Δw ≥ 0 infeasible
    with pytest.raises(ValueError, match="structurally infeasible"):
        ConstraintSnapshot(
            **_snap_kwargs(
                w_current=np.array([0.06, 0.0]),
                w_upper=np.array([0.05, 0.083]),
                no_sell_mask=np.array([True, False]),
            ),
        )


def test_snapshot_rejects_mask_on_over_hard_cap_holding():
    with pytest.raises(ValueError, match="OVER-hard-cap"):
        ConstraintSnapshot(
            **_snap_kwargs(
                w_current=np.array([0.20, 0.0]),
                w_upper_hard=np.full(2, 0.083),
                w_upper=np.array([0.083, 0.083]),
                no_sell_mask=np.array([True, False]),
            ),
        )


# ── ApplySoftSellGuardMaskTask ───────────────────────────────────────────────

import datetime as _dt


class _Holding:
    def __init__(self, entry_date: str):
        self.entry_date = _dt.date.fromisoformat(entry_date)
        self.entry_regime = None


class _Ctx:
    def __init__(self, *, config, tickers, w_current, w_upper, w_hard, holdings,
                 regime="BULL_CALM", today=_dt.date(2026, 6, 9)):
        self.config = config
        self.regime = regime
        self.today = today
        self.holdings = holdings
        self._qp_tickers = tickers
        self._qp_w_current = np.asarray(w_current, dtype=float)
        self._qp_w_upper = np.asarray(w_upper, dtype=float)
        self._qp_w_upper_hard = np.asarray(w_hard, dtype=float)


def _guard_config(align: bool = True):
    return {
        "rotation": {"joint_actions": {
            "qp_soft_sell_guard": {
                "enabled": True,
                "align_solver": align,
                "min_holding_days_by_regime": {"BULL_CALM": 60},
            },
        }},
        "risk": {"panel_exit": {}},
    }


def test_task_masks_young_within_cap_holding():
    ctx = _Ctx(
        config=_guard_config(),
        tickers=["YOUNG", "NEW"],
        w_current=[0.05, 0.0],
        w_upper=[0.04, 0.083],     # soft cap below current (scaled down)
        w_hard=[0.083, 0.083],
        holdings={"YOUNG": _Holding("2026-06-01")},  # 8 days < 60
    )
    ApplySoftSellGuardMaskTask().run(ctx)
    mask = getattr(ctx, "_qp_no_sell_mask", None)
    assert mask is not None and bool(mask[0]) and not bool(mask[1])
    # hold-flat: soft cap raised to w_current for the masked name
    assert ctx._qp_w_upper[0] == pytest.approx(0.05)


def test_task_never_masks_over_hard_cap_holding():
    ctx = _Ctx(
        config=_guard_config(),
        tickers=["ORCL", "NEW"],
        w_current=[0.194, 0.0],    # over hard cap — must stay sellable
        w_upper=[0.083, 0.083],
        w_hard=[0.083, 0.083],
        holdings={"ORCL": _Holding("2026-06-01")},
    )
    ApplySoftSellGuardMaskTask().run(ctx)
    assert getattr(ctx, "_qp_no_sell_mask", None) is None
    assert ctx._qp_w_upper[0] == pytest.approx(0.083)


def test_task_noop_when_align_solver_off():
    ctx = _Ctx(
        config=_guard_config(align=False),
        tickers=["YOUNG"],
        w_current=[0.05],
        w_upper=[0.04],
        w_hard=[0.083],
        holdings={"YOUNG": _Holding("2026-06-01")},
    )
    ApplySoftSellGuardMaskTask().run(ctx)
    assert getattr(ctx, "_qp_no_sell_mask", None) is None
    assert ctx._qp_w_upper[0] == pytest.approx(0.04)


def test_task_clears_stale_mask_when_suppression_disappears():
    ctx = _Ctx(
        config=_guard_config(),
        tickers=["YOUNG"],
        w_current=[0.05],
        w_upper=[0.04],
        w_hard=[0.083],
        holdings={"YOUNG": _Holding("2026-06-01")},
    )
    ApplySoftSellGuardMaskTask().run(ctx)
    assert getattr(ctx, "_qp_no_sell_mask", None) is not None

    # Emulate the next bar after upstream constraint tasks recomputed w_upper
    # and the holding aged out of the soft-sell suppression window.
    ctx._qp_w_upper = np.asarray([0.04], dtype=float)
    ctx.holdings = {"YOUNG": _Holding("2026-01-01")}

    ApplySoftSellGuardMaskTask().run(ctx)

    assert getattr(ctx, "_qp_no_sell_mask", None) is None
    assert ctx._qp_w_upper[0] == pytest.approx(0.04)
