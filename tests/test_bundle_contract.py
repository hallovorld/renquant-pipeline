"""GOAL-5 AC4 phase 2 — public pair-validation API (RFC RenQuant#492 §2.5).

Five surfaces under test:

1. the contract fixture vectors (``tests/fixtures/bundle_contract/
   vectors.json`` — the file phase 3 promotes to renquant-common so both
   sides of the artifacts seam pin the SAME verdict semantics);
2. runtime equivalence — the public verdict agrees, case by case, with
   the actual runtime binding check
   (``job_panel_scoring._assert_calibrator_matches_scorer`` fed by the
   real ``GlobalPanelCalibration.load`` and the ``PanelScorer.load``
   metadata construction), plus is-identity assertions that the runtime
   path is BOUND to the shared helpers rather than a divergent copy;
3. import-lightness — ``import renquant_pipeline.bundle_contract`` in a
   fresh interpreter pulls no runtime-only heavy deps (xgboost/torch/
   cvxpy/…) and no pipeline runtime modules. pandas/numpy/scipy are
   ALLOWED residuals: they come from ``renquant_common.__init__`` (eager
   in the shared dep renquant-artifacts already carries), not from this
   package;
4. verdict falsy semantics against the artifacts-store seam rule
   (renquant-artifacts#25 ``BundleStore._run_pair_validator``: raise /
   ``False`` / ``.ok`` falsy ⇒ reject), including an end-to-end publish
   through the real ``BundleStore`` with ``pair_validator=validate_pair``;
5. rejection-path edge cases (schema guard, member-set guard, unreadable
   members, corrupt v1 stamps, flag-off version gap).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import warnings
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

import renquant_common.model_fingerprint as shared
from renquant_pipeline.bundle_contract import (
    BUNDLE_CONTRACT_VERSION,
    CALIBRATOR_MEMBER,
    REASON_CALIBRATOR_STAMP_INVALID,
    REASON_CROSS_SCHEMA_REFUSED,
    REASON_FINGERPRINT_MISMATCH,
    REASON_MANIFEST_SCHEMA_UNSUPPORTED,
    REASON_MEMBER_INVALID,
    REASON_MEMBER_MISSING,
    REASON_MEMBER_UNREADABLE,
    REASON_MISSING_BINDING,
    REASON_SCORER_KIND_UNSUPPORTED,
    REASON_SCORER_STAMP_INVALID,
    REASON_VERSION_GAP,
    SCORER_MEMBER,
    PairVerdict,
    validate_pair,
)
from renquant_pipeline.kernel.panel_pipeline import fingerprint_dispatch as fd
from renquant_pipeline.kernel.panel_pipeline import job_panel_scoring as jps
from renquant_pipeline.kernel.panel_pipeline.global_calibrator import (
    GlobalPanelCalibration,
)

VECTORS_PATH = Path(__file__).parent / "fixtures" / "bundle_contract" / "vectors.json"
VECTORS = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
CASES = {case["name"]: case for case in VECTORS["cases"]}
CASE_NAMES = sorted(CASES)


def _serialize(payload: dict) -> bytes:
    """The pinned member serialization declared in vectors.json."""
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _materialize(tmp_path: Path, case: dict) -> dict[str, Path]:
    scorer_path = tmp_path / SCORER_MEMBER
    scorer_path.write_bytes(_serialize(case["scorer_payload"]))
    calibrator_path = tmp_path / CALIBRATOR_MEMBER
    calibrator_path.write_bytes(_serialize(case["calibrator_payload"]))
    return {SCORER_MEMBER: scorer_path, CALIBRATOR_MEMBER: calibrator_path}


def _validate_case(tmp_path: Path, case: dict) -> PairVerdict:
    member_paths = _materialize(tmp_path, case)
    return validate_pair(
        case["manifest"],
        member_paths,
        accept_legacy_stamps=case["accept_legacy_stamps"],
    )


# ---------------------------------------------------------------------------
# 1. Contract fixture vectors
# ---------------------------------------------------------------------------

def test_vectors_file_pins_this_contract_version() -> None:
    assert VECTORS["contract_version"] == BUNDLE_CONTRACT_VERSION
    assert VECTORS["scorer_member"] == SCORER_MEMBER
    assert VECTORS["calibrator_member"] == CALIBRATOR_MEMBER
    assert set(CASES) == {
        "matching_pair_legacy_schema",
        "matching_pair_v1_schema",
        "mismatched_pair",
        "missing_binding",
        "cross_schema_comparison_refused",
    }


@pytest.mark.parametrize("name", CASE_NAMES)
def test_vector_manifest_digests_are_true(tmp_path: Path, name: str) -> None:
    """The fixture manifests carry REAL digests for the pinned member
    serialization (so the same vectors drive store-level digest tests when
    promoted to renquant-common)."""
    case = CASES[name]
    member_paths = _materialize(tmp_path, case)
    for member, entry in case["manifest"]["members"].items():
        raw = member_paths[member].read_bytes()
        assert sha256(raw).hexdigest() == entry["sha256"], member
        assert len(raw) == entry["bytes"], member


@pytest.mark.parametrize("name", CASE_NAMES)
def test_vector_verdicts(tmp_path: Path, name: str) -> None:
    case = CASES[name]
    verdict = _validate_case(tmp_path, case)
    expected = case["expected"]
    assert verdict.ok is expected["ok"], verdict
    assert verdict.matched_schema == expected["matched_schema"], verdict
    assert list(verdict.reason_codes) == expected["reason_codes"], verdict
    assert verdict.contract_version == BUNDLE_CONTRACT_VERSION


# ---------------------------------------------------------------------------
# 2. Runtime equivalence
# ---------------------------------------------------------------------------

def test_runtime_path_is_bound_to_the_shared_helpers() -> None:
    """The refactor exports the runtime matcher's claim construction, it
    does not fork it: the names the runtime binding check calls ARE the
    fingerprint_dispatch helpers (is-identity, mirroring
    test_model_content_sha256_shared's pin against triple-impl drift)."""
    assert jps._fingerprint_values is fd.fingerprint_values_from_metadata
    assert jps._scorer_claim_from_metadata is fd.scorer_claim_from_metadata
    assert jps._calibrator_claim_from_metadata is fd.calibrator_claim_from_metadata
    assert jps._match_fingerprint_claims is fd.match_claims


def _runtime_scorer_metadata(scorer_path: Path) -> dict:
    """Exactly the ``PanelScorer.load`` default-branch metadata steps."""
    payload = json.loads(scorer_path.read_text(encoding="utf-8"))
    meta = {k: v for k, v in payload.items() if k != "booster_raw_json"}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        meta = shared.stamp_artifact_metadata(meta, scorer_path, payload=payload)
    return fd.resolve_scorer_stamp_metadata(
        meta, payload, scorer_path, context="test-runtime-equivalence",
    )


def _runtime_ctx(scorer_meta: dict, *, accept_legacy: bool) -> SimpleNamespace:
    return SimpleNamespace(
        _panel_scorer=SimpleNamespace(metadata=scorer_meta),
        config={
            "ranking": {"panel_scoring": {"fingerprint": {
                "accept_legacy_stamps": accept_legacy,
            }}},
        },
    )


@pytest.mark.parametrize("name", CASE_NAMES)
def test_public_verdict_matches_runtime_matcher(tmp_path: Path, name: str) -> None:
    """Public API accept/reject == the runtime loader's binding check on
    the same pair, with the real calibrator loader on the calibrator side."""
    case = CASES[name]
    member_paths = _materialize(tmp_path, case)
    verdict = validate_pair(
        case["manifest"], member_paths,
        accept_legacy_stamps=case["accept_legacy_stamps"],
    )

    scorer_meta = _runtime_scorer_metadata(member_paths[SCORER_MEMBER])
    calibrator = GlobalPanelCalibration.load(member_paths[CALIBRATOR_MEMBER])
    ctx = _runtime_ctx(scorer_meta, accept_legacy=case["accept_legacy_stamps"])

    raised: ValueError | None = None
    try:
        jps._assert_calibrator_matches_scorer(
            ctx, calibrator, member_paths[CALIBRATOR_MEMBER], strict=True,
        )
    except ValueError as exc:
        raised = exc

    assert (raised is None) is verdict.ok, (
        f"public verdict {verdict!r} disagrees with runtime matcher "
        f"({'no raise' if raised is None else raised})"
    )
    if raised is not None:
        message = str(raised)
        if verdict.reason_codes == (REASON_MISSING_BINDING,):
            assert "missing scorer/calibrator fingerprint" in message
        elif verdict.reason_codes == (REASON_CROSS_SCHEMA_REFUSED,):
            assert "route=cross-schema" in message
        elif verdict.reason_codes == (REASON_VERSION_GAP,):
            assert "route=version-gap" in message
        else:
            assert verdict.reason_codes == (REASON_FINGERPRINT_MISMATCH,)
            assert "fingerprint mismatch" in message


def test_flag_off_equivalence_on_legacy_pair(tmp_path: Path) -> None:
    """accept_legacy_stamps=False: both surfaces refuse the versionless
    pair with the version-gap remedy (the M6 step-4 flag flip)."""
    case = CASES["matching_pair_legacy_schema"]
    member_paths = _materialize(tmp_path, case)
    verdict = validate_pair(
        case["manifest"], member_paths, accept_legacy_stamps=False,
    )
    assert not verdict.ok
    assert verdict.reason_codes == (REASON_VERSION_GAP,)

    scorer_meta = _runtime_scorer_metadata(member_paths[SCORER_MEMBER])
    calibrator = GlobalPanelCalibration.load(member_paths[CALIBRATOR_MEMBER])
    ctx = _runtime_ctx(scorer_meta, accept_legacy=False)
    with pytest.raises(ValueError, match="route=version-gap"):
        jps._assert_calibrator_matches_scorer(
            ctx, calibrator, member_paths[CALIBRATOR_MEMBER], strict=True,
        )


# ---------------------------------------------------------------------------
# 3. Import-lightness
# ---------------------------------------------------------------------------

#: Runtime-only heavy roots the public API must NOT pull (renquant_common's
#: eager __init__ residuals — pandas/numpy/scipy — are documented-allowed).
_FORBIDDEN_ROOTS = (
    "xgboost", "torch", "transformers", "lightgbm", "catboost", "cvxpy",
    "renquant_artifacts", "renquant_base_data",
)
#: Pipeline runtime modules that must stay un-imported (the lazy package
#: __init__ is what keeps them out — this pins the laziness too).
_FORBIDDEN_MODULES = (
    "renquant_pipeline.inference",
    "renquant_pipeline.panel_scoring",
    "renquant_pipeline.kernel.pipeline",
    "renquant_pipeline.kernel.panel_pipeline.job_panel_scoring",
    "renquant_pipeline.kernel.panel_pipeline.panel_scorer",
)


def test_bundle_contract_import_is_light() -> None:
    code = (
        "import json, sys\n"
        "import renquant_pipeline.bundle_contract\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, check=True,
    )
    modules = set(json.loads(proc.stdout))
    offenders = sorted(
        m for m in modules
        if m.split(".", 1)[0] in _FORBIDDEN_ROOTS or m in _FORBIDDEN_MODULES
    )
    assert offenders == [], (
        "renquant_pipeline.bundle_contract must stay import-light — the "
        "artifacts-store publisher imports it (RFC §2.5); offending "
        f"imports: {offenders}"
    )


# ---------------------------------------------------------------------------
# 4. Verdict semantics at the artifacts-store seam
# ---------------------------------------------------------------------------

def _store_seam_rejects(verdict: object) -> bool:
    """Verbatim replica of renquant-artifacts#25
    ``BundleStore._run_pair_validator``'s acceptance rule (sans raise)."""
    if verdict is False:
        return True
    if verdict is not None and hasattr(verdict, "ok"):
        return not bool(verdict.ok)
    return False


def test_pair_verdict_falsy_semantics() -> None:
    accept = PairVerdict(ok=True, matched_schema="v1")
    reject = PairVerdict(ok=False, reason_codes=("fingerprint_mismatch",))
    assert bool(accept) and accept.ok
    assert not bool(reject) and not reject.ok
    assert _store_seam_rejects(reject)
    assert not _store_seam_rejects(accept)


def test_artifacts_store_publish_gated_by_validate_pair(tmp_path: Path) -> None:
    """End-to-end through the REAL phase-1 store: a matching pair
    publishes; the orphaned-binding shape is refused BEFORE the pointer
    flip (writer step 6). Phase 3 makes this wiring the production
    default; here it proves the seam contract."""
    bundle_store = pytest.importorskip(
        "renquant_artifacts.bundle_store",
        reason="requires renquant-artifacts with the phase-1 bundle store (#25)",
    )
    store = bundle_store.BundleStore(
        tmp_path / "store",
        pair_validator=validate_pair,
        local_mount_guard=lambda p: (True, "test-injected"),
    )
    authorization = {
        "tool": "wf_promote",
        "tool_version": "1.0.0",
        "actor": {"os_user": "test", "operator": "renhao"},
        "source": {"wf_run_id": "wf-2026-07-18", "verdict_id": "PASS-001"},
        "inputs": {"panel": "sha256:" + "0" * 64},
    }
    bindings = {"phase3": "bindings content pinned by the phase-3 binding step"}

    good = CASES["matching_pair_legacy_schema"]
    result = store.publish(
        {
            SCORER_MEMBER: _serialize(good["scorer_payload"]),
            CALIBRATOR_MEMBER: _serialize(good["calibrator_payload"]),
        },
        bindings=bindings,
        authorization=authorization,
    )
    assert result.manifest.bundle_id

    bad = CASES["mismatched_pair"]
    with pytest.raises(bundle_store.BundleValidationError, match="pair validator"):
        store.publish(
            {
                SCORER_MEMBER: _serialize(bad["scorer_payload"]),
                CALIBRATOR_MEMBER: _serialize(bad["calibrator_payload"]),
            },
            bindings=bindings,
            authorization=authorization,
        )
    # The refused bundle left no directory behind and ACTIVE still points
    # at the good bundle.
    resolved = store.resolve_active()
    assert resolved.manifest.bundle_id == result.manifest.bundle_id


# ---------------------------------------------------------------------------
# 5. Rejection-path edges (fail-closed guards)
# ---------------------------------------------------------------------------

def test_unknown_manifest_schema_fails_closed(tmp_path: Path) -> None:
    case = CASES["matching_pair_legacy_schema"]
    member_paths = _materialize(tmp_path, case)
    verdict = validate_pair({"schema_version": 2, "members": {}}, member_paths)
    assert verdict.reason_codes == (REASON_MANIFEST_SCHEMA_UNSUPPORTED,)


def test_wrong_member_set_rejected(tmp_path: Path) -> None:
    case = CASES["matching_pair_legacy_schema"]
    member_paths = _materialize(tmp_path, case)
    manifest = json.loads(json.dumps(case["manifest"]))
    manifest["members"]["extra.json"] = {"sha256": "0" * 64, "bytes": 1}
    verdict = validate_pair(manifest, member_paths)
    assert verdict.reason_codes == (REASON_MEMBER_INVALID,)


def test_missing_member_path_rejected(tmp_path: Path) -> None:
    case = CASES["matching_pair_legacy_schema"]
    member_paths = _materialize(tmp_path, case)
    del member_paths[CALIBRATOR_MEMBER]
    verdict = validate_pair(case["manifest"], member_paths)
    assert verdict.reason_codes == (REASON_MEMBER_MISSING,)


def test_unreadable_member_rejected(tmp_path: Path) -> None:
    case = CASES["matching_pair_legacy_schema"]
    member_paths = _materialize(tmp_path, case)
    member_paths[SCORER_MEMBER].write_text("{not json", encoding="utf-8")
    verdict = validate_pair(case["manifest"], member_paths)
    assert verdict.reason_codes == (REASON_MEMBER_UNREADABLE,)


def test_wrong_calibrator_kind_rejected(tmp_path: Path) -> None:
    case = CASES["matching_pair_legacy_schema"]
    member_paths = _materialize(tmp_path, case)
    payload = json.loads(json.dumps(case["calibrator_payload"]))
    payload["kind"] = "something_else"
    member_paths[CALIBRATOR_MEMBER].write_bytes(_serialize(payload))
    verdict = validate_pair(case["manifest"], member_paths)
    assert verdict.reason_codes == (REASON_MEMBER_INVALID,)


def test_non_default_scorer_kind_fails_closed(tmp_path: Path) -> None:
    case = CASES["matching_pair_legacy_schema"]
    member_paths = _materialize(tmp_path, case)
    payload = json.loads(json.dumps(case["scorer_payload"]))
    payload["kind"] = "panel_lgbm"
    member_paths[SCORER_MEMBER].write_bytes(_serialize(payload))
    verdict = validate_pair(case["manifest"], member_paths)
    assert verdict.reason_codes == (REASON_SCORER_KIND_UNSUPPORTED,)


def test_scorer_without_booster_rejected(tmp_path: Path) -> None:
    case = CASES["matching_pair_legacy_schema"]
    member_paths = _materialize(tmp_path, case)
    payload = json.loads(json.dumps(case["scorer_payload"]))
    del payload["booster_raw_json"]
    member_paths[SCORER_MEMBER].write_bytes(_serialize(payload))
    verdict = validate_pair(case["manifest"], member_paths)
    assert verdict.reason_codes == (REASON_MEMBER_INVALID,)


def test_tampered_v1_stamp_rejected(tmp_path: Path) -> None:
    """A v1-stamped scorer whose content no longer reproduces its stamp is
    corrupt (fail-closed at runtime load; MismatchError)."""
    case = CASES["matching_pair_v1_schema"]
    member_paths = _materialize(tmp_path, case)
    payload = json.loads(json.dumps(case["scorer_payload"]))
    payload["feature_cols"] = ["a", "b", "tampered"]
    member_paths[SCORER_MEMBER].write_bytes(_serialize(payload))
    verdict = validate_pair(case["manifest"], member_paths)
    assert verdict.reason_codes == (REASON_SCORER_STAMP_INVALID,)


def test_malformed_calibrator_v1_declaration_rejected(tmp_path: Path) -> None:
    """scorer_fingerprint_schema_version=1 with no v1 digest is a
    malformed stamp — fail closed, never coerced to legacy."""
    case = CASES["matching_pair_v1_schema"]
    member_paths = _materialize(tmp_path, case)
    payload = json.loads(json.dumps(case["calibrator_payload"]))
    payload["metadata"] = {"scorer_fingerprint_schema_version": 1}
    member_paths[CALIBRATOR_MEMBER].write_bytes(_serialize(payload))
    verdict = validate_pair(case["manifest"], member_paths)
    assert verdict.reason_codes == (REASON_CALIBRATOR_STAMP_INVALID,)


def test_v1_digest_mismatch_reports_mismatch(tmp_path: Path) -> None:
    case = CASES["matching_pair_v1_schema"]
    member_paths = _materialize(tmp_path, case)
    payload = json.loads(json.dumps(case["calibrator_payload"]))
    payload["metadata"]["scorer_model_content_fingerprint"] = "e" * 64
    member_paths[CALIBRATOR_MEMBER].write_bytes(_serialize(payload))
    verdict = validate_pair(case["manifest"], member_paths)
    assert verdict.reason_codes == (REASON_FINGERPRINT_MISMATCH,)
    assert verdict.matched_schema is None
