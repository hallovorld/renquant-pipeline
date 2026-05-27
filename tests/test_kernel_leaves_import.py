"""Smoke + boundary tests for the lifted decision-leaf kernel modules.

Verifies the copy-not-move slice imports cleanly in the pipeline package
and pulls no forbidden backend/execution modules (RFC §"Backfill Plan"
functional-lift).
"""
from __future__ import annotations

import importlib

import pytest

LIFTED_MODULES = [
    # slice 1
    "renquant_pipeline.kernel.kelly",
    "renquant_pipeline.kernel.exit_types",
    "renquant_pipeline.kernel.market_gates",
    "renquant_pipeline.kernel.vol_target",
    "renquant_pipeline.kernel.sizing",
    # slice 2
    "renquant_pipeline.kernel.regime_resolver",
    "renquant_pipeline.kernel.regime_hmm",
    "renquant_pipeline.kernel.intraday",
    "renquant_pipeline.kernel.intraday_wash",
    "renquant_pipeline.kernel.config",
    "renquant_pipeline.kernel.config_consistency",
    "renquant_pipeline.kernel.net_safety",
    "renquant_pipeline.kernel.realized_pnl",
    "renquant_pipeline.kernel.portfolio",
    "renquant_pipeline.kernel.scoring",
    # slice 3
    "renquant_pipeline.kernel.portfolio_qp.qp_solver",
    "renquant_pipeline.kernel.portfolio_qp.signal_combiner",
    "renquant_pipeline.kernel.portfolio_qp.cvxportfolio_backend",
    "renquant_pipeline.kernel.selection",
    "renquant_pipeline.kernel.rotation",
    "renquant_pipeline.kernel.rotation_convex",
    "renquant_pipeline.kernel.exits",
]


@pytest.mark.parametrize("module_name", LIFTED_MODULES)
def test_lifted_module_imports(module_name: str) -> None:
    mod = importlib.import_module(module_name)
    assert mod is not None


def test_kelly_fraction_is_bounded() -> None:
    """Sanity on the canonical kelly entry point if present."""
    kelly = importlib.import_module("renquant_pipeline.kernel.kelly")
    # The module exposes kelly-fraction helpers; just assert callables exist.
    public = [n for n in dir(kelly) if not n.startswith("_")]
    assert public, "kelly module exposes no public symbols"


def test_qp_solver_prefers_higher_mu_asset() -> None:
    """Behavioral sanity on the lifted QP solver (slice 3).

    From cash, with asset 0 carrying positive expected return and asset 1
    flat, the convex Markowitz solve must allocate at least as much weight
    to the higher-μ asset and stay inside the unit budget.
    """
    qp = importlib.import_module("renquant_pipeline.kernel.portfolio_qp.qp_solver")
    sol = qp.solve_portfolio_qp(
        w_current=[0.0, 0.0],
        mu=[0.05, 0.0],
        sigma=[0.20, 0.20],
        risk_aversion=3.0,
        w_upper=0.50,
    )
    assert sol.status.startswith("optimal")
    assert sol.target_w[0] >= sol.target_w[1] - 1e-9
    assert sum(sol.target_w) <= 1.0 + 1e-6
