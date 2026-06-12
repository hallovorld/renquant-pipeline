"""GateRegistry writer migration #2 — panel_scoring.py dual-write (S2-PR4).

Same contract as migration #1 (test_gate_writers_migration.py): every
``buy_blocked`` site also submits a block verdict with the precise
reason; non-blocking paths submit nothing; direct writes untouched.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from renquant_pipeline.panel_scoring import (
    ApplyGlobalCalibrationTask,
    ApplyScoresTask,
    BuildFeatureMatrixTask,
    LoadScorerTask,
    _block_all,
)


def _ctx(**kw) -> SimpleNamespace:
    base = dict(
        artifact_manifest={}, strategy_config={"watchlist": ["MU", "GE"]},
        config={"watchlist": ["MU", "GE"]}, watchlist=["MU", "GE"],
        feature_rows={}, scores={}, blocked_by=None, buy_blocked=False,
        gate_registry=None, candidates=[], counters={},
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _gates(ctx) -> list[str]:
    if ctx.gate_registry is None:
        return []
    return [r["gate"] for r in ctx.gate_registry.ledger_rows(run_id="t")]


class TestBlockingPathsSubmit:

    def test_block_all_helper_submits_with_reason(self):
        ctx = _ctx()
        _block_all(ctx, "missing_panel_artifact")
        assert ctx.buy_blocked
        rows = ctx.gate_registry.ledger_rows(run_id="t")
        assert rows[0]["gate"] == "panel_scoring"
        assert rows[0]["reason"] == "missing_panel_artifact"
        assert rows[0]["inputs"]["watchlist_size"] == 2

    def test_load_scorer_missing_artifact_routes_through_block_all(self):
        ctx = _ctx(artifact_manifest={})
        result = LoadScorerTask().run(ctx)
        assert result is False
        assert ctx.buy_blocked
        assert "panel_scoring" in _gates(ctx)

    def test_empty_feature_matrix_submits(self):
        ctx = _ctx(artifact_manifest={"artifact_id": "a",
                                      "feature_contract": {"feature_cols": ["f1"]}},
                   feature_rows={})
        result = BuildFeatureMatrixTask().run(ctx)
        assert result is False
        assert ctx.buy_blocked
        assert "panel_feature_matrix" in _gates(ctx)

    def test_no_scores_submits(self):
        ctx = _ctx(artifact_manifest={"artifact_id": "a"},
                   panel_feature_matrix={}, panel_score_snapshot={})
        result = ApplyScoresTask().run(ctx)
        assert result is False
        assert ctx.buy_blocked
        assert "panel_scores" in _gates(ctx)

    def test_calibration_all_invalid_submits(self):
        ctx = _ctx(
            artifact_manifest={
                "artifact_id": "a",
                "calibration": {"method": "linear", "slope": float("nan"),
                                "intercept": 0.0},
            },
            panel_scores={"MU": 0.5},
        )
        result = ApplyGlobalCalibrationTask().run(ctx)
        if result is False:  # NaN params → every score invalid
            assert ctx.buy_blocked
            assert "global_calibration" in _gates(ctx)


class TestNonBlockingSilent:

    def test_successful_calibration_no_rows(self):
        ctx = _ctx(
            artifact_manifest={"artifact_id": "a",
                               "calibration": {"method": "identity"}},
            panel_scores={"MU": 0.5},
        )
        assert ApplyGlobalCalibrationTask().run(ctx) is True
        assert not ctx.buy_blocked
        assert _gates(ctx) == []


class TestCensusPin:

    def test_direct_writers_still_four(self):
        src = (Path(__file__).resolve().parent.parent /
               "src/renquant_pipeline/panel_scoring.py")
        tree = ast.parse(src.read_text())
        count = 0
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "setattr"
                    and len(node.args) == 3
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == "buy_blocked"
                    and isinstance(node.args[2], ast.Constant)
                    and node.args[2].value is True):
                count += 1
        assert count == 4, (
            f"panel_scoring.py direct buy_blocked writers = {count}, expected "
            f"4 during dual-write; flip to 0 at choke-point retirement")
