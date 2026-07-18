"""Regenerate ``vectors.json`` — the bundle-contract verdict fixtures.

GOAL-5 AC4 phase 2 (RFC RenQuant#492 §2.5): these vectors pin the verdict
semantics of ``renquant_pipeline.bundle_contract.validate_pair`` for both
sides of the artifacts-store seam. Promoting this FILE to renquant-common
(so renquant-artifacts tests the same vectors without importing pipeline
test code) is the phase-3 binding step — keep it self-contained and
JSON-serializable for that move.

Digest basis: the scorer stamps are computed by renquant-common's
fingerprint implementation (legacy 0.8.1 shim hash + schema-v1
``stamp()``), so the vectors pin against the SHARED implementation, never
a local re-fork. Member files are materialized with the pinned
serialization below; the manifest ``members`` digests are true for that
serialization.

Run from the repo root (test env on PYTHONPATH):

    python tests/fixtures/bundle_contract/generate_vectors.py
"""
from __future__ import annotations

import hashlib
import json
import warnings
from pathlib import Path

import renquant_common.model_fingerprint as shared

OUT = Path(__file__).parent / "vectors.json"

#: Pinned member-file serialization (tests materialize with EXACTLY this).
SERIALIZATION = "json.dumps(payload, sort_keys=True, indent=2) + '\\n' (utf-8)"

SCORER_MEMBER = "panel-ltr.alpha158_fund.json"
CALIBRATOR_MEMBER = "panel-rank-calibration.json"


def serialize(payload: dict) -> bytes:
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _base_scorer_payload() -> dict:
    return {
        "kind": "panel_ltr_xgboost",
        "version": 3,
        "feature_cols": ["a", "b", "c"],
        "params": {"objective": "rank:pairwise", "max_depth": 4},
        "booster_raw_json": '{"fake": "booster"}',
        "label_col": "fwd_60d_excess",
        "trained_date": "2026-06-01",
        "metadata": {"note": "bundle-contract fixture"},
    }


def _legacy_hash(payload: dict) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return shared._legacy_model_content_sha256(payload)


def _legacy_stamped_scorer() -> dict:
    payload = _base_scorer_payload()
    payload["model_content_fingerprint"] = _legacy_hash(payload)
    return payload


def _v1_stamped_scorer() -> dict:
    payload = _base_scorer_payload()
    payload.update(shared.stamp(payload))
    return payload


def _calibrator(metadata: dict) -> dict:
    return {
        "kind": "global_panel_calibration",
        "trained_date": "2026-06-02",
        "probability": {"x": [-1.0, 0.0, 1.0], "y": [0.2, 0.5, 0.8]},
        "expected_return": {"x": [-1.0, 0.0, 1.0], "y": [-0.05, 0.0, 0.05]},
        "metadata": metadata,
    }


def _manifest(scorer: dict, calibrator: dict) -> dict:
    members = {}
    for name, payload in (
        (SCORER_MEMBER, scorer), (CALIBRATOR_MEMBER, calibrator),
    ):
        raw = serialize(payload)
        members[name] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        }
    return {
        "schema_version": 1,
        "members": members,
        # Store-level fields (authorization, parent_bundle, created_at,
        # manifest_digest, bundle_id) are exercised by the artifacts-store
        # suite; the pair validator reads schema_version + members only.
        # Pinning bindings CONTENT against member content is phase 3.
        "bindings": {"phase3": "bindings content pinned by the phase-3 binding step"},
    }


def build_cases() -> list[dict]:
    legacy_scorer = _legacy_stamped_scorer()
    v1_scorer = _v1_stamped_scorer()

    legacy_cal_match = _calibrator(
        {"scorer_model_content_fingerprint": legacy_scorer["model_content_fingerprint"]}
    )
    v1_cal_match = _calibrator(
        {
            "scorer_fingerprint_schema_version": 1,
            "scorer_model_content_fingerprint": v1_scorer["model_content_fingerprint"],
        }
    )
    legacy_cal_foreign = _calibrator(
        # Full-length foreign digest: cannot prefix-match any real hash.
        {"scorer_model_content_fingerprint": "f" * 64}
    )
    cal_unbound = _calibrator({"fit_date": "2026-06-02"})
    # The no-OR trap: a versionless calibrator declaring the v1-stamped
    # scorer's LEGACY hash — a match here would be the forbidden
    # cross-schema OR-acceptance.
    legacy_cal_of_v1_scorer = _calibrator(
        {"scorer_model_content_fingerprint": _legacy_hash(_base_scorer_payload())}
    )

    def case(name, description, scorer, calibrator, expected) -> dict:
        return {
            "name": name,
            "description": description,
            "accept_legacy_stamps": True,
            "scorer_payload": scorer,
            "calibrator_payload": calibrator,
            "manifest": _manifest(scorer, calibrator),
            "expected": expected,
        }

    return [
        case(
            "matching_pair_legacy_schema",
            "versionless scorer stamp == versionless calibrator declaration "
            "(legacy shim equality route)",
            legacy_scorer,
            legacy_cal_match,
            {"ok": True, "matched_schema": "legacy", "reason_codes": []},
        ),
        case(
            "matching_pair_v1_schema",
            "v1-stamped scorer == v1-declared calibrator (exact digest route)",
            v1_scorer,
            v1_cal_match,
            {"ok": True, "matched_schema": "v1", "reason_codes": []},
        ),
        case(
            "mismatched_pair",
            "same (legacy) schema, foreign calibrator digest — the "
            "2026-05-27/06-22/07-01/07-14 orphaned-binding incident shape",
            legacy_scorer,
            legacy_cal_foreign,
            {
                "ok": False,
                "matched_schema": None,
                "reason_codes": ["fingerprint_mismatch"],
            },
        ),
        case(
            "missing_binding",
            "calibrator metadata carries no scorer identity at all",
            legacy_scorer,
            cal_unbound,
            {
                "ok": False,
                "matched_schema": None,
                "reason_codes": ["missing_binding"],
            },
        ),
        case(
            "cross_schema_comparison_refused",
            "v1-stamped scorer vs versionless calibrator declaring the "
            "scorer's LEGACY hash — never compared across schemas (no-OR)",
            v1_scorer,
            legacy_cal_of_v1_scorer,
            {
                "ok": False,
                "matched_schema": None,
                "reason_codes": ["cross_schema_refused"],
            },
        ),
    ]


def main() -> None:
    document = {
        "contract": "renquant_pipeline.bundle_contract.validate_pair",
        "contract_version": 1,
        "rfc": "RenQuant#492 §2.5 (GOAL-5 AC4 phase 2)",
        "member_serialization": SERIALIZATION,
        "scorer_member": SCORER_MEMBER,
        "calibrator_member": CALIBRATOR_MEMBER,
        "cases": build_cases(),
    }
    OUT.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(document['cases'])} cases)")


if __name__ == "__main__":
    main()
