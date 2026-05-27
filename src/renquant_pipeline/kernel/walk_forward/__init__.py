"""Walk-forward leakage GUARDS (pipeline subset).

Only the pure validation guards are lifted into the decision pipeline. The
model-artifact loader/manifest (``WalkForwardModelLoader`` / ``read_manifest``
/ ``WalkForwardManifest``) are intentionally NOT here: they import
``panel_pipeline.panel_scorer`` (xgboost/torch), which belongs in
renquant-model / renquant-backtesting, not the decision pipeline. Loading them
here would violate the import boundary. The QP correlation-no-leakage check
needs only ``correlation_guard``.
"""
from __future__ import annotations

from .correlation_guard import (
    assert_correlation_no_leakage,
    parse_correlation_artifact,
)
from .gmm_guard import assert_gmm_no_leakage, gmm_artifact_as_of
from .leakage_guard import assert_no_leakage
from .lean_guard import assert_lean_panel_no_leakage

__all__ = [
    "assert_no_leakage",
    "assert_lean_panel_no_leakage",
    "assert_correlation_no_leakage",
    "parse_correlation_artifact",
    "assert_gmm_no_leakage",
    "gmm_artifact_as_of",
]
