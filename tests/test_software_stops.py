"""S-FRAC stage 3 (core, sprint D2) — software-stop layer tests, pipeline side.

Companion to renquant-orchestrator's ``backtesting/renquant_104/tests/test_software_stops.py``
(the Phase-1 byte-equivalent-mirror pair for
``kernel/pipeline/task_software_stops.py``). The registry itself
(``adapters.software_stops.SoftwareStopRegistry``) lives only in the umbrella
(no ``adapters/`` mirror in this repo — the S-FRAC RunnerAdapter/registry glue
is umbrella-only), so this file exercises ``SoftwareStopExitTask`` and the
``kernel.exit_types``/``pp_inference`` wiring against a FAKE registry object
(duck-typed: ``is_armed()`` + ``evaluate(prices)``) instead of the real one —
``SoftwareStopExitTask`` never imports the registry class directly, only
reads ``ctx.software_stops`` via ``getattr``, so this is a faithful test of
its actual contract.

Design: renquant-orchestrator doc/design/2026-07-02-s-frac-fractional-v2.md
§3.2 (registry + sell-only-loop delta).
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

from renquant_pipeline.kernel.exit_types import (
    META_LABEL_VETO_ELIGIBLE,
    PANEL_VETO_BYPASS,
    PER_BAR_CAP_EXEMPT,
    POST_STOP_COOLDOWN_TRIGGERS,
)
from renquant_pipeline.kernel.pipeline.pp_inference import SellOnlyPipeline
from renquant_pipeline.kernel.pipeline.task_software_stops import SoftwareStopExitTask


class _FakeRegistry:
    """Minimal duck-typed stand-in for adapters.software_stops.SoftwareStopRegistry."""

    def __init__(self, *, armed, breaches=None):
        self._armed = armed
        self._breaches = breaches or []

    def is_armed(self):
        return self._armed

    def evaluate(self, prices):  # noqa: ARG002 — fixed breach list per test
        return self._breaches


def _ctx(registry, prices):
    return SimpleNamespace(software_stops=registry, prices=dict(prices), exits=[])


class TestTaxonomyMembership:
    def test_software_stop_bypasses_veto_and_cap_but_not_meta_label_eligible(self):
        """A software stop is a stop: bypasses panel veto + per-bar cap,
        triggers the post-stop re-entry blackout, and is NOT meta-label
        vetoable (only canonical core types are)."""
        assert "software_stop" in PANEL_VETO_BYPASS
        assert "software_stop" in PER_BAR_CAP_EXEMPT
        assert "software_stop" in POST_STOP_COOLDOWN_TRIGGERS
        assert "software_stop" not in META_LABEL_VETO_ELIGIBLE


class TestSellOnlyLoopWiring:
    def test_breach_appends_software_stop_exit(self):
        reg = _FakeRegistry(
            armed=True,
            breaches=[{"symbol": "BLK", "qty": 0.35, "reason": "software_stop breach: price 700.0 <= stop 760.0"}],
        )
        ctx = _ctx(reg, {"BLK": 700.0})
        SoftwareStopExitTask().run(ctx)
        assert len(ctx.exits) == 1
        ticker, sig = ctx.exits[0]
        assert ticker == "BLK"
        assert sig.should_exit is True
        assert sig.exit_type == "software_stop"
        assert sig.quantity == 0.35
        assert "software_stop breach" in sig.reason

    def test_no_breach_no_exit(self):
        reg = _FakeRegistry(armed=True, breaches=[])
        ctx = _ctx(reg, {"BLK": 800.0})
        SoftwareStopExitTask().run(ctx)
        assert ctx.exits == []

    def test_unarmed_registry_is_loud_noop(self, caplog):
        reg = _FakeRegistry(armed=False)
        ctx = _ctx(reg, {"BLK": 1.0})
        with caplog.at_level("ERROR", logger="kernel.pipeline"):
            SoftwareStopExitTask().run(ctx)
        assert ctx.exits == []
        assert "NOT armed" in caplog.text

    def test_sell_only_pipeline_runs_task_after_veto_and_cap(self):
        """Source-order pin: the software-stop pass runs AFTER the
        meta-label veto and the per-bar sell cap (a broker-resident stop
        can't be vetoed or capped; nor can its software mirror). Mirrors
        the umbrella's identical assertion on its own SellOnlyPipeline."""
        src = inspect.getsource(SellOnlyPipeline.run)
        i_veto = src.index("MetaLabelVetoTask().run")
        i_cap = src.index("LimitSellsPerBarTask().run")
        i_sw = src.index("SoftwareStopExitTask().run")
        assert i_veto < i_sw
        assert i_cap < i_sw


class TestFlagOffInert:
    def test_task_noop_without_registry(self, tmp_path):
        """Flag-off byte-inertness on the sell-only loop: no registry on
        ctx ⇒ no exits appended, nothing written anywhere."""
        for ctx in (
            SimpleNamespace(prices={"BLK": 1.0}, exits=[]),           # attr absent
            SimpleNamespace(software_stops=None,
                            prices={"BLK": 1.0}, exits=[]),           # attr None
        ):
            task = SoftwareStopExitTask()
            assert task.should_skip(ctx) is True
            task.run(ctx)
            assert ctx.exits == []
        assert list(tmp_path.iterdir()) == []          # no state file created
