"""M6 stage-2 step-1: schema-version-dispatched fingerprint verification.

Design: renquant-orchestrator
``doc/design/2026-07-03-m6-stage2-fingerprint-migration.md`` §3 step 1.
This module is the ONE place the pipeline decides how a scorer/calibrator
identity pair is compared during the M6 migration window. Both fail-closed
binding checks route through it:

* ``job_panel_scoring.py::_assert_calibrator_matches_scorer`` (the strict
  daily buy path — the 2026-05-27/06-22/07-01 incident site);
* ``walk_forward/loader.py::_assert_calibrator_matches_entry`` (the WF
  per-fold contract behind ``weekly_wf_promote``).

The dispatch rule (design §3, "never an OR-accepting window")
--------------------------------------------------------------
Every stamp carries (or lacks) ``fingerprint_schema_version``; a verifier
verifies each artifact under the ONE semantics its stamp declares — a
versionless stamp IS the legacy (renquant-common 0.8.1) declaration, since
all 0.8.1 stamps predate the version field by construction. An identity
pair is compared WITHIN one schema only:

* v1-stamped scorer identity vs v1-declared calibrator identity: exact
  digest equality (no prefix acceptance, no multi-key list) — the scorer's
  v1 stamp itself is verified against its payload via
  ``renquant_common.model_fingerprint.verify()`` where the payload is
  available (``PanelScorer.load`` / the WF loader's fold read).
* versionless vs versionless: the legacy shim equality path — the
  pre-existing multi-key list + 12-char-prefix acceptance, byte-for-byte
  the behavior live artifacts verified under before this module existed
  (that heterogeneous acceptance dies with the flag at step 4, design §5
  row 6).
* cross-schema: NEVER a match. A v1 mismatch can never hide behind a
  passing legacy hash because no artifact is ever evaluated under both.

The migration window is controlled by ONE explicit flag:
``ranking.panel_scoring.fingerprint.accept_legacy_stamps`` (default
``true`` during the window, policy owned by strategy config per #210 §6).
With the flag ``false`` only the v1 route exists and a versionless stamp
fails closed with the explicit "re-stamp under v1" remedy
(:class:`~renquant_common.model_fingerprint.VersionGapError` semantics).

UNSTAMPED artifacts (no ``model_content_fingerprint`` at all) fall back to
the pre-existing recompute behavior — with the venv-coupled bare
``model_content_sha256`` name replaced by the EXPLICIT pair (legacy shim
recompute + v1 recompute), so the fallback identity no longer silently
changes semantics when the venv's renquant-common version changes (the
#160 problem). Post step-0 the production inventory carries stamps
(47/47, census-enforced); unstamped is a dev/test-fixture state only, and
it fails closed once the flag flips.

Hash logic is IMPORTS ONLY from ``renquant_common.model_fingerprint``
(the triple-impl lesson, design §5 row 3). Nothing is re-implemented
here; ``tests/test_model_content_sha256_shared.py`` pins the is-identity.
"""
from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

# IMPORTS ONLY — the shared fingerprint implementation. Do not re-fork any
# of this logic locally (design §5 row 3); the is-identity test pins these.
from renquant_common.model_fingerprint import (  # noqa: F401
    FINGERPRINT_SCHEMA_VERSION,
    FingerprintError,
    MismatchError,
    UnclassifiedKeyError,
    VersionGapError,
    artifact_sha256,
    model_content_sha256,
    model_content_sha256_from_path,
    stamp,
    verify,
)

log = logging.getLogger(__name__)

#: Route names (telemetry + verdict reporting).
SCHEMA_V1 = "v1"
SCHEMA_LEGACY = "legacy"

#: The one dual-accept flag (design §3 step 1). Policy lives in strategy
#: config (strategy-104), enforcement here. Default ``True`` = the
#: migration window (legacy-stamped population verifies unchanged).
ACCEPT_LEGACY_STAMPS_DEFAULT = True

#: In-memory scorer-metadata keys stamped by ``resolve_scorer_stamp_metadata``
#: (divergence telemetry, the stage-1 shadow analog). Distinct from every
#: key ``_fingerprint_values`` collects, so the v1 recompute can never leak
#: into a legacy-route identity list (that would be the OR this design
#: forbids).
META_V1_RECOMPUTE = "model_content_fingerprint_v1_recompute"
META_V1_RECOMPUTE_ERROR = "model_content_fingerprint_v1_recompute_error"
META_LEGACY_RECOMPUTE = "model_content_fingerprint_legacy_recompute"

_VERSION_GAP_REMEDY = (
    "re-stamp the artifact under fingerprint schema "
    f"v{FINGERPRINT_SCHEMA_VERSION} (an auditable operation — the stage-2 "
    "re-stamp tool) — do not treat this as a content mismatch"
)


def accept_legacy_stamps(config: Mapping[str, Any] | None) -> bool:
    """Read ``ranking.panel_scoring.fingerprint.accept_legacy_stamps``.

    Default ``True`` (the migration window) when the key or any ancestor
    is absent — flag-off is an explicit strategy-config decision (design
    §3 step 4), never an accident of a missing key.
    """
    if not isinstance(config, Mapping):
        return ACCEPT_LEGACY_STAMPS_DEFAULT
    node: Any = config
    for key in ("ranking", "panel_scoring", "fingerprint"):
        if not isinstance(node, Mapping):
            return ACCEPT_LEGACY_STAMPS_DEFAULT
        node = node.get(key, {})
    if not isinstance(node, Mapping):
        return ACCEPT_LEGACY_STAMPS_DEFAULT
    value = node.get("accept_legacy_stamps", ACCEPT_LEGACY_STAMPS_DEFAULT)
    return bool(value)


# ---------------------------------------------------------------------------
# Identity claims
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IdentityClaim:
    """One side's declared scorer identity, under exactly one schema.

    ``schema`` is :data:`SCHEMA_V1` (a versioned stamp: exactly one
    acceptable digest) or :data:`SCHEMA_LEGACY` (a versionless stamp or
    recompute fallback: the pre-existing multi-value list).
    """

    schema: str
    values: tuple[str, ...]
    source: str

    @property
    def empty(self) -> bool:
        return not self.values


def build_claim(
    *,
    schema_version: Any,
    v1_value: Any,
    legacy_values: Iterable[str],
    source: str,
) -> IdentityClaim:
    """Classify one side's identity declaration into a single-schema claim.

    ``schema_version is None`` ⇒ a versionless declaration ⇒ the legacy
    claim (design §3: "a versionless stamp IS the legacy declaration").
    ``schema_version == 1`` ⇒ the v1 claim, whose ONLY acceptable value is
    the declared v1 digest. Any other version value (including bool /
    non-int malformations and future versions) fails closed with the
    version-gap remedy — never coerced, never guessed.
    """
    if schema_version is None:
        return IdentityClaim(
            schema=SCHEMA_LEGACY,
            values=tuple(str(v) for v in legacy_values if v),
            source=source,
        )
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != FINGERPRINT_SCHEMA_VERSION
    ):
        raise ValueError(
            f"{source}: fingerprint schema version gap: declared "
            f"{schema_version!r} but this pipeline implements "
            f"v{FINGERPRINT_SCHEMA_VERSION}; {_VERSION_GAP_REMEDY}."
        )
    if not v1_value:
        raise ValueError(
            f"{source}: fingerprint_schema_version="
            f"{FINGERPRINT_SCHEMA_VERSION} declared but no v1 content "
            "fingerprint value present — a malformed stamp fails closed; "
            f"{_VERSION_GAP_REMEDY}."
        )
    return IdentityClaim(
        schema=SCHEMA_V1, values=(str(v1_value),), source=source,
    )


# ---------------------------------------------------------------------------
# Matching (verbatim-preserved legacy helpers + the v1 exact route)
# ---------------------------------------------------------------------------

def normalize_fingerprint(value: str | None) -> str:
    return str(value or "").strip().lower().removeprefix("sha256:")


def fingerprints_match(expected: str | None, actual: str | None) -> bool:
    """LEGACY route only: exact match OR historical 12-char-sha prefixes.

    Byte-for-byte the pre-dispatch behavior (job_panel_scoring/loader both
    carried a copy). Never used on the v1 route — design §5 row 6: no
    prefix acceptance on v1.
    """
    exp = normalize_fingerprint(expected)
    act = normalize_fingerprint(actual)
    if not exp or not act:
        return False
    if exp == act:
        return True
    min_prefix = 12
    return (
        len(exp) >= min_prefix
        and len(act) >= min_prefix
        and (exp.startswith(act) or act.startswith(exp))
    )


def any_fingerprints_match(expected: list[str], actual: list[str]) -> bool:
    return any(
        fingerprints_match(exp, act)
        for exp in expected
        for act in actual
    )


def _v1_digests_equal(a: str, b: str) -> bool:
    """v1 route: exact full-digest equality (prefix-insensitive only to the
    ``sha256:`` wrapper + case), never a length-prefix match."""
    na, nb = normalize_fingerprint(a), normalize_fingerprint(b)
    return bool(na) and na == nb


@dataclass(frozen=True)
class MatchVerdict:
    matched: bool
    route: str          # "v1" | "legacy" | "cross-schema" | "version-gap"
    reason: str


def match_claims(
    scorer: IdentityClaim,
    calibrator: IdentityClaim,
    *,
    accept_legacy: bool,
) -> MatchVerdict:
    """Compare an identity pair within ONE schema (design §3 step 1).

    Never raises on a plain mismatch — callers own their fail-closed
    ``ValueError`` messages (stable for downstream matching); the verdict
    carries the dispatch route + reason for those messages and telemetry.
    """
    if not accept_legacy:
        versionless = [
            c.source for c in (scorer, calibrator) if c.schema != SCHEMA_V1
        ]
        if versionless:
            return MatchVerdict(
                matched=False,
                route="version-gap",
                reason=(
                    "accept_legacy_stamps=false: versionless (legacy) "
                    f"identity from {', '.join(versionless)} is no longer "
                    f"acceptable; {_VERSION_GAP_REMEDY}"
                ),
            )
    if scorer.schema != calibrator.schema:
        return MatchVerdict(
            matched=False,
            route="cross-schema",
            reason=(
                f"cross-schema identity pair ({scorer.source}="
                f"{scorer.schema}-declared, {calibrator.source}="
                f"{calibrator.schema}-declared) is never compared across "
                "schemas (design §3: one acceptable hash per artifact); "
                "re-stamp the lagging side under "
                f"v{FINGERPRINT_SCHEMA_VERSION} (step-2 ordering: scorer "
                "artifacts first, then calibrators)"
            ),
        )
    if scorer.schema == SCHEMA_V1:
        matched = _v1_digests_equal(scorer.values[0], calibrator.values[0])
        return MatchVerdict(
            matched=matched,
            route=SCHEMA_V1,
            reason="v1 exact-digest comparison"
            + ("" if matched else ": digests differ"),
        )
    matched = any_fingerprints_match(
        list(calibrator.values), list(scorer.values),
    )
    return MatchVerdict(
        matched=matched,
        route=SCHEMA_LEGACY,
        reason="legacy shim equality path (multi-key list + historical "
               "prefix acceptance)"
        + ("" if matched else ": no identity in common"),
    )


def log_verify_telemetry(
    context: str,
    artifact: Any,
    scorer: IdentityClaim,
    calibrator: IdentityClaim,
    verdict: MatchVerdict,
    *,
    accept_legacy: bool,
) -> None:
    """The step-1 divergence-telemetry line (one per verify).

    Step-3 census criterion (e) counts legacy-route acceptances over the
    observation window from these lines — keep the ``fingerprint-dispatch``
    prefix and ``route=`` token stable.
    """
    log.info(
        "fingerprint-dispatch verify: context=%s artifact=%s route=%s "
        "matched=%s accept_legacy_stamps=%s scorer_schema=%s "
        "calibrator_schema=%s scorer_values=%d calibrator_values=%d "
        "reason=%s",
        context, artifact, verdict.route, verdict.matched, accept_legacy,
        scorer.schema, calibrator.schema, len(scorer.values),
        len(calibrator.values), verdict.reason,
    )


# ---------------------------------------------------------------------------
# Scorer-side stamp resolution (PanelScorer.load / the WF fold read)
# ---------------------------------------------------------------------------

def _legacy_recompute_from_path(path: "str | Path") -> str | None:
    """Legacy (0.8.1) content recompute via the deprecated shim.

    Telemetry + unstamped-fallback use only. The shim's DeprecationWarning
    is silenced here because using the shim IS the migration-window
    contract (verbatim 0.8.1 semantics; removed at step 5).
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return model_content_sha256_from_path(path)
    except Exception:  # noqa: BLE001 — recompute is best-effort here
        return None


def _v1_recompute(payload: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """(digest, error) — the v1 recompute or the classified failure."""
    try:
        return model_content_sha256(dict(payload)), None
    except FingerprintError as exc:
        detail = f"{type(exc).__name__}: {exc}"
        if len(detail) > 300:
            detail = detail[:297] + "..."
        return None, detail


def resolve_scorer_stamp_metadata(
    meta: dict,
    payload: Mapping[str, Any],
    path: "str | Path",
    *,
    context: str,
) -> dict:
    """Stamp BOTH identities into the in-memory scorer metadata + telemetry.

    Design §3 step 1: the legacy identity is already present (the 0.9.1
    ``stamp_artifact_metadata`` shim keeps the artifact's stamped value, or
    the legacy recompute for an unstamped artifact); this adds the v1
    recompute (when computable) and the legacy recompute under
    telemetry-only keys, then logs the divergence line.

    For a v1-STAMPED artifact (``fingerprint_schema_version`` present) the
    stamp is verified against the payload via ``verify()`` — fail-closed:
    a v1-stamped artifact whose content does not reproduce its own stamp
    is corrupt regardless of any flag (``MismatchError``/
    ``VersionGapError``/``UnclassifiedKeyError`` are ``ValueError``
    subclasses, so existing fail-closed handling applies). A LEGACY stamp
    mismatching its recompute is telemetry only — enforcement stays at the
    binding checks, exactly as before this module existed.
    """
    stamped_value = meta.get("model_content_fingerprint")
    stamped_version = payload.get("fingerprint_schema_version")
    if stamped_version is not None:
        # Verifies version validity AND content — raises on any gap.
        verify(dict(payload), str(payload.get("model_content_fingerprint")),
               stamped_version)
    v1_digest, v1_error = _v1_recompute(payload)
    if v1_digest is not None:
        meta[META_V1_RECOMPUTE] = v1_digest
    elif v1_error is not None:
        meta[META_V1_RECOMPUTE_ERROR] = v1_error
    legacy_digest = _legacy_recompute_from_path(path)
    if legacy_digest is not None:
        meta[META_LEGACY_RECOMPUTE] = legacy_digest
    stamped_schema = (
        SCHEMA_V1 if stamped_version is not None
        else (SCHEMA_LEGACY if "model_content_fingerprint" in payload
              else "unstamped")
    )
    log.info(
        "fingerprint-dispatch load: context=%s artifact=%s "
        "stamped_schema=%s stamped=%s legacy_recompute=%s v1_recompute=%s "
        "stamp_eq_legacy=%s stamp_eq_v1=%s%s",
        context, path, stamped_schema, stamped_value, legacy_digest,
        v1_digest,
        fingerprints_match(stamped_value, legacy_digest)
        if stamped_value and legacy_digest else "n/a",
        _v1_digests_equal(str(stamped_value), str(v1_digest))
        if stamped_value and v1_digest else "n/a",
        f" v1_error={v1_error}" if v1_error else "",
    )
    return meta


def scorer_claim_from_payload(
    payload: Mapping[str, Any],
    path: "str | Path | None",
    *,
    file_hash: str | None = None,
) -> IdentityClaim:
    """Identity claim for a scorer artifact read directly from disk (WF).

    Replaces the WF loader's bare-name recompute (design §3 step 1: "moves
    off the bare name onto the explicit pair"), so the fold identity no
    longer silently follows the venv's renquant-common version:

    * v1-stamped: ``verify()`` the stamp against the payload (fail-closed),
      then the ONE acceptable value is the stamp.
    * legacy-stamped (versionless): the stamped value + the other
      historical stamped-identity keys + the explicit LEGACY shim
      recompute + the whole-file hash — the pre-existing acceptance set,
      with the venv-coupled bare recompute replaced by the explicit legacy
      shim.
    * unstamped: the pre-existing fallback, made explicit + deterministic:
      BOTH recomputes (legacy shim + v1) + stamped identity keys + file
      hash. Production is never unstamped post step-0 (census-enforced);
      this state exists for dev/test fixtures and dies at flag-off.
    """
    stamped_version = payload.get("fingerprint_schema_version")
    if stamped_version is not None:
        verify(dict(payload), str(payload.get("model_content_fingerprint")),
               stamped_version)
        return IdentityClaim(
            schema=SCHEMA_V1,
            values=(str(payload["model_content_fingerprint"]),),
            source=f"scorer:{path}",
        )
    values: list[str] = []
    unstamped = "model_content_fingerprint" not in payload
    if unstamped:
        # Explicit pair (recompute fallback) — order preserves the historic
        # "content recompute first" convention.
        legacy_digest = _legacy_recompute_from_path(path) if path else None
        if legacy_digest:
            values.append(legacy_digest)
        v1_digest, _v1_err = _v1_recompute(payload)
        if v1_digest:
            values.append(v1_digest)
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    for source in (payload, metadata):
        for key in (
            "model_content_fingerprint",
            "artifact_fingerprint",
            "artifact_sha256",
            "model_fingerprint",
            "fingerprint",
        ):
            value = source.get(key)
            if value:
                values.append(str(value))
    if not unstamped and path is not None:
        # Legacy-stamped: the explicit legacy shim recompute keeps the
        # pre-existing recompute acceptance without the venv coupling.
        legacy_digest = _legacy_recompute_from_path(path)
        if legacy_digest:
            values.append(legacy_digest)
    if file_hash:
        values.append(file_hash)
    return IdentityClaim(
        schema=SCHEMA_LEGACY,
        values=tuple(dict.fromkeys(values)),
        source=f"scorer:{path}",
    )
