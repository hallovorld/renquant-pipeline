"""Tests for the S5 decision-ledger write task wiring."""
from __future__ import annotations

import datetime
import sys
from dataclasses import dataclass, field
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest


@dataclass
class _FakeCtx:
    config: dict = field(default_factory=dict)
    today: datetime.date = datetime.date(2026, 7, 1)
    regime: str = "BULL_CALM"
    candidates: list = field(default_factory=list)
    exits: list = field(default_factory=list)
    holdings: dict = field(default_factory=dict)
    rotations: list = field(default_factory=list)
    counters: dict = field(default_factory=dict)
    run_id: str = "2026-07-01-daily-full"


def _make_task():
    from renquant_pipeline.kernel.pipeline.task_decision_ledger import (
        DecisionLedgerWriteTask,
    )
    return DecisionLedgerWriteTask()


@pytest.fixture(autouse=True)
def _mock_orchestrator_module():
    """Pre-register a fake renquant_orchestrator.decision_ledger so
    unittest.mock.patch can target it without the real package installed."""
    orch_mod = ModuleType("renquant_orchestrator")
    dl_mod = ModuleType("renquant_orchestrator.decision_ledger")
    dl_mod.connect = MagicMock()
    dl_mod.write_verdicts = MagicMock()
    orch_mod.decision_ledger = dl_mod

    originals = {}
    for name in ("renquant_orchestrator", "renquant_orchestrator.decision_ledger"):
        originals[name] = sys.modules.get(name)

    sys.modules["renquant_orchestrator"] = orch_mod
    sys.modules["renquant_orchestrator.decision_ledger"] = dl_mod
    yield
    for name, orig in originals.items():
        if orig is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = orig


class TestDecisionLedgerWriteTask:
    def test_disabled_by_default(self):
        ctx = _FakeCtx()
        task = _make_task()
        assert task.run(ctx) is False

    def test_disabled_when_false(self):
        ctx = _FakeCtx(config={"decision_ledger": {"enabled": False}})
        task = _make_task()
        assert task.run(ctx) is False

    def test_enabled_calls_formatters(self):
        ctx = _FakeCtx(config={"decision_ledger": {"enabled": True}})
        task = _make_task()

        mock_verdicts = [
            {"scope": "s104", "gate": "regime", "verdict": "allow",
             "reason": "BULL_CALM", "inputs": {}},
        ]
        mock_decisions = [
            {"as_of": "2026-07-01", "scope": "s104", "ticker": "AAPL",
             "gate": "buy", "verdict": "allow"},
        ]

        with patch(
            "renquant_pipeline.decision_ledger.format_gate_verdicts",
            return_value=mock_verdicts,
        ) as fmt_v, patch(
            "renquant_pipeline.decision_ledger.format_ticker_decisions",
            return_value=mock_decisions,
        ) as fmt_d, patch(
            "renquant_orchestrator.decision_ledger.connect",
        ) as mock_connect, patch(
            "renquant_orchestrator.decision_ledger.write_verdicts",
        ) as mock_write:
            mock_conn = MagicMock()
            mock_connect.return_value = mock_conn

            result = task.run(ctx)

            fmt_v.assert_called_once()
            fmt_d.assert_called_once()
            mock_connect.assert_called_once()
            mock_write.assert_called_once()
            mock_conn.close.assert_called_once()

            assert result is True
            assert ctx.counters["s5_verdicts_written"] == 1
            assert ctx.counters["s5_decisions_formatted"] == 1

    def test_decisions_formatted_but_never_persisted(self):
        """Regression guard: per-ticker decisions must NOT be written to
        decision_outcomes from this task. Writing a verdict-only row here
        (before the 60d aging window) would poison outcome_observer's
        pending_decisions() existence check (renquant-orchestrator#351),
        permanently suppressing that (as_of, scope, gate) from ever being
        picked up for real forward-return backfill. See module docstring."""
        ctx = _FakeCtx(config={"decision_ledger": {"enabled": True}})
        task = _make_task()

        mock_decisions = [
            {"as_of": "2026-07-01", "scope": "s104", "ticker": "AAPL",
             "gate": "buy", "verdict": "allow"},
        ]

        ledger_attribution_mod = ModuleType("renquant_orchestrator.ledger_attribution")
        ledger_attribution_mod.write_outcomes = MagicMock()
        ledger_attribution_mod.connect_attribution = MagicMock()

        with patch(
            "renquant_pipeline.decision_ledger.format_gate_verdicts",
            return_value=[{"scope": "s", "gate": "g", "verdict": "allow",
                           "reason": "r", "inputs": {}}],
        ), patch(
            "renquant_pipeline.decision_ledger.format_ticker_decisions",
            return_value=mock_decisions,
        ), patch(
            "renquant_orchestrator.decision_ledger.connect",
        ) as mock_connect, patch(
            "renquant_orchestrator.decision_ledger.write_verdicts",
        ), patch.dict(
            "sys.modules",
            {"renquant_orchestrator.ledger_attribution": ledger_attribution_mod},
        ):
            mock_connect.return_value = MagicMock()
            result = task.run(ctx)

            assert result is True
            assert ctx.counters["s5_decisions_formatted"] == 1
            ledger_attribution_mod.write_outcomes.assert_not_called()
            ledger_attribution_mod.connect_attribution.assert_not_called()

    def test_failopen_on_orchestrator_import_error(self):
        ctx = _FakeCtx(config={"decision_ledger": {"enabled": True}})
        task = _make_task()

        with patch(
            "renquant_pipeline.decision_ledger.format_gate_verdicts",
            return_value=[{"scope": "s", "gate": "g", "verdict": "allow",
                           "reason": "r", "inputs": {}}],
        ), patch(
            "renquant_pipeline.decision_ledger.format_ticker_decisions",
            return_value=[],
        ), patch.dict(
            "sys.modules",
            {"renquant_orchestrator": None,
             "renquant_orchestrator.decision_ledger": None},
        ):
            result = task.run(ctx)
            assert result is False
            assert ctx.counters.get("s5_write_skipped") == 1

    def test_failopen_on_write_exception(self):
        ctx = _FakeCtx(config={"decision_ledger": {"enabled": True}})
        task = _make_task()

        with patch(
            "renquant_pipeline.decision_ledger.format_gate_verdicts",
            return_value=[{"scope": "s", "gate": "g", "verdict": "allow",
                           "reason": "r", "inputs": {}}],
        ), patch(
            "renquant_pipeline.decision_ledger.format_ticker_decisions",
            return_value=[],
        ), patch(
            "renquant_orchestrator.decision_ledger.connect",
            side_effect=RuntimeError("DB locked"),
        ):
            result = task.run(ctx)
            assert result is False
            assert ctx.counters.get("s5_write_error") == 1

    def test_run_id_fallback(self):
        ctx = _FakeCtx(config={"decision_ledger": {"enabled": True}})
        ctx.run_id = None

        task = _make_task()

        with patch(
            "renquant_pipeline.decision_ledger.format_gate_verdicts",
            return_value=[],
        ) as fmt_v, patch(
            "renquant_pipeline.decision_ledger.format_ticker_decisions",
            return_value=[],
        ), patch(
            "renquant_orchestrator.decision_ledger.connect",
        ) as mock_connect, patch(
            "renquant_orchestrator.decision_ledger.write_verdicts",
        ):
            mock_connect.return_value = MagicMock()
            task.run(ctx)

            call_args = fmt_v.call_args
            assert call_args[0][2] == "2026-07-01-unscoped"

    def test_verdicts_shape_passed_to_write(self):
        ctx = _FakeCtx(config={"decision_ledger": {"enabled": True}})
        task = _make_task()

        raw_verdicts = [
            {"scope": "s104", "gate": "conviction", "verdict": "allow",
             "reason": "3/5 above floor", "inputs": {"n": 3, "floor": 0.03}},
            {"scope": "s104", "gate": "vol_gate", "verdict": "block",
             "reason": "2 blocked by vol", "inputs": {"blocked": ["TSLA"]}},
        ]

        with patch(
            "renquant_pipeline.decision_ledger.format_gate_verdicts",
            return_value=raw_verdicts,
        ), patch(
            "renquant_pipeline.decision_ledger.format_ticker_decisions",
            return_value=[],
        ), patch(
            "renquant_orchestrator.decision_ledger.connect",
        ) as mock_connect, patch(
            "renquant_orchestrator.decision_ledger.write_verdicts",
        ) as mock_write:
            mock_connect.return_value = MagicMock()
            task.run(ctx)

            written = mock_write.call_args[1]["verdicts"]
            assert len(written) == 2
            assert written[0]["gate"] == "conviction"
            assert written[1]["verdict"] == "block"
