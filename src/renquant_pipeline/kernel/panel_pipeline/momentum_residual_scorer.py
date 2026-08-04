"""Momentum-residual shadow SERVING handler — GOAL-7 slice 4b (model#197 F-1).

The s104 `momentum_residual_v0_shadow` entry (strategy-104 PR #77) pins the
one cutoff-stable file in the weekly publish set: the append-only,
digest-chained LEDGER `artifacts/momentum/momentum_artifact_ledger.jsonl`.
The declared serving contract (s104#77 narrative key; model#197 amendment 2):

  read the VERIFIED ledger tail row
    → load the dated artifact beside the ledger
      (`<ledger dir>/<cutoff_date>/momentum_residual_v0.json`, the convention
      fixed by model#197 decision 1 and the model repo's
      tools/momentum_train_run.py ARTIFACT_BASENAME)
    → verify its self-carried content_sha256 AND the ledger row's pin over it
    → serve the package's score construction for the serving date.

OWNERSHIP / BOUNDARIES. Chain math, artifact hashing, and the composite score
construction are IMPORTED from the renquant-model distribution
(`renquant_model_momentum` + `renquant_model_common.momentum_features`), never
reimplemented — the same never-copy rule the train side follows. The import is
GUARDED: when the distribution is absent (install `renquant-pipeline[momentum]`
or put the sibling renquant-model checkout's src on the path, the established
hf_patchtst precedent), the loader raises an ImportError NAMING the missing
dependency, which `ApplyShadowScoringTask` records as a load-failure FAULT —
the lane degrades visibly, nothing crashes, the primary path is untouched.

SINGLE-READ IDENTITY CLOSURE (codex CR on pipeline#253). The task certifies
the ledger's content digest via ``resolve_artifact_identity`` BEFORE calling
this loader; re-opening the live path here would open a TOCTOU window — a
weekly append landing between the two reads would serve the NEW tail under
the OLD certified digest. The loader therefore reads the live path's bytes
EXACTLY ONCE, and everything downstream — the consumed digest, the chain
verification (over a private snapshot of those same bytes, still the
package's verifier), the tail selection — derives from that one snapshot.
The digest of the bytes actually consumed is exposed as
``metadata["consumed_content_sha256"]`` (the identity recipe:
``sha256:<16 hex>``); the task refuses to cache, mark loaded, or record
health as certified unless the certified identity and the consumed digest
agree (re-certifying once for the benign append race).

FAIL-CLOSED DISCIPLINE. Every verification refusal raises with a distinct,
grep-able prefix that the health record's `load_error` carries verbatim:

  * ``ledger_unreadable:`` — the resolved ledger's bytes could not be read,
    including a certified ledger that DISAPPEARED before the loader's single
    read: the task gates on ``identity.resolved`` first, so absence here is
    a fault, never the pre-publish window (pipeline#254);
  * ``ledger_chain_verification_failed:`` — the ledger's per-row digest chain
    does not verify (a rewritten/reordered/edited row);
  * ``dated_artifact_missing:`` / ``dated_artifact_unparseable:`` — the tail
    row names a cutoff whose dated artifact is absent or not JSON;
  * ``artifact_content_sha_mismatch:`` — the artifact's self-carried
    content_sha256 does not recompute over its own body;
  * ``ledger_row_artifact_sha_mismatch:`` — the artifact self-verifies but is
    NOT the bytes the ledger row pinned (a swapped dated file);
  * ``artifact_kind_mismatch:`` / ``artifact_cutoff_mismatch:`` /
    ``artifact_params_version_mismatch:`` — row↔artifact cross-field parity;
  * ``scores_reconstruction_mismatch:`` — recomputing the composite from the
    artifact's stored features (the package's own ``composite_scores``)
    disagrees with the stored scores. Digests verify identity, not validity;
    this is the golden-reproduction check that pairs with them.

The ONE non-fault refusal: a SUCCESSFULLY READ, chain-verified ledger carrying
ZERO rows — the designed PENDING_FIRST_ARTIFACT window (model#197 amendment 2)
— raises ``ShadowNotYetPublished``, which the task stamps as the distinct
``not_yet_published`` EXPECTED skip, not a fault.

Tests: tests/test_momentum_residual_shadow_handler.py.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

from renquant_pipeline.kernel.panel_pipeline.shadow_health import (
    CONTENT_SHA256_PREFIX,
    ShadowNotYetPublished,
)

log = logging.getLogger("kernel.panel_pipeline.momentum_residual_scorer")

#: Dated-artifact basename beside the ledger — the serving-path convention
#: fixed by model#197 (decision 1): the weekly job publishes
#: ``artifacts/momentum/<cutoff>/momentum_residual_v0.json``. Mirrors the model
#: repo's tools/momentum_train_run.py ``ARTIFACT_BASENAME``; the loaded
#: artifact's ``kind`` is additionally cross-checked against the ledger row's,
#: so a drifted convention fails loudly instead of loading the wrong file.
MOMENTUM_DATED_ARTIFACT_BASENAME = "momentum_residual_v0.json"

#: The five composite features of the v0 construction (train.py f1..f5).
_FEATURE_NAMES = ("f1", "f2", "f3", "f4", "f5")

#: Reconstruction tolerance. The stored scores were computed by the SAME
#: ``composite_scores`` over the SAME float64 values now read back from JSON
#: (round-trip exact), so agreement should be bit-level; the tolerance only
#: absorbs cross-platform libm noise. It matches the model repo's golden-test
#: bar (<1e-9 score identity against the sealed runner's assemble_day).
_SCORE_RECONSTRUCTION_ATOL = 1e-9


def _import_momentum_construction():
    """Guarded import of the renquant-model construction surface.

    Returns ``(momentum_pkg, composite_scores)``. Raises ImportError NAMING the
    missing dependency + the remedy — ``ApplyShadowScoringTask`` records it as
    the lane's load-failure FAULT (never a crash; hf_patchtst precedent)."""
    try:
        import renquant_model_momentum as _mm  # noqa: PLC0415
        from renquant_model_common.momentum_features import (  # noqa: PLC0415
            composite_scores,
        )
    except ImportError as exc:
        raise ImportError(
            "missing dependency renquant_model_momentum / "
            "renquant_model_common (shipped by the renquant-model "
            "distribution) — install renquant-pipeline[momentum] or put the "
            "sibling renquant-model checkout's src on the path (the "
            f"hf_patchtst precedent); underlying: {exc}"
        ) from exc
    return _mm, composite_scores


def _as_float(value: Any) -> float:
    """Strict-JSON scalar → float; null (the serialized non-finite) → nan."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float("nan")
    return float(value)


def _params_fingerprint(params: Mapping[str, Any]) -> str:
    """Deterministic training-config identity derived from the artifact's own
    params block: ``momentum-<params_version>-<sha256(canonical params)[:16]>``.

    The momentum artifact carries no ``config_fingerprint`` field (s104#77
    F-2: its identity fields are ``trained_at_utc`` / ``cutoff_date`` /
    ``content_sha256``), but the health contract requires one — this stamps
    the honest equivalent: the params block IS the training config, and the
    fingerprint is recomputable from the artifact by any reader."""
    canon = json.dumps(dict(params), sort_keys=True, separators=(",", ":"),
                       allow_nan=False)
    digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
    version = str(params.get("params_version", "unversioned"))
    return f"momentum-{version}-{digest}"


def _reconstruct_scores(artifact: Mapping[str, Any], composite_scores,
                        *, min_features: int) -> dict[str, float]:
    """Recompute the composite from the artifact's stored features via the
    package's OWN construction and require agreement with the stored scores.

    Digests verify identity, not validity: the content sha proves these are
    the published bytes, this proves the published scores actually ARE the
    declared construction over the declared features. Returns the
    reconstructed scores (nan entries included; the scorer serves finite)."""
    feats_by_name = artifact.get("features")
    if not isinstance(feats_by_name, Mapping) or not feats_by_name:
        raise ValueError(
            "scores_reconstruction_mismatch: artifact carries no 'features' "
            "block to reconstruct from")
    feats: dict[str, dict[str, float]] = {f: {} for f in _FEATURE_NAMES}
    for ticker, cols in feats_by_name.items():
        cols = cols if isinstance(cols, Mapping) else {}
        for fname in _FEATURE_NAMES:
            feats[fname][str(ticker)] = _as_float(cols.get(fname))
    reconstructed, _n_used = composite_scores(feats, min_features=min_features)

    stored_raw = artifact.get("scores")
    stored = ({str(t): _as_float(v) for t, v in stored_raw.items()}
              if isinstance(stored_raw, Mapping) else {})
    if set(reconstructed) != set(stored):
        raise ValueError(
            "scores_reconstruction_mismatch: reconstructed name set "
            f"({len(reconstructed)}) != stored name set ({len(stored)})")
    for ticker, recon in reconstructed.items():
        kept = stored[ticker]
        if math.isnan(recon) and math.isnan(kept):
            continue
        if math.isnan(recon) != math.isnan(kept) or (
                abs(recon - kept) > _SCORE_RECONSTRUCTION_ATOL):
            raise ValueError(
                "scores_reconstruction_mismatch: composite over the stored "
                f"features disagrees with the stored score for {ticker!r} "
                f"(reconstructed {recon!r}, stored {kept!r}, atol "
                f"{_SCORE_RECONSTRUCTION_ATOL})")
    return reconstructed


class MomentumResidualScorer:
    """Serves the verified tail artifact's per-ticker composite scores.

    The v0 construction is cross-sectional and frozen at the artifact's
    cutoff: serving-date scoring is a per-ticker LOOKUP into the verified
    score set — no feature matrix, no history panel (``scores_by_ticker``
    is the capability flag ``ApplyShadowScoringTask`` dispatches on).

    PRIMARY-SCORER SURFACE (2026-08-03, pipeline#258). The class also
    implements the ``PanelScorer`` serving contract every primary call site
    assumes — ``feature_cols`` / ``seq_len`` / ``score(feature_matrix)`` —
    so a readonly e2e lane can run the momentum model as its primary and
    produce sized shadow orders (the operator's explicit ask; the previous
    state crashed LoadScorerTask's logging line on the missing attribute).
    ``feature_cols`` is EMPTY by construction: scoring is a lookup, so the
    feature-matrix builder has nothing to assemble and ``score`` reads only
    the matrix INDEX (tickers). Names outside the frozen universe come back
    NaN — the primary path's "scoring ran but produced no score" marker —
    rather than being silently omitted, mirroring BlendPanelScorer's
    unscored-name contract."""

    kind = "momentum_residual"
    requires_history = False
    scores_by_ticker = True
    feature_cols: list[str] = []
    seq_len = 1

    def __init__(self, *, scores: Mapping[str, float],
                 metadata: Mapping[str, Any]) -> None:
        self._scores = {str(t): float(v) for t, v in scores.items()
                        if not math.isnan(_as_float(v))}
        self.metadata = dict(metadata)

    @property
    def universe(self) -> list[str]:
        return sorted(self._scores)

    def score_tickers(self, tickers: Iterable[str]) -> pd.Series:
        """Finite scores for the requested tickers; names outside the
        artifact's scored universe are OMITTED (the task's coverage math then
        counts them against ``coverage_frac`` instead of serving a guess)."""
        out = {t: self._scores[t] for t in tickers if t in self._scores}
        return pd.Series(out, dtype=float)

    def score(self, feature_matrix: pd.DataFrame, ctx: Any = None) -> pd.Series:
        """PanelScorer-contract entry: lookup by the matrix's ticker index.

        ``ctx`` accepted-but-ignored for signature uniformity (same as
        BlendPanelScorer). The matrix's COLUMNS are irrelevant by design
        (``feature_cols`` is empty); only its index is read. Unscored names
        return NaN so the caller's own unscored accounting applies — the
        shadow path's omit-and-count-coverage convention would silently
        shrink the primary cross-section instead."""
        del ctx
        tickers = [str(t) for t in feature_matrix.index]
        return self.score_tickers(tickers).reindex(tickers)


def _verified_row_scores(ledger: Path, row: Mapping[str, Any], mm, composite_scores):
    """Steps 3-6 of the serving contract for ONE ledger row: dated-artifact
    load, content identity both directions, row-artifact cross-field parity,
    and golden reproduction. Extracted VERBATIM from the serving loader so
    the as-of loader (pipeline#262) shares the exact contract instead of
    duplicating it. Returns (artifact, scores, params)."""
    # 3) DATED ARTIFACT beside the ledger, per the tail row's cutoff. Also a
    #    single read: parse once, verify over the parsed object (the package
    #    recomputes the canonical-JSON sha of exactly what was consumed), so
    #    no check/use divergence is possible for the dated file either. The
    #    dated file is immutable by the append-only store contract; its
    #    row-pin check below is what enforces that.
    dated = ledger.parent / str(row["cutoff_date"]) / MOMENTUM_DATED_ARTIFACT_BASENAME
    try:
        artifact = json.loads(dated.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(
            f"dated_artifact_missing: ledger tail row {row['row_index']} "
            f"(cutoff {row['cutoff_date']}, artifact sha "
            f"{str(row['artifact_content_sha256'])[:12]}…) has no dated "
            f"artifact at {dated}")
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"dated_artifact_unparseable: {dated}: {exc}") from exc

    # 4) CONTENT IDENTITY, both directions: the artifact's self-carried sha
    #    recomputes (package verifier), AND it is the exact artifact the
    #    append-only row pinned.
    try:
        mm.verify_artifact_content_sha(artifact)
    except ValueError as exc:
        raise ValueError(f"artifact_content_sha_mismatch: {dated}: {exc}") from exc
    if artifact.get("content_sha256") != row["artifact_content_sha256"]:
        raise ValueError(
            f"ledger_row_artifact_sha_mismatch: {dated} self-verifies as "
            f"{str(artifact.get('content_sha256'))[:12]}… but the ledger tail "
            f"row pinned {str(row['artifact_content_sha256'])[:12]}… — the "
            "dated file is not the artifact the append-only ledger recorded")

    # 5) ROW ↔ ARTIFACT cross-field parity (identity ≠ validity).
    if artifact.get("kind") != row["kind"]:
        raise ValueError(
            f"artifact_kind_mismatch: row says {row['kind']!r}, artifact says "
            f"{artifact.get('kind')!r}")
    if artifact.get("cutoff_date") != row["cutoff_date"]:
        raise ValueError(
            f"artifact_cutoff_mismatch: row says {row['cutoff_date']!r}, "
            f"artifact says {artifact.get('cutoff_date')!r}")
    params = artifact.get("params") if isinstance(artifact.get("params"), Mapping) else {}
    if params.get("params_version") != row["params_version"]:
        raise ValueError(
            f"artifact_params_version_mismatch: row says "
            f"{row['params_version']!r}, artifact says "
            f"{params.get('params_version')!r}")
    min_features = params.get("min_features")
    if isinstance(min_features, bool) or not isinstance(min_features, int):
        raise ValueError(
            "artifact_params_version_mismatch: params carry no integer "
            f"'min_features' (got {min_features!r}) — cannot reproduce the "
            "declared construction")

    # 6) GOLDEN REPRODUCTION — the package's construction over the stored
    #    features must reproduce the stored scores; serve the reconstruction.
    scores = _reconstruct_scores(artifact, composite_scores,
                                 min_features=min_features)
    return artifact, scores, params


def load_momentum_artifact_as_of(ledger_path: str | Path, *,
                                 session_date: str,
                                 session_cutoff_utc: str,
                                 ) -> "tuple[dict[str, float], dict[str, Any]] | None":
    """AS-OF verified artifact loader (pipeline#262, for orch#783's S2
    readout): the serving row for ``session_date`` selected TIME-SAFELY —
    the LAST chain-verified ledger row with ``cutoff_date <= session_date``
    AND ``appended_at_utc <= session_cutoff_utc`` — then the FULL serving
    contract on that row via the same extracted steps the live loader runs
    (dated artifact, content sha both directions, row-artifact parity,
    golden reproduction; nothing duplicated downstream).

    Returns ``(scores, identity)`` where identity carries the frozen
    triplet (row_index, row_sha, artifact_content_sha256) plus
    cutoff_date/params_version, or ``None`` when no qualifying row exists,
    the ledger is absent/empty, the chain fails, or the selected row's
    artifact fails ANY contract step — the readout counts that session
    against coverage; it never needs a reason string to act on.
    Single-read discipline identical to the serving loader.
    """
    mm, composite_scores = _import_momentum_construction()
    ledger = Path(ledger_path)
    try:
        raw = ledger.read_bytes()
    except OSError:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="momentum-ledger-asof-") as td:
            snap = Path(td) / "momentum_artifact_ledger.jsonl"
            snap.write_bytes(raw)
            rows = mm.load_and_verify_ledger(snap)
    except mm.LedgerIntegrityError:
        return None
    qualifying = [
        r for r in rows
        if str(r["cutoff_date"]) <= session_date
        and str(r["appended_at_utc"]) <= session_cutoff_utc
    ]
    if not qualifying:
        return None
    row = qualifying[-1]
    try:
        artifact, scores, params = _verified_row_scores(
            ledger, row, mm, composite_scores)
    except (ValueError, ShadowNotYetPublished):
        return None
    finite = {str(t): float(v) for t, v in scores.items()
              if not math.isnan(_as_float(v))}
    identity = {
        "row_index": row["row_index"],
        "row_sha": row["row_sha"],
        "artifact_content_sha256": row["artifact_content_sha256"],
        "cutoff_date": row["cutoff_date"],
        "params_version": params.get("params_version"),
    }
    return finite, identity


def load_momentum_residual_scorer(ledger_path: str | Path,
                                  config: Mapping[str, Any] | None = None,
                                  ) -> MomentumResidualScorer:
    """Verified-ledger-tail → verified dated artifact → scorer.

    ``ledger_path`` is the ALREADY-RESOLVED ledger file (the task resolves the
    configured ref through the one canonical ``resolve_artifact_identity``
    authority — absolute → strategy_dir → repo_root — and passes the certified
    path here; no second resolution happens in this module, per the
    single-resolution rule from codex CR#2)."""
    del config  # the v0 construction is fully self-described by the artifact
    mm, composite_scores = _import_momentum_construction()
    ledger = Path(ledger_path)

    # 1) ONE read of the live path → an immutable in-memory snapshot (codex
    #    CR on #253: the task certified the digest BEFORE this call; a second
    #    open of the live path would let a weekly append between the two
    #    reads serve a NEW tail under the OLD certified identity). The
    #    consumed digest and the chain verification both derive from THIS
    #    snapshot — the live file is never opened again.
    try:
        raw = ledger.read_bytes()
    except FileNotFoundError as exc:
        # NEVER the designed pre-first-publish skip (pipeline#254 — a
        # regression from #253 mapped this to ShadowNotYetPublished). The
        # task only calls this loader AFTER certifying ``identity.resolved``
        # over this exact path, so a file absent NOW disappeared between
        # certification and use — a load FAULT the record must name. The one
        # non-fault refusal stays the successfully read, chain-verified
        # EMPTY ledger below.
        raise ValueError(
            f"ledger_unreadable: {ledger} disappeared between identity "
            "certification and the loader's read — the task certified this "
            "resolved path immediately before this call, so absence here is "
            "a load fault, not the designed PENDING_FIRST_ARTIFACT window"
        ) from exc
    except OSError as exc:
        raise ValueError(f"ledger_unreadable: {ledger}: {exc}") from exc
    consumed_digest = (CONTENT_SHA256_PREFIX
                       + hashlib.sha256(raw).hexdigest()[:16])

    # 2) LEDGER CHAIN over the snapshot — the package's own verification,
    #    never reimplemented. The verifier takes a path, so the snapshot
    #    bytes go to a private temp file; verifying the snapshot (not the
    #    live path) is exactly what keeps the read single.
    try:
        with tempfile.TemporaryDirectory(prefix="momentum-ledger-snap-") as td:
            snap = Path(td) / "momentum_artifact_ledger.jsonl"
            snap.write_bytes(raw)
            rows = mm.load_and_verify_ledger(snap)
    except mm.LedgerIntegrityError as exc:
        raise ValueError(f"ledger_chain_verification_failed: {exc}") from exc
    if not rows:
        raise ShadowNotYetPublished(
            f"momentum ledger {ledger} verifies but carries zero rows — the "
            "designed PENDING_FIRST_ARTIFACT window (model#197 amendment 2): "
            "the weekly train job has not published its first artifact yet")
    row = rows[-1]

    artifact, scores, params = _verified_row_scores(
        ledger, row, mm, composite_scores)
    metadata: dict[str, Any] = {
        "kind": artifact["kind"],
        "cutoff_date": row["cutoff_date"],
        # SINGLE-READ CLOSURE: the digest (identity recipe) of the exact
        # ledger bytes this load consumed. The task compares it against the
        # identity it certified BEFORE the load and refuses/re-certifies on
        # divergence — new bytes are never served under an old certified
        # digest (codex CR on #253).
        "consumed_content_sha256": consumed_digest,
        # STALENESS SURFACE (deliberate): the sentinel's freshness axis reads
        # `effective_train_cutoff_date` off this metadata, and for this lane it
        # is the tail row's cutoff_date — the weekly publish cadence — per the
        # declared serving contract. The artifact's own MEASURED
        # effective_train_cutoff_date trails the cutoff by the skip embargo
        # (~21 business days) BY CONSTRUCTION, so measuring staleness from it
        # would flag a same-day publish stale on arrival (the exact
        # fwd60-stale-on-arrival class PR #220 fixed). It stays visible below
        # under its own name.
        "effective_train_cutoff_date": row["cutoff_date"],
        "artifact_effective_train_cutoff_date": artifact.get(
            "effective_train_cutoff_date"),
        "trained_date": (str(artifact["trained_at_utc"])[:10]
                         if artifact.get("trained_at_utc") else None),
        # Deliberately NO `lookahead_days`: the v0 construction trains on no
        # forward label (the skip is an embargo, not a horizon), so declaring
        # one would borrow a widened freshness bound the recipe did not earn;
        # the finalizer's single-axis 28d rule over the cutoff date is the
        # correct gate for a weekly publish cadence.
        "config_fingerprint": _params_fingerprint(params),
        "params_version": params.get("params_version"),
        "n_scored": artifact.get("n_scored"),
        "names_floor_ok": artifact.get("names_floor_ok"),
        "artifact_content_sha256": artifact["content_sha256"],
        "ledger_row_index": row["row_index"],
        "ledger_row_sha": row["row_sha"],
    }
    scorer = MomentumResidualScorer(scores=scores, metadata=metadata)
    log.info(
        "momentum_residual: serving verified ledger tail row %s (cutoff %s, "
        "artifact %s…, %d finite scores)", row["row_index"], row["cutoff_date"],
        str(artifact["content_sha256"])[:12], len(scorer.universe))
    return scorer


__all__ = [
    "load_momentum_artifact_as_of",
    "MOMENTUM_DATED_ARTIFACT_BASENAME",
    "MomentumResidualScorer",
    "ShadowNotYetPublished",
    "load_momentum_residual_scorer",
]
