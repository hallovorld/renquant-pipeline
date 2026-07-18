"""Public serving-pair validation API — GOAL-5 AC4 phase 2 (RFC RenQuant#492 §2.5).

``validate_pair(manifest, member_paths) -> PairVerdict`` is the VERSIONED
public boundary the transactional bundle store (renquant-artifacts#25,
``BundleStore(pair_validator=...)``) invokes at writer-protocol step 6
BEFORE publication, and the pipeline reader's binding check enforces at
serve time. Internally it is the SAME matching logic as the runtime
loader's calibrator↔scorer matcher
(``job_panel_scoring._assert_calibrator_matches_scorer`` — the
2026-05-27/06-22/07-01/07-14→16 incident site): claim construction and
schema dispatch live once in
``kernel.panel_pipeline.fingerprint_dispatch`` (M6 rule: a legacy vs v1
identity pair is compared within ONE schema only, never across), consumed
by BOTH the runtime path and this API — exported deliberately, not
re-implemented (RFC §2.5, review finding 5: the private matcher is not
importable by umbrella code; this module is the sanctioned surface).

Import-lightness contract: this module (and the package ``__init__`` it
forces) must stay importable with stdlib +
``renquant_common.model_fingerprint`` only — no pandas/numpy/xgboost/
torch/cvxpy and no renquant_artifacts import (the artifacts store imports
US; ``tests/test_bundle_contract.py`` pins this in a subprocess).

Scope notes (v1 of this contract):

* Pair CONSISTENCY only — RFC §2.7: a passing verdict asserts the
  calibrator was fitted to this scorer, NOT that the pair passed the WF
  gate; buy admission stays preflight P-WF-GATE's job.
* Member digests, the exact-member-set rule, and manifest field closure
  are the artifacts store's schema checks (writer step 6 re-reads and
  verifies digests before calling us); ``manifest`` is consulted here for
  ``schema_version`` and the member-name → role mapping only. Pinning the
  ``bindings`` block content against member content is the phase-3
  binding step.
* The scorer member is validated under the runtime default (XGBoost LTR
  JSON) loader semantics — the production serving pair. Artifact kinds
  the runtime routes to OTHER loaders (``panel_transformer``,
  ``panel_lgbm``, ``panel_linear``) fail closed with
  ``scorer_kind_unsupported`` rather than guessing metadata semantics
  this contract version does not pin.
* Full load-viability (booster deserialization, calibration spline
  arrays) is NOT checked — that needs runtime-only heavy deps; the
  runtime reader remains fail-closed behind this validator.
"""
from __future__ import annotations

import json
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

# Light by construction: renquant_common.model_fingerprint is stdlib-only,
# and fingerprint_dispatch imports nothing heavier (the panel_pipeline
# package __init__ is lazy for exactly this reason).
from renquant_common.model_fingerprint import stamp_artifact_metadata

from renquant_pipeline.kernel.panel_pipeline.fingerprint_dispatch import (
    ACCEPT_LEGACY_STAMPS_DEFAULT,
    calibrator_claim_from_metadata,
    log_verify_telemetry,
    match_claims,
    resolve_scorer_stamp_metadata,
    scorer_claim_from_metadata,
)

#: Version of THIS validation contract (bump on any semantic change; the
#: renquant-common contract fixtures pin verdict semantics per version).
BUNDLE_CONTRACT_VERSION = 1

#: Bundle schema v1 member roles (RFC §2.2 — the EXACT member set).
SCORER_MEMBER = "panel-ltr.alpha158_fund.json"
CALIBRATOR_MEMBER = "panel-rank-calibration.json"

#: Scorer artifact kinds the runtime routes AWAY from the default XGBoost
#: JSON loader (``PanelScorer.load`` dispatch) — not pinned by contract v1.
_UNSUPPORTED_SCORER_KINDS = frozenset(
    {"panel_transformer", "panel_lgbm", "panel_linear"}
)

# ---------------------------------------------------------------------------
# Reason codes — stable strings, part of the versioned contract.
# ---------------------------------------------------------------------------
REASON_MANIFEST_SCHEMA_UNSUPPORTED = "manifest_schema_unsupported"
REASON_MEMBER_MISSING = "member_missing"
REASON_MEMBER_UNREADABLE = "member_unreadable"
REASON_MEMBER_INVALID = "member_invalid"
REASON_SCORER_KIND_UNSUPPORTED = "scorer_kind_unsupported"
REASON_SCORER_STAMP_INVALID = "scorer_stamp_invalid"
REASON_CALIBRATOR_STAMP_INVALID = "calibrator_stamp_invalid"
REASON_MISSING_BINDING = "missing_binding"
REASON_CROSS_SCHEMA_REFUSED = "cross_schema_refused"
REASON_VERSION_GAP = "version_gap"
REASON_FINGERPRINT_MISMATCH = "fingerprint_mismatch"

#: Matched-schema values on an accepting verdict (== the dispatch routes).
MATCHED_SCHEMA_V1 = "v1"
MATCHED_SCHEMA_LEGACY = "legacy"


@dataclass(frozen=True)
class PairVerdict:
    """Structured verdict for the artifacts-store seam.

    The store's acceptance rule (renquant-artifacts#25
    ``BundleStore._run_pair_validator``) is: raise ⇒ reject, ``False`` ⇒
    reject, ``.ok`` falsy ⇒ reject — so ``ok`` is the load-bearing field
    and ``bool(verdict)`` mirrors it.

    ``matched_schema`` is the ONE schema the identity pair matched under
    (``"v1"`` or ``"legacy"``) when ``ok``; ``None`` otherwise —
    cross-schema comparison is refused by construction (M6 dispatch rule),
    never reported as a match.
    """

    ok: bool
    reason_codes: tuple[str, ...] = ()
    matched_schema: str | None = None
    detail: str = ""
    contract_version: int = field(default=BUNDLE_CONTRACT_VERSION)

    def __bool__(self) -> bool:
        return self.ok


def _reject(code: str, detail: str) -> PairVerdict:
    return PairVerdict(ok=False, reason_codes=(code,), detail=detail)


def _member_path(
    member_paths: Mapping[str, Any], name: str
) -> "tuple[Path, None] | tuple[None, PairVerdict]":
    raw = member_paths.get(name)
    if raw is None:
        return None, _reject(
            REASON_MEMBER_MISSING,
            f"member_paths has no entry for required member {name!r}",
        )
    path = Path(raw)
    if not path.is_file():
        return None, _reject(
            REASON_MEMBER_MISSING, f"member {name!r} not found at {path}",
        )
    return path, None


def _member_payload(
    path: Path, name: str
) -> "tuple[dict, None] | tuple[None, PairVerdict]":
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, _reject(
            REASON_MEMBER_UNREADABLE,
            f"member {name!r} at {path} is not readable JSON: {exc}",
        )
    if not isinstance(payload, dict):
        return None, _reject(
            REASON_MEMBER_INVALID,
            f"member {name!r} at {path} is not a JSON object",
        )
    return payload, None


def _scorer_metadata(
    payload: dict, path: Path
) -> "tuple[dict, None] | tuple[None, PairVerdict]":
    """Build the scorer's runtime metadata exactly as ``PanelScorer.load``
    does on its default (XGBoost LTR JSON) branch, minus the booster
    deserialization: legacy stamp/recompute via the renquant-common shim,
    then the M6 both-identities resolution (which ``verify()``s a
    v1-stamped payload fail-closed)."""
    kind = payload.get("kind")
    if isinstance(kind, str) and kind in _UNSUPPORTED_SCORER_KINDS:
        return None, _reject(
            REASON_SCORER_KIND_UNSUPPORTED,
            f"scorer kind {kind!r} is loaded by a non-default runtime loader; "
            f"bundle contract v{BUNDLE_CONTRACT_VERSION} pins only the "
            "default XGBoost LTR JSON semantics",
        )
    if "booster_raw_json" not in payload:
        # Runtime PanelScorer.load raises KeyError here — a bundle whose
        # scorer cannot load would fail-close the serve path; refuse to
        # publish it.
        return None, _reject(
            REASON_MEMBER_INVALID,
            f"scorer member at {path} has no 'booster_raw_json' — not a "
            "loadable panel-LTR artifact",
        )
    meta = {k: v for k, v in payload.items() if k != "booster_raw_json"}
    with warnings.catch_warnings():
        # The shim's DeprecationWarning is the migration-window contract
        # (same suppression as fingerprint_dispatch's legacy recompute);
        # values are identical to the runtime's un-suppressed call.
        warnings.simplefilter("ignore", DeprecationWarning)
        meta = stamp_artifact_metadata(meta, path, payload=payload)
    try:
        meta = resolve_scorer_stamp_metadata(
            meta, payload, path, context="bundle_contract.validate_pair",
        )
    except ValueError as exc:
        # A v1-stamped scorer whose content does not reproduce its own
        # stamp is corrupt regardless of any flag (fail-closed at runtime
        # load; MismatchError/VersionGapError/UnclassifiedKeyError).
        return None, _reject(
            REASON_SCORER_STAMP_INVALID,
            f"scorer stamp verification failed for {path}: {exc}",
        )
    return meta, None


def validate_pair(
    manifest: Mapping[str, Any],
    member_paths: Mapping[str, Any],
    *,
    accept_legacy_stamps: "bool | None" = None,
) -> PairVerdict:
    """Validate that a bundle's calibrator was fitted to its scorer.

    Parameters mirror the artifacts-store seam
    (``pair_validator(manifest_payload, member_paths)``): ``manifest`` is
    the manifest payload mapping (RFC §2.2), ``member_paths`` maps member
    name → filesystem path of the staged member.

    ``accept_legacy_stamps`` is the M6 migration-window flag
    (``ranking.panel_scoring.fingerprint.accept_legacy_stamps`` — policy
    owned by strategy config). The runtime resolves it from the strategy
    config; a store-side caller without one gets the same window default
    (``True``) the runtime uses, so publication acceptance can never be
    LOOSER than serve-time acceptance under default policy. Pass the
    resolved config value when available.

    Never raises on a validation outcome — every rejection is a structured
    ``PairVerdict`` with stable ``reason_codes`` (the store treats a raise
    as reject too, but the contract fixtures pin the structured shape).
    """
    accept_legacy = (
        ACCEPT_LEGACY_STAMPS_DEFAULT
        if accept_legacy_stamps is None
        else bool(accept_legacy_stamps)
    )

    schema_version = manifest.get("schema_version")
    if schema_version != 1:
        return _reject(
            REASON_MANIFEST_SCHEMA_UNSUPPORTED,
            f"bundle manifest schema_version {schema_version!r} is not "
            f"supported by bundle contract v{BUNDLE_CONTRACT_VERSION} "
            "(fail-closed on unknown schemas)",
        )
    members = manifest.get("members")
    if not isinstance(members, Mapping) or set(members) != {
        SCORER_MEMBER, CALIBRATOR_MEMBER,
    }:
        return _reject(
            REASON_MEMBER_INVALID,
            "manifest members must be exactly "
            f"{{{SCORER_MEMBER!r}, {CALIBRATOR_MEMBER!r}}} (RFC §2.2), got "
            f"{sorted(members) if isinstance(members, Mapping) else members!r}",
        )

    scorer_path, err = _member_path(member_paths, SCORER_MEMBER)
    if err is not None:
        return err
    calibrator_path, err = _member_path(member_paths, CALIBRATOR_MEMBER)
    if err is not None:
        return err

    scorer_payload, err = _member_payload(scorer_path, SCORER_MEMBER)
    if err is not None:
        return err
    calibrator_payload, err = _member_payload(calibrator_path, CALIBRATOR_MEMBER)
    if err is not None:
        return err

    if calibrator_payload.get("kind") != "global_panel_calibration":
        # Runtime GlobalPanelCalibration.load raises here → fail-closed.
        return _reject(
            REASON_MEMBER_INVALID,
            f"calibrator member at {calibrator_path} has kind "
            f"{calibrator_payload.get('kind')!r}, expected "
            "'global_panel_calibration'",
        )

    scorer_meta, err = _scorer_metadata(scorer_payload, scorer_path)
    if err is not None:
        return err
    calibrator_meta = dict(calibrator_payload.get("metadata") or {})

    try:
        scorer_claim = scorer_claim_from_metadata(scorer_meta)
    except ValueError as exc:
        return _reject(
            REASON_SCORER_STAMP_INVALID,
            f"scorer identity claim invalid for {scorer_path}: {exc}",
        )
    try:
        calibrator_claim = calibrator_claim_from_metadata(calibrator_meta)
    except ValueError as exc:
        return _reject(
            REASON_CALIBRATOR_STAMP_INVALID,
            f"calibrator identity claim invalid for {calibrator_path}: {exc}",
        )

    if not scorer_claim.values or not calibrator_claim.values:
        # The runtime's "missing scorer/calibrator fingerprint" fail-close.
        return _reject(
            REASON_MISSING_BINDING,
            "missing scorer/calibrator fingerprint: "
            f"scorer={list(scorer_claim.values)!r} "
            f"calibrator={list(calibrator_claim.values)!r} — refit or "
            "re-stamp the calibrator with scorer_model_content_fingerprint",
        )

    verdict = match_claims(
        scorer_claim, calibrator_claim, accept_legacy=accept_legacy,
    )
    log_verify_telemetry(
        "bundle_contract.validate_pair", scorer_path, scorer_claim,
        calibrator_claim, verdict, accept_legacy=accept_legacy,
    )
    if verdict.matched:
        return PairVerdict(
            ok=True,
            matched_schema=verdict.route,
            detail=verdict.reason,
        )
    if verdict.route == "cross-schema":
        code = REASON_CROSS_SCHEMA_REFUSED
    elif verdict.route == "version-gap":
        code = REASON_VERSION_GAP
    else:
        code = REASON_FINGERPRINT_MISMATCH
    return _reject(
        code,
        f"calibrator/scorer fingerprint verdict: route={verdict.route}: "
        f"{verdict.reason}. calibrator={list(calibrator_claim.values)} "
        f"scorer={list(scorer_claim.values)}",
    )


__all__ = [
    "BUNDLE_CONTRACT_VERSION",
    "CALIBRATOR_MEMBER",
    "MATCHED_SCHEMA_LEGACY",
    "MATCHED_SCHEMA_V1",
    "PairVerdict",
    "REASON_CALIBRATOR_STAMP_INVALID",
    "REASON_CROSS_SCHEMA_REFUSED",
    "REASON_FINGERPRINT_MISMATCH",
    "REASON_MANIFEST_SCHEMA_UNSUPPORTED",
    "REASON_MEMBER_INVALID",
    "REASON_MEMBER_MISSING",
    "REASON_MEMBER_UNREADABLE",
    "REASON_MISSING_BINDING",
    "REASON_SCORER_KIND_UNSUPPORTED",
    "REASON_SCORER_STAMP_INVALID",
    "REASON_VERSION_GAP",
    "SCORER_MEMBER",
    "validate_pair",
]
