"""Parity test for the drawdown Job lift (functional-lift slice 8).

Second decision-tree Job lifted into the pipeline repo (after the regime Job,
slice 7). Proves:

1. The Job + its Tasks import cleanly — the `from .context import` relative
   import resolves through the slice-7 re-export shim, and the rewritten
   `renquant_pipeline.kernel.exits` import (task_drawdown_rebalance) resolves.
2. The full DrawdownJob runs end-to-end and makes the actual buy-side circuit
   decision (skip_buys) — not just imports.

Fixture mirrors the umbrella's tests/test_drawdown_circuit.py family
(SimpleNamespace ctx carrying exactly the fields the drawdown tasks read/write:
portfolio_value, hwm, skip_buys, regime, config, holdings, exits).
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

job_drawdown = importlib.import_module(
    "renquant_pipeline.kernel.pipeline.job_drawdown"
)
task_drawdown_rebalance = importlib.import_module(
    "renquant_pipeline.kernel.pipeline.task_drawdown_rebalance"
)


def _ctx(*, hwm: float, pv: float, skip_buys: bool = False,
         halt_pct: float = 0.20, resume_pct: float | None = None,
         regime: str = "BULL_CALM") -> SimpleNamespace:
    regime_p: dict = {"drawdown_halt_pct": halt_pct}
    if resume_pct is not None:
        regime_p["drawdown_resume_pct"] = resume_pct
    return SimpleNamespace(
        portfolio_value=pv,
        hwm=hwm,
        skip_buys=skip_buys,
        regime=regime,
        config={"regime_params": {regime: regime_p}, "risk": {}},
        holdings={},
        exits=[],
    )


def test_drawdown_job_imports() -> None:
    assert hasattr(job_drawdown, "DrawdownJob")


def test_hwm_ratchets_and_no_halt_below_threshold() -> None:
    # PV above prior HWM → HWM ratchets up; DD=0 < halt → buys stay enabled.
    ctx = _ctx(hwm=100.0, pv=110.0)
    job_drawdown.DrawdownJob().run(ctx)
    assert ctx.hwm == 110.0
    assert ctx.skip_buys is False


def test_circuit_halts_on_drawdown_breach() -> None:
    # 30% drawdown ≥ 20% halt → block new buys.
    ctx = _ctx(hwm=100.0, pv=70.0, halt_pct=0.20)
    job_drawdown.DrawdownJob().run(ctx)
    assert ctx.skip_buys is True


def test_circuit_resumes_on_recovery() -> None:
    # Already halted; PV recovered to 5% DD < 20% halt → buys resume.
    ctx = _ctx(hwm=100.0, pv=95.0, skip_buys=True, halt_pct=0.20)
    job_drawdown.DrawdownJob().run(ctx)
    assert ctx.skip_buys is False


def test_resume_hysteresis_keeps_halt_between_resume_and_halt() -> None:
    # halted; DD=12% sits between resume(10%) and halt(20%) → stay halted.
    ctx = _ctx(hwm=100.0, pv=88.0, skip_buys=True, halt_pct=0.20, resume_pct=0.10)
    job_drawdown.DrawdownJob().run(ctx)
    assert ctx.skip_buys is True


def test_nan_portfolio_value_fails_safe() -> None:
    # Non-finite PV → HWM preserved AND buys blocked (fail-SAFE), never NaN-passes.
    ctx = _ctx(hwm=100.0, pv=float("nan"), halt_pct=0.20)
    job_drawdown.DrawdownJob().run(ctx)
    assert ctx.hwm == 100.0          # HWMUpdateTask kept prior HWM
    assert ctx.skip_buys is True     # DrawdownCircuitTask fail-safe


def test_drawdown_rebalance_disabled_by_default_emits_no_exits() -> None:
    ctx = _ctx(hwm=100.0, pv=70.0)
    ctx.holdings = {"AAA": SimpleNamespace(panel_score=0.1)}
    job_drawdown.DrawdownJob().run(ctx)
    assert ctx.exits == []  # risk.drawdown_rebalance not enabled


def test_drawdown_dd_helper_matches_umbrella_formula() -> None:
    f = task_drawdown_rebalance.compute_portfolio_drawdown
    assert f(100.0, 75.0) == pytest.approx(0.25)
    assert f(100.0, 110.0) == 0.0            # PV above HWM → 0
    assert f(0.0, 50.0) == 0.0               # zero HWM → 0
    assert f(float("nan"), 50.0) == 0.0      # NaN-guard
