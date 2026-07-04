"""No-re-fork guard for the shared fingerprint implementation (M6).

Root cause of the recurring fail-closed no-trade incidents (2026-05-27,
06-22, 07-01): three repos hand-copied `model_content_sha256` with
DIFFERENT included/excluded field sets, so a calibrator fit by one side
could never match the runtime check of another — by construction.

Fix: ONE implementation in `renquant_common.model_fingerprint`, imported
everywhere. M6 stage-2 step-1 moved the pipeline onto the schema-v1 API
(`stamp()`/`verify()` + version dispatch in
`kernel/panel_pipeline/fingerprint_dispatch.py`), so this test now pins:

  1. is-identity of the v1 API re-exports (`is`, not value-equality) —
     both on `panel_scorer` (the pipeline's import surface) and on
     `fingerprint_dispatch` (the dispatch module) — so nobody can silently
     reintroduce a diverging local copy;
  2. is-identity of the deprecated 0.8.1 shim names — they remain the
     legacy route of the migration window and are removed at stage-2
     step 5 (delete those pins in the same PR that bumps renquant-common
     to 0.10);
  3. a FROZEN legacy test-vector (from the renquant-common#21 fixtures,
     ground truth computed by executing the actual 0.8.1 implementation)
     and a FROZEN v1 test-vector for the same payload — the two schemas'
     outputs are pinned byte-for-byte AND pinned to differ (an accidental
     re-unification would mean one side's semantics silently moved).
"""
from __future__ import annotations

import pytest

import renquant_common.model_fingerprint as shared
from renquant_pipeline.kernel.panel_pipeline import (
    fingerprint_dispatch,
    panel_scorer,
)

# ---------------------------------------------------------------------------
# Frozen test-vectors. LEGACY is the renquant-common#21 fixture ground truth
# (computed by executing the actual 0.8.1 implementation, commit b96d190).
# V1 pins the schema-v1 canonicalization for the same payload. Do NOT "fix"
# a failing hash by updating a constant — a divergence means a semantics
# change reached a live verifier route.
# ---------------------------------------------------------------------------
REF_LEGACY_CONTENT = "sha256:a64b282442ceeb767846f29251305467dd727a91344a30e74cb0cb8ba4a87322"
REF_V1_CONTENT = "sha256:0f6dc00a4e14906032451d614c662870a3f16beae252104839529826ca1ed7e3"


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


def test_v1_api_reexports_are_the_shared_objects() -> None:
    """Not copies — the SAME function objects, imported. This is what
    structurally guarantees fit-time and runtime agree, forever."""
    assert panel_scorer.model_content_sha256 is shared.model_content_sha256
    assert panel_scorer.stamp is shared.stamp
    assert panel_scorer.verify is shared.verify
    assert panel_scorer.artifact_sha256 is shared.artifact_sha256
    assert panel_scorer.FINGERPRINT_SCHEMA_VERSION is shared.FINGERPRINT_SCHEMA_VERSION


def test_dispatch_module_uses_the_shared_objects() -> None:
    """The dispatch module (both fail-closed binding checks route through
    it) must import, never re-implement (design §5 row 3)."""
    assert fingerprint_dispatch.model_content_sha256 is shared.model_content_sha256
    assert fingerprint_dispatch.stamp is shared.stamp
    assert fingerprint_dispatch.verify is shared.verify
    assert fingerprint_dispatch.artifact_sha256 is shared.artifact_sha256
    assert (
        fingerprint_dispatch.model_content_sha256_from_path
        is shared.model_content_sha256_from_path
    )
    for err in ("FingerprintError", "MismatchError", "UnclassifiedKeyError",
                "VersionGapError"):
        assert getattr(fingerprint_dispatch, err) is getattr(shared, err)


def test_legacy_shim_reexports_are_the_shared_objects() -> None:
    """The 0.8.1 shim surface — the migration window's legacy route.

    REMOVE these pins in the stage-2 step-5 PR (renquant-common 0.10 shim
    removal + pipeline cap bump), not before.
    """
    assert panel_scorer.stamp_artifact_metadata is shared.stamp_artifact_metadata
    assert panel_scorer.model_content_sha256_from_path is shared.model_content_sha256_from_path
    assert panel_scorer._MUTABLE_ARTIFACT_KEYS is shared.MUTABLE_ARTIFACT_KEYS
    assert panel_scorer._PREDICTIVE_CONTENT_HINTS is shared.PREDICTIVE_CONTENT_HINTS


def test_pipeline_entry_point_matches_shared_function_on_fixture_payload() -> None:
    payload = _payload()
    assert panel_scorer.model_content_sha256(payload) == shared.model_content_sha256(payload)


def test_frozen_v1_vector() -> None:
    payload = _payload()
    assert shared.model_content_sha256(payload) == REF_V1_CONTENT
    fields = shared.stamp(payload)
    assert fields["model_content_fingerprint"] == REF_V1_CONTENT
    assert fields["fingerprint_schema_version"] == shared.FINGERPRINT_SCHEMA_VERSION


def test_frozen_legacy_vector_survives_until_step5(tmp_path) -> None:
    """The legacy route must keep producing the 0.8.1 ground-truth hash
    for as long as the shims exist (live legacy-stamped artifacts verify
    through it during the migration window)."""
    import json

    p = tmp_path / "artifact.json"
    p.write_text(json.dumps(_payload(), sort_keys=True))
    with pytest.warns(DeprecationWarning):
        assert shared.model_content_sha256_from_path(p) == REF_LEGACY_CONTENT


def test_the_two_schemas_are_pinned_to_differ() -> None:
    """v1 ≠ legacy on real payload shapes (label_col moved to PREDICTIVE,
    canonicalization replaced default=str). If this ever fails, one
    schema's semantics silently moved onto the other — investigate before
    touching any constant."""
    assert REF_V1_CONTENT != REF_LEGACY_CONTENT
