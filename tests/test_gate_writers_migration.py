"""GateRegistry writer migration #1 — task_gates.py dual-write (S2-PR4).

Design: renquant-orchestrator
doc/research/2026-06-12-engineering-architecture-deep-plan.md S2-PR4 +
errata C(iii); census authority counts these 9 sites (AST census,
renquant_orchestrator.engineering_census).

Contract (post-retirement, errata C(iii)): gate tasks submit verdicts
and NEVER write ``ctx.buy_blocked`` directly; ``BuyGatesJob.run``
applies the max-join aggregate once at the job boundary. Blocking
behavior is therefore asserted at the JOB level; task-level tests
assert submission. The census test pins ZERO direct writers.
"""
from __future__ import annotations

import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from renquant_pipeline.kernel.config import BULL_VOLATILE
from renquant_pipeline.kernel.pipeline.task_gates import (
    BullVolOffensiveBlockTask,
    DrawdownGateTask,
    EMA50GateTask,
    FlattenCooldownGateTask,
    RegimeAlphaGateTask,
    TransitionWindowTask,
    VelocityCrashTask,
)


def _ctx(**kw) -> SimpleNamespace:
    base = dict(
        config={}, counters={}, skip_buys=False, buy_blocked=False,
        bear_only=False, regime="BULL_CALM", confidence=0.9,
        regime_state=None, monitor_state={}, spy_returns=[],
        ohlcv={}, today=datetime.date(2026, 6, 12), gate_registry=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _registry_blocked(ctx) -> bool:
    return (ctx.gate_registry is not None
            and ctx.gate_registry.blocked("book"))


def _apply_aggregate(ctx) -> None:
    """What BuyGatesJob.run does after the chain — applied manually in
    task-level tests."""
    if _registry_blocked(ctx):
        ctx.buy_blocked = True


class TestBlockingPathsSubmit:

    def test_drawdown_gate(self):
        ctx = _ctx(skip_buys=True)
        DrawdownGateTask().run(ctx)
        assert not ctx.buy_blocked, "task must not write the flag directly"
        assert _registry_blocked(ctx)
        _apply_aggregate(ctx)
        assert ctx.buy_blocked
        rows = ctx.gate_registry.ledger_rows(run_id="t")
        assert rows[0]["gate"] == "drawdown_circuit"

    def test_flatten_cooldown_same_bar(self):
        ctx = _ctx(monitor_state={"flatten_last_date_iso": "2026-06-12",
                                  "flatten_cooldown_bars": 3})
        FlattenCooldownGateTask().run(ctx)
        assert not ctx.buy_blocked and _registry_blocked(ctx)
        v = ctx.gate_registry.ledger_rows(run_id="t")[0]
        assert v["gate"] == "flatten_cooldown"
        assert v["inputs"]["cooldown_bars"] == 3

    def test_flatten_cooldown_in_window(self):
        ctx = _ctx(monitor_state={"flatten_last_date_iso": "2026-06-10",
                                  "flatten_cooldown_bars": 5})
        FlattenCooldownGateTask().run(ctx)
        assert not ctx.buy_blocked and _registry_blocked(ctx)
        assert ctx.gate_registry.ledger_rows(run_id="t")[0]["inputs"]["days_since"] == 2

    def test_transition_window(self):
        ctx = _ctx(regime_state=SimpleNamespace(in_transition=True))
        TransitionWindowTask().run(ctx)
        assert not ctx.buy_blocked and _registry_blocked(ctx)

    def test_bull_vol_defensives_too(self):
        ctx = _ctx(regime=BULL_VOLATILE,
                   config={"regime": {"bull_vol_block_offensive": True,
                                      "bull_vol_defensives_too": True}})
        BullVolOffensiveBlockTask().run(ctx)
        assert not ctx.buy_blocked and _registry_blocked(ctx)

    def test_regime_alpha(self):
        ctx = _ctx(regime="BULL_CALM",
                   config={"regime_params": {"BULL_CALM": {"disable_new_buys": True}}})
        RegimeAlphaGateTask().run(ctx)
        assert not ctx.buy_blocked and _registry_blocked(ctx)
        assert ctx.gate_registry.ledger_rows(run_id="t")[0]["inputs"]["regime"] == "BULL_CALM"

    def test_velocity_crash(self):
        ctx = _ctx(spy_returns=[-0.05, -0.04, -0.03],
                   config={"regime_params": {"BULL_CALM": {
                       "spy_velocity_halt_pct": 0.05,
                       "spy_velocity_lookback_days": 3}}})
        VelocityCrashTask().run(ctx)
        assert not ctx.buy_blocked and _registry_blocked(ctx)

    def test_ema50_missing_spy_fail_safe(self):
        ctx = _ctx(ohlcv={})
        EMA50GateTask().run(ctx)
        assert not ctx.buy_blocked and _registry_blocked(ctx)
        assert ctx.gate_registry.ledger_rows(run_id="t")[0]["inputs"]["data_outage"] is True

    def test_ema50_below_trend(self):
        # 60 declining closes → last close below EMA50
        closes = pd.Series([100.0 - i for i in range(60)])
        ctx = _ctx(ohlcv={"SPY": pd.DataFrame({"close": closes})})
        EMA50GateTask().run(ctx)
        assert not ctx.buy_blocked and _registry_blocked(ctx)
        assert ctx.gate_registry.ledger_rows(run_id="t")[0]["inputs"]["data_outage"] is False


class TestNonBlockingPathsStaySilent:
    """A gate that doesn't block must not submit — no phantom ledger rows."""

    @pytest.mark.parametrize("task,ctx_kw", [
        (DrawdownGateTask, {}),
        (FlattenCooldownGateTask, {"monitor_state": {}}),
        (TransitionWindowTask, {"regime_state": SimpleNamespace(in_transition=False)}),
        (BullVolOffensiveBlockTask, {"regime": BULL_VOLATILE}),  # knob off
        (RegimeAlphaGateTask, {}),
        (VelocityCrashTask, {"spy_returns": [0.01, 0.01, 0.01]}),
    ])
    def test_silent_when_not_blocking(self, task, ctx_kw):
        ctx = _ctx(**ctx_kw)
        task().run(ctx)
        assert not ctx.buy_blocked
        assert ctx.gate_registry is None or \
            ctx.gate_registry.ledger_rows(run_id="t") == []

    def test_ema50_above_trend_silent(self):
        closes = pd.Series([100.0 + i for i in range(60)])
        ctx = _ctx(ohlcv={"SPY": pd.DataFrame({"close": closes})})
        EMA50GateTask().run(ctx)
        assert not ctx.buy_blocked
        assert not _registry_blocked(ctx)


class TestCensusRetirement:
    """Errata C(iii): zero direct buy_blocked writers in task_gates.py."""

    def test_direct_writers_zero(self):
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parent.parent /
               "src/renquant_pipeline/kernel/pipeline/task_gates.py")
        tree = ast.parse(src.read_text())
        count = 0
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Attribute) and t.attr == "buy_blocked"
                            for t in node.targets)
                    and isinstance(node.value, ast.Constant)
                    and node.value.value is True):
                count += 1
        assert count == 0, (
            f"task_gates.py direct buy_blocked writers = {count}, expected 0 "
            f"post-retirement (errata C(iii)) — new gates must submit to the "
            f"registry, never write the flag")


class TestChokePoint:
    """BuyGatesJob.run applies the aggregate once at the job boundary."""

    def test_job_applies_block_aggregate(self):
        from renquant_pipeline.kernel.pipeline.job_gates import BuyGatesJob

        ctx = _ctx(skip_buys=True)  # DrawdownGate will submit block
        BuyGatesJob().run(ctx)
        assert ctx.buy_blocked
        assert ctx.gate_registry.blocked("book")

    def test_job_leaves_flag_clear_when_no_blocks(self):
        from renquant_pipeline.kernel.pipeline.job_gates import BuyGatesJob
        import pandas as pd

        closes = pd.Series([100.0 + i for i in range(60)])
        ctx = _ctx(ohlcv={"SPY": pd.DataFrame({"close": closes})},
                   spy_returns=[0.01, 0.01, 0.01])
        BuyGatesJob().run(ctx)
        assert not ctx.buy_blocked

    def test_short_circuit_preserved(self):
        # FlattenCooldown same-bar returns False → later gates never run;
        # the aggregate still lands at the job boundary.
        from renquant_pipeline.kernel.pipeline.job_gates import BuyGatesJob

        ctx = _ctx(monitor_state={"flatten_last_date_iso": "2026-06-12",
                                  "flatten_cooldown_bars": 3})
        BuyGatesJob().run(ctx)
        assert ctx.buy_blocked
        gates = [r["gate"] for r in ctx.gate_registry.ledger_rows(run_id="t")]
        assert gates == ["flatten_cooldown"], "chain must have short-circuited"

    def test_preexisting_flag_never_cleared(self):
        # Risk-monotone at the job level too: an upstream writer's True is
        # never reset by an all-allow gate chain.
        from renquant_pipeline.kernel.pipeline.job_gates import BuyGatesJob
        import pandas as pd

        closes = pd.Series([100.0 + i for i in range(60)])
        ctx = _ctx(buy_blocked=True,
                   ohlcv={"SPY": pd.DataFrame({"close": closes})},
                   spy_returns=[0.01, 0.01, 0.01])
        BuyGatesJob().run(ctx)
        assert ctx.buy_blocked
