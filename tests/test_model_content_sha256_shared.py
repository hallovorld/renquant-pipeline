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

import json

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
    structurally guarantees fit-time and runtime agree, forever.

    2026-07-02 M6 unification (renquant-common#19/#20): the shared module's
    public API settled on PREDICTIVE_KEYS/OPERATIONAL_KEYS (named ancestors
    of this repo's old _PREDICTIVE_CONTENT_HINTS/_MUTABLE_ARTIFACT_KEYS, per
    the shared module's own docstring) and a minimal `stamp()`. This repo's
    `stamp_artifact_metadata`/`model_content_sha256_from_path` are now LOCAL
    composition helpers (never re-exported by renquant_common) — pinned
    below by delegation, not object identity.
    """
    assert panel_scorer.model_content_sha256 is shared.model_content_sha256
    assert panel_scorer.artifact_sha256 is shared.artifact_sha256
    assert panel_scorer._MUTABLE_ARTIFACT_KEYS is shared.OPERATIONAL_KEYS
    assert panel_scorer._PREDICTIVE_CONTENT_HINTS is shared.PREDICTIVE_KEYS


def test_pipeline_entry_point_matches_shared_function_on_fixture_payload() -> None:
    payload = _payload()
    assert panel_scorer.model_content_sha256(payload) == shared.model_content_sha256(payload)


def test_stamp_artifact_metadata_delegates_to_shared_stamp(tmp_path) -> None:
    """The local composition helper must not reimplement the fingerprint —
    its stamped fields must equal calling the shared `stamp()` directly."""
    payload = _payload()
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text("irrelevant file content for artifact_sha256")
    public_payload = {k: v for k, v in payload.items() if k != "booster_raw_json"}

    meta = panel_scorer.stamp_artifact_metadata(public_payload, artifact_path, payload=payload)

    expected_stamp = shared.stamp(payload)
    assert meta["model_content_fingerprint"] == expected_stamp["model_content_fingerprint"]
    assert meta["fingerprint_schema_version"] == expected_stamp["fingerprint_schema_version"]
    assert meta["artifact_sha256"] == shared.artifact_sha256(artifact_path)
    # public_payload's own fields are preserved in the merged metadata.
    assert meta["label_col"] == payload["label_col"]


def test_stamp_artifact_metadata_defaults_payload_to_public_payload(tmp_path) -> None:
    """HFPatchTSTPanelScorer's call site passes only one payload dict (no
    separate `payload=`) — confirm the fingerprint is computed over that
    same dict in that case, not silently over an empty/wrong payload."""
    payload = _payload()
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text("irrelevant file content for artifact_sha256")

    meta = panel_scorer.stamp_artifact_metadata(payload, artifact_path)

    assert meta["model_content_fingerprint"] == shared.model_content_sha256(payload)


def test_model_content_sha256_from_path_hashes_json_artifact_content(tmp_path) -> None:
    payload = _payload()
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(json.dumps(payload))

    assert panel_scorer.model_content_sha256_from_path(artifact_path) == shared.model_content_sha256(payload)


def test_model_content_sha256_from_path_falls_back_to_artifact_sha256_for_non_json(tmp_path) -> None:
    non_json_path = tmp_path / "artifact.pt"
    non_json_path.write_bytes(b"\x00\x01not json")

    assert panel_scorer.model_content_sha256_from_path(non_json_path) == shared.artifact_sha256(non_json_path)
