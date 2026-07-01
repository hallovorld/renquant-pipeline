"""Regression guard for the 2026-07-01 calibrator/scorer fingerprint fix.

Root cause: renquant-model's calibrator-fit script and renquant-pipeline's
runtime scorer-binding check each hand-copied `model_content_sha256` with
DIFFERENT included/excluded field sets, so a calibrator fit by one could
never match the runtime check by another — a monthly fail-closed incident
(2026-05-27, 2026-06-22, 2026-07-01).

Fix: both repos now import the SAME function from
`renquant_common.model_fingerprint`. This test pins two invariants:

  1. `renquant_pipeline.kernel.panel_pipeline.panel_scorer` re-exports the
     exact object from `renquant_common.model_fingerprint` (not a
     redefinition) — so nobody can silently reintroduce a diverging local
     copy without this test catching it (`is`, not just value-equality).
  2. Given a synthetic panel-LTR payload, the pipeline-side entry point
     produces the identical hash to calling the shared function directly.
"""
from __future__ import annotations

import renquant_common.model_fingerprint as shared
from renquant_pipeline.kernel.panel_pipeline import panel_scorer


def _payload() -> dict:
    return {
        "kind": "panel_ltr_xgboost",
        "version": 3,
        "feature_cols": ["a", "b", "c"],
        "params": {"objective": "rank:pairwise", "max_depth": 4},
        "booster_raw_json": '{"fake": "booster"}',
        "label_col": "fwd_60d_excess",
        "trained_date": "2026-06-01",
        "metadata": {"note": "irrelevant"},
    }


def test_panel_scorer_model_content_sha256_is_the_shared_function() -> None:
    """Not a copy — the SAME function object, imported. This is what
    structurally guarantees fit-time and runtime agree, forever."""
    assert panel_scorer.model_content_sha256 is shared.model_content_sha256
    assert panel_scorer.stamp_artifact_metadata is shared.stamp_artifact_metadata
    assert panel_scorer.artifact_sha256 is shared.artifact_sha256
    assert panel_scorer.model_content_sha256_from_path is shared.model_content_sha256_from_path
    assert panel_scorer._MUTABLE_ARTIFACT_KEYS is shared.MUTABLE_ARTIFACT_KEYS
    assert panel_scorer._PREDICTIVE_CONTENT_HINTS is shared.PREDICTIVE_CONTENT_HINTS


def test_pipeline_entry_point_matches_shared_function_on_fixture_payload() -> None:
    payload = _payload()
    assert panel_scorer.model_content_sha256(payload) == shared.model_content_sha256(payload)
