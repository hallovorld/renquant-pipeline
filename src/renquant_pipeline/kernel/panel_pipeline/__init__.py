"""Panel-LTR inference kernel package — parallel to kernel/pipeline/.

Swaps the per-ticker `ScoreBuyTask` + `RankingJob` pair for a single
cross-sectional `ComputePanelScoresTask` + `PanelRankGateTask` pair
driven by a trained `PanelLTRModel` artifact.

This package is `common/`-free so it can run under LEAN and the live
runner. The training side lives in `training_panel/` (notebook / cron).

Entry points::

    from renquant_pipeline.kernel.panel_pipeline import (
        PanelScorer,               # thin loader around the saved artifact
        compute_panel_scores,      # pure function: (artifact, feature_matrix) → scores
        top_n_by_score,            # rank gate: keep top-N
        probability_gate,          # rank gate: keep score ≥ threshold
    )
"""
from __future__ import annotations

# Lazy (PEP 562) — importing a SUBMODULE of this package (e.g.
# ``renquant_pipeline.kernel.panel_pipeline.fingerprint_dispatch``) forces
# Python to run this __init__.py first, regardless of what that submodule
# itself needs. panel_scorer/feature_matrix/job_panel_scoring all pull in
# xgboost transitively; fingerprint_dispatch does not. Eager imports here
# turned "import fingerprint_dispatch" into a hard xgboost dependency for
# every caller, including backtesting's WF verification path, which has no
# other reason to need it (renquant-backtesting#64 review). Attribute
# access below still works exactly as documented; only the *type* of import
# (name access vs. module load) changed.
_LAZY = {
    "PanelScorer": (".panel_scorer", "PanelScorer"),
    "compute_panel_scores": (".panel_scorer", "compute_panel_scores"),
    "top_n_by_score": (".panel_scorer", "top_n_by_score"),
    "probability_gate": (".panel_scorer", "probability_gate"),
    "GlobalPanelCalibration": (".global_calibrator", "GlobalPanelCalibration"),
    "build_inference_matrix": (".feature_matrix", "build_inference_matrix"),
    "run_panel_inference": (".feature_matrix", "run_panel_inference"),
    "PanelScoringJob": (".job_panel_scoring", "PanelScoringJob"),
    "LoadScorerTask": (".job_panel_scoring", "LoadScorerTask"),
    "BuildFeatureMatrixTask": (".job_panel_scoring", "BuildFeatureMatrixTask"),
    "ApplyScoresTask": (".job_panel_scoring", "ApplyScoresTask"),
    "VetoWeakBuysTask": (".job_panel_scoring", "VetoWeakBuysTask"),
    "LoadGlobalCalibrationTask": (".job_panel_scoring", "LoadGlobalCalibrationTask"),
    "ApplyGlobalCalibrationTask": (".job_panel_scoring", "ApplyGlobalCalibrationTask"),
    "LoadNGBoostTask": (".job_panel_scoring", "LoadNGBoostTask"),
    "ApplyNGBoostTask": (".job_panel_scoring", "ApplyNGBoostTask"),
}

__all__ = list(_LAZY)


def __getattr__(name: str):
    try:
        module_name, attr_name = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    import importlib

    module = importlib.import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value  # cache on the package module, PEP 562 style
    return value


def __dir__():
    return sorted(list(globals()) + list(_LAZY))
