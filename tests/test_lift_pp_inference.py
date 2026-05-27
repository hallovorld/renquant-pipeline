"""Integration parity test for the InferencePipeline assembly (functional-lift).

pp_inference.py orders the whole decision tree into phases (regime → drawdown →
gates → sell → candidates → ranking → rotation → selection → QP → veto →
score-distribution). This test proves the assembly imports cleanly and binds to
the LIFTED Jobs in renquant_pipeline.kernel — i.e. the decision tree is wired
from the lifted code, not the umbrella.

NOT exercised here: a full end-to-end .run(). The sell/buy paths unconditionally
invoke MetaLabelVetoTask and PanelScoringJob, which cross the model-scoring
boundary (xgboost). Panel scoring is already routed through the load_scorer path
(renquant_pipeline.panel_scoring); meta_label still needs the load_scorer
treatment — that is the model-integration cutover (separate track). Running the
pipeline end-to-end is pinned there.
"""
from __future__ import annotations

import importlib

pp = importlib.import_module("renquant_pipeline.kernel.pipeline.pp_inference")


def test_pipelines_construct() -> None:
    assert pp.InferencePipeline() is not None
    assert pp.SellOnlyPipeline() is not None


def test_assembly_binds_to_lifted_jobs() -> None:
    """The module-level Job names resolve to the lifted kernel.pipeline Jobs."""
    from renquant_pipeline.kernel.pipeline.job_regime import RegimeJob
    from renquant_pipeline.kernel.pipeline.job_sell import TickerSellJob
    from renquant_pipeline.kernel.pipeline.job_selection import SelectionJob
    from renquant_pipeline.kernel.pipeline.job_joint_actions import JointActionJob

    assert pp.RegimeJob is RegimeJob
    assert pp.TickerSellJob is TickerSellJob
    assert pp.SelectionJob is SelectionJob
    assert pp.JointActionJob is JointActionJob


def test_panel_scoring_routed_through_load_scorer_path() -> None:
    """Panel scoring must use the RFC load_scorer path, not kernel.panel_pipeline."""
    import ast
    import inspect

    src = inspect.getsource(pp)
    assert "from renquant_pipeline.panel_scoring import PanelScoringJob" in src
    # No actual IMPORT statement (comments excluded) may reach into panel_pipeline.
    offenders = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module and "panel_pipeline" in node.module:
            offenders.append(node.module)
        elif isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if "panel_pipeline" in a.name]
    assert offenders == [], (
        f"InferencePipeline must not import the xgboost-direct panel_pipeline: {offenders}; "
        "panel scoring goes through renquant_pipeline.panel_scoring (load_scorer)."
    )
