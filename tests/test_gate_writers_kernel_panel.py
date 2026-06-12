"""GateRegistry writer migration #4 — kernel/panel_pipeline fail-closed
helpers dual-write (eng plan S2-PR4; pipeline-repo copy of the umbrella
mirror migrated in RenQuant#316). Same-repo import — direct, no degrade.
Direct writes stay until these helpers get an aggregate boundary (they
are called mid-task with same-chain readers of buy_blocked/skip_buys)."""
from __future__ import annotations

from types import SimpleNamespace

from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
    _fail_closed_missing_calibrator,
    _fail_closed_ngboost,
    _fail_closed_panel_scoring,
)


def _ctx(**kw):
    base = dict(candidates=[], buy_blocked=False, skip_buys=False,
                counters={}, gate_registry=None, config={})
    base.update(kw)
    return SimpleNamespace(**base)


def _rows(ctx):
    return ctx.gate_registry.ledger_rows(run_id="t") if ctx.gate_registry else []


class TestDualWrite:

    def test_panel_scoring_fail_closed(self):
        ctx = _ctx()
        _fail_closed_panel_scoring(ctx, "panel_scorer_load_failed")
        assert ctx.buy_blocked and ctx.skip_buys
        assert _rows(ctx)[0]["gate"] == "panel_scoring_fail_closed"
        assert ctx.gate_registry.blocked("book") == ctx.buy_blocked

    def test_calibrator_fail_closed(self):
        ctx = _ctx()
        _fail_closed_missing_calibrator(ctx, "calibrator_missing")
        assert ctx.buy_blocked
        assert _rows(ctx)[0]["gate"] == "calibrator_fail_closed"

    def test_ngboost_fail_closed(self):
        ctx = _ctx()
        _fail_closed_ngboost(ctx, "ngb_artifact_unreadable", detail="bad json")
        assert ctx.buy_blocked
        row = _rows(ctx)[0]
        assert row["gate"] == "ngboost_fail_closed"
        assert row["inputs"]["detail"] == "bad json"
