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
def _mock_common_module():
    """Pre-register a fake renquant_common.decision_ledger so
    unittest.mock.patch can target it without the real package installed.

    task_decision_ledger.py does ``from renquant_common.decision_ledger import
    connect, write_verdicts`` as a function-local import (V-003: moved off of
    renquant_orchestrator.decision_ledger onto renquant_common.decision_ledger,
    see renquant-common#30). The fake submodule mirrors that module's real
    shape (connect, write_verdicts, DDL, DEFAULT_DB, _VALID_VERDICTS) so
    patches here target the same names the production import resolves at
    call time.

    Unlike the old renquant_orchestrator fake, ``renquant_common`` is a REAL
    dependency renquant_pipeline already imports from elsewhere (Job,
    Pipeline, Task, ... in renquant_pipeline/__init__.py -> inference.py).
    Blindly replacing ``sys.modules["renquant_common"]`` wholesale — as the
    pre-V-003 fixture safely did for the (unrelated to pipeline) orchestrator
    package — shadows those real exports and breaks any test whose first
    ``import renquant_pipeline`` happens while the fixture is active, with
    ``ImportError: cannot import name 'Job' from 'renquant_common'``. So this
    fixture reuses the real ``renquant_common`` module object when it's
    importable (as it is in this environment, sibling-checkout on
    PYTHONPATH/pytest `pythonpath` ini) and only swaps in the fake
    ``decision_ledger`` submodule/attribute, restoring both on teardown.
    """
    try:
        import renquant_common as common_mod
        created_common = False
    except ImportError:
        common_mod = ModuleType("renquant_common")
        created_common = True
        sys.modules["renquant_common"] = common_mod

    dl_mod = ModuleType("renquant_common.decision_ledger")
    dl_mod.connect = MagicMock()
    dl_mod.write_verdicts = MagicMock()
    dl_mod.DDL = ""
    dl_mod.DEFAULT_DB = None
    dl_mod._VALID_VERDICTS = ("allow", "halve", "block")

    original_submodule = sys.modules.get("renquant_common.decision_ledger")
    had_attr = hasattr(common_mod, "decision_ledger")
    original_attr = getattr(common_mod, "decision_ledger", None)

    common_mod.decision_ledger = dl_mod
    sys.modules["renquant_common.decision_ledger"] = dl_mod

    yield

    if original_submodule is None:
        sys.modules.pop("renquant_common.decision_ledger", None)
    else:
        sys.modules["renquant_common.decision_ledger"] = original_submodule

    if had_attr:
        common_mod.decision_ledger = original_attr
    elif hasattr(common_mod, "decision_ledger"):
        delattr(common_mod, "decision_ledger")

    if created_common:
        sys.modules.pop("renquant_common", None)


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
            "renquant_common.decision_ledger.connect",
        ) as mock_connect, patch(
            "renquant_common.decision_ledger.write_verdicts",
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
            "renquant_common.decision_ledger.connect",
        ) as mock_connect, patch(
            "renquant_common.decision_ledger.write_verdicts",
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

    def test_failopen_on_common_import_error(self):
        """Simulates version skew where renquant_common is installed but
        does not yet provide decision_ledger (e.g. common#30 not merged
        yet). Only the submodule is nulled in sys.modules — nulling the
        top-level renquant_common too would also break the real Job/
        Pipeline/Task exports renquant_pipeline itself depends on."""
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
            {"renquant_common.decision_ledger": None},
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
            "renquant_common.decision_ledger.connect",
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
            "renquant_common.decision_ledger.connect",
        ) as mock_connect, patch(
            "renquant_common.decision_ledger.write_verdicts",
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
            "renquant_common.decision_ledger.connect",
        ) as mock_connect, patch(
            "renquant_common.decision_ledger.write_verdicts",
        ) as mock_write:
            mock_connect.return_value = MagicMock()
            task.run(ctx)

            written = mock_write.call_args[1]["verdicts"]
            assert len(written) == 2
            assert written[0]["gate"] == "conviction"
            assert written[1]["verdict"] == "block"
