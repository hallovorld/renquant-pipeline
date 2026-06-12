"""GateRegistry writer migration #4 — kernel/panel_pipeline fail-closed
helpers dual-write (eng plan S2-PR4; pipeline-repo copy of the umbrella
mirror migrated in RenQuant#316). Same-repo import — direct, no degrade.
Post-retirement (errata C(iii)): helpers submit-only; PanelScoringJob.run
applies the aggregate at the job boundary. skip_buys keeps its direct
effect (position mechanics, not the admission lattice)."""
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
        assert not ctx.buy_blocked, "helper must not write the flag directly"
        assert ctx.skip_buys
        assert _rows(ctx)[0]["gate"] == "panel_scoring_fail_closed"
        assert ctx.gate_registry.blocked("book")

    def test_calibrator_fail_closed(self):
        ctx = _ctx()
        _fail_closed_missing_calibrator(ctx, "calibrator_missing")
        assert not ctx.buy_blocked and ctx.gate_registry.blocked("book")
        assert _rows(ctx)[0]["gate"] == "calibrator_fail_closed"

    def test_ngboost_fail_closed(self):
        ctx = _ctx()
        _fail_closed_ngboost(ctx, "ngb_artifact_unreadable", detail="bad json")
        assert not ctx.buy_blocked and ctx.gate_registry.blocked("book")
        row = _rows(ctx)[0]
        assert row["gate"] == "ngboost_fail_closed"
        assert row["inputs"]["detail"] == "bad json"


class TestChokePoint:

    def test_job_applies_aggregate_after_fail_closed(self):
        from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
            PanelScoringJob,
        )

        ctx = _ctx(
            candidates=[SimpleNamespace(ticker="MU")],
            holdings={},
            config={"ranking": {"panel_scoring": {"enabled": True}}},
        )
        # No artifact configured → LoadScorerTask fail-closes mid-chain;
        # the job boundary must still land the aggregate on the flag.
        PanelScoringJob().run(ctx)
        assert ctx.buy_blocked
        assert ctx.gate_registry.blocked("book")
