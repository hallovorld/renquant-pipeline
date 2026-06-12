"""GateRegistry writer migration #1 — task_gates.py dual-write (S2-PR4).

Design: renquant-orchestrator
doc/research/2026-06-12-engineering-architecture-deep-plan.md S2-PR4 +
errata C(iii); census authority counts these 9 sites (AST census,
renquant_orchestrator.engineering_census).

Contract pinned here: every ``ctx.buy_blocked = True`` site in
task_gates.py ALSO submits a block verdict to the registry — and never
submits when it doesn't block. Equivalence (registry.blocked("book") ==
ctx.buy_blocked) holds for every gate in this file by construction;
behavior is unchanged because the direct writes are untouched (additive
lines only — the retirement happens with the aggregate choke point).
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


def _assert_equivalence(ctx) -> None:
    reg_blocked = (ctx.gate_registry is not None
                   and ctx.gate_registry.blocked("book"))
    assert reg_blocked == ctx.buy_blocked, (
        f"dual-write divergence: registry={reg_blocked} "
        f"direct={ctx.buy_blocked}")


class TestBlockingPathsSubmit:

    def test_drawdown_gate(self):
        ctx = _ctx(skip_buys=True)
        DrawdownGateTask().run(ctx)
        assert ctx.buy_blocked
        _assert_equivalence(ctx)
        rows = ctx.gate_registry.ledger_rows(run_id="t")
        assert rows[0]["gate"] == "drawdown_circuit"

    def test_flatten_cooldown_same_bar(self):
        ctx = _ctx(monitor_state={"flatten_last_date_iso": "2026-06-12",
                                  "flatten_cooldown_bars": 3})
        FlattenCooldownGateTask().run(ctx)
        assert ctx.buy_blocked
        _assert_equivalence(ctx)
        v = ctx.gate_registry.ledger_rows(run_id="t")[0]
        assert v["gate"] == "flatten_cooldown"
        assert v["inputs"]["cooldown_bars"] == 3

    def test_flatten_cooldown_in_window(self):
        ctx = _ctx(monitor_state={"flatten_last_date_iso": "2026-06-10",
                                  "flatten_cooldown_bars": 5})
        FlattenCooldownGateTask().run(ctx)
        assert ctx.buy_blocked
        _assert_equivalence(ctx)
        assert ctx.gate_registry.ledger_rows(run_id="t")[0]["inputs"]["days_since"] == 2

    def test_transition_window(self):
        ctx = _ctx(regime_state=SimpleNamespace(in_transition=True))
        TransitionWindowTask().run(ctx)
        assert ctx.buy_blocked
        _assert_equivalence(ctx)

    def test_bull_vol_defensives_too(self):
        ctx = _ctx(regime=BULL_VOLATILE,
                   config={"regime": {"bull_vol_block_offensive": True,
                                      "bull_vol_defensives_too": True}})
        BullVolOffensiveBlockTask().run(ctx)
        assert ctx.buy_blocked
        _assert_equivalence(ctx)

    def test_regime_alpha(self):
        ctx = _ctx(regime="BULL_CALM",
                   config={"regime_params": {"BULL_CALM": {"disable_new_buys": True}}})
        RegimeAlphaGateTask().run(ctx)
        assert ctx.buy_blocked
        _assert_equivalence(ctx)
        assert ctx.gate_registry.ledger_rows(run_id="t")[0]["inputs"]["regime"] == "BULL_CALM"

    def test_velocity_crash(self):
        ctx = _ctx(spy_returns=[-0.05, -0.04, -0.03],
                   config={"regime_params": {"BULL_CALM": {
                       "spy_velocity_halt_pct": 0.05,
                       "spy_velocity_lookback_days": 3}}})
        VelocityCrashTask().run(ctx)
        assert ctx.buy_blocked
        _assert_equivalence(ctx)

    def test_ema50_missing_spy_fail_safe(self):
        ctx = _ctx(ohlcv={})
        EMA50GateTask().run(ctx)
        assert ctx.buy_blocked
        _assert_equivalence(ctx)
        assert ctx.gate_registry.ledger_rows(run_id="t")[0]["inputs"]["data_outage"] is True

    def test_ema50_below_trend(self):
        # 60 declining closes → last close below EMA50
        closes = pd.Series([100.0 - i for i in range(60)])
        ctx = _ctx(ohlcv={"SPY": pd.DataFrame({"close": closes})})
        EMA50GateTask().run(ctx)
        assert ctx.buy_blocked
        _assert_equivalence(ctx)
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
        _assert_equivalence(ctx)

    def test_ema50_above_trend_silent(self):
        closes = pd.Series([100.0 + i for i in range(60)])
        ctx = _ctx(ohlcv={"SPY": pd.DataFrame({"close": closes})})
        EMA50GateTask().run(ctx)
        assert not ctx.buy_blocked
        _assert_equivalence(ctx)


class TestCensusRetirement:
    """The AST census over task_gates.py must show the direct writes are
    still present (dual-write phase) — this test flips to assert ZERO
    when the aggregate choke point retires them (errata C(iii))."""

    def test_direct_writers_still_nine(self):
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
        assert count == 9, (
            f"task_gates.py direct buy_blocked writers = {count}, expected 9 "
            f"during dual-write; if you retired them, flip this test to 0")
