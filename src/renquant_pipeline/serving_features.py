"""Serving feature persistence — the daily run stops discarding what it computed.

Rollout step 2 of ``doc/design/2026-08-02-serving-feature-persistence.md``
(pipeline#250, merged): persist the AS-SERVED feature matrix — the exact
object the primary snapshot scorer consumed, post ``transform_feature_frame``
— as ``<run_output_dir>/serving_features.parquet`` plus an additive
``serving_features`` sidecar block for the run bundle.

Producer / consumer contract (the decision_trace precedent)
-----------------------------------------------------------
* ``stage_serving_features(ctx, matrix, scorer)`` is called by the kernel
  ``ApplyScoresTask`` (``kernel/panel_pipeline/job_panel_scoring.py``) at the
  exact point the serving matrix exists and before scoring consumes it. It
  freezes a deep copy plus provenance on ``ctx`` and, when the context
  already knows its run output dir (``ctx.run_output_dir``), writes the
  parquet immediately.
* ``write_staged_serving_features(ctx, output_dir)`` performs (or completes)
  the write into the run output dir. The payload writers in
  :mod:`renquant_pipeline.inference` call it with the payload's parent dir —
  the same dir the rest of the bundle lands in.
* ``serving_features_bundle_block(ctx)`` is the absent-tolerant accessor the
  run-bundle collectors read (``runtime_inference_payload`` /
  ``live_context_snapshot_from_live_context`` embed it as the additive
  top-level ``serving_features`` key). The orchestrator-side pickup into
  ``run_bundle.json`` is the design's rollout step 3 (a separate PR).

Failure contract (record-don't-raise)
-------------------------------------
The writer NEVER raises into the decision path. Any failure records
``status: "write_failed"`` (+ ``error``) in the sidecar block; scoring and
every downstream decision byte are unaffected. A run whose scorer consumes
no snapshot matrix (``score_with_history`` sequence scorers) stages nothing
and the block is simply absent — the additive idiom's default.

Sidecar keys (design-verbatim; ``feature_builder_version`` is the literal
key ``FeatureSnapshot.from_mapping`` and ``RunProvenance`` require):
``{path, sha256, n_rows, n_cols, feature_cutoff, feature_builder_version,
panel_read_sha256, status[, error]}``.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("renquant_pipeline.serving_features")

#: File name of the persisted AS-SERVED matrix under the run output dir.
SERVING_FEATURES_FILENAME = "serving_features.parquet"

#: Top-level payload / bundle key of the sidecar block.
SERVING_FEATURES_BLOCK_KEY = "serving_features"

#: ctx attribute holding the frozen matrix copy + provenance until a run
#: output dir is known (private staging surface).
STAGED_ATTR = "_serving_features_staged"

#: ctx attribute holding the completed sidecar block — the surface the
#: run-bundle collectors read (the ``wash_sale_decision_records`` idiom).
RECORD_ATTR = "serving_features_record"

STATUS_WRITTEN = "written"
STATUS_WRITE_FAILED = "write_failed"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _feature_cutoff(ctx: Any) -> str | None:
    """The as-of date the serving rows were computed at (features use bars
    ≤ this date) — the value the T-1 snapshot producer formats as
    ``feature_cutoff``."""
    today = getattr(ctx, "today", None)
    if today is None:
        return None
    if hasattr(today, "isoformat"):
        return str(today.isoformat())
    return str(today)


def _feature_builder_version(scorer: Any) -> str | None:
    """The transform's own version, sourced from where it already lives.

    ``transform_feature_frame`` is driven entirely by the scorer artifact's
    stored feature stats, and the artifact stamps that contract's version as
    ``feature_preprocess_version`` (stamped by the panel builder's stats
    sidecar, carried into the trained artifact — value ``2`` on the current
    prod artifact). No new constant is invented; an artifact without the
    stamp records ``None`` and the gap surfaces loudly at the Stage-3
    formatting step instead of being papered over here.
    """
    metadata = getattr(scorer, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    version = metadata.get("feature_preprocess_version")
    if version is None:
        return None
    return str(version)


def _set_record(ctx: Any, record: dict[str, Any]) -> dict[str, Any]:
    try:
        setattr(ctx, RECORD_ATTR, record)
    except Exception:  # noqa: BLE001 — never raise into the decision path
        log.error("serving_features: could not stamp record on ctx", exc_info=True)
    return record


def _failure_record(
    ctx: Any,
    error: BaseException | str,
    *,
    path: Path | str | None = None,
    staged: dict[str, Any] | None = None,
) -> dict[str, Any]:
    staged = staged if isinstance(staged, dict) else {}
    matrix = staged.get("matrix")
    record = {
        "path": str(path) if path is not None else None,
        "sha256": None,
        "n_rows": int(matrix.shape[0]) if matrix is not None else None,
        "n_cols": int(matrix.shape[1]) if matrix is not None else None,
        "feature_cutoff": staged.get("feature_cutoff"),
        "feature_builder_version": staged.get("feature_builder_version"),
        "panel_read_sha256": None,
        "status": STATUS_WRITE_FAILED,
        "error": str(error)[:500],
    }
    log.error("serving_features: write FAILED (recorded, not raised): %s", error)
    return _set_record(ctx, record)


def stage_serving_features(
    ctx: Any,
    matrix: Any,
    scorer: Any,
    *,
    panel_read_path: str | Path | None = None,
) -> None:
    """Freeze the AS-SERVED matrix + provenance at the serving-transform site.

    Called with the IDENTICAL matrix object the scorer is about to consume.
    A deep copy is staged so later in-place mutations of the live matrix
    (e.g. the sentiment gate zeroing ``ctx._panel_matrix``) cannot change
    what is persisted. If ``ctx.run_output_dir`` is already known the write
    happens immediately; otherwise the payload writers complete it.

    Record-don't-raise: any failure records ``status: write_failed`` and
    returns — scoring proceeds untouched.
    """
    try:
        staged = {
            "matrix": matrix.copy(deep=True),
            "feature_cutoff": _feature_cutoff(ctx),
            "feature_builder_version": _feature_builder_version(scorer),
            "panel_read_path": str(panel_read_path) if panel_read_path else None,
        }
        setattr(ctx, STAGED_ATTR, staged)
    except Exception as exc:  # noqa: BLE001 — never raise into the decision path
        _failure_record(ctx, exc)
        return
    out_dir = getattr(ctx, "run_output_dir", None)
    if out_dir:
        write_staged_serving_features(ctx, out_dir)


def write_staged_serving_features(
    ctx: Any,
    output_dir: str | Path,
) -> dict[str, Any] | None:
    """Write the staged matrix to ``<output_dir>/serving_features.parquet``.

    Idempotent: a completed (``status: written``) record is returned as-is,
    so the staging site and the payload writers may both call this. Returns
    ``None`` when nothing was staged (history-scorer runs; pre-#250
    contexts) — the absent-tolerant default. Never raises: failures are
    recorded as ``status: write_failed`` with the error string.
    """
    record = getattr(ctx, RECORD_ATTR, None)
    if isinstance(record, dict) and record.get("status") == STATUS_WRITTEN:
        return record
    staged = getattr(ctx, STAGED_ATTR, None)
    if not isinstance(staged, dict) or staged.get("matrix") is None:
        return record if isinstance(record, dict) else None
    path: Path | None = None
    try:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / SERVING_FEATURES_FILENAME
        matrix = staged["matrix"]
        frame = matrix.copy()
        # Design column contract: explicit `ticker` column + the exact
        # AS-SERVED feature columns, in the served order.
        frame.insert(0, "ticker", [str(t) for t in matrix.index])
        frame.to_parquet(path, index=False)
        panel_read_path = staged.get("panel_read_path")
        panel_read_sha256 = (
            _sha256_file(Path(panel_read_path)) if panel_read_path else None
        )
        record = {
            "path": str(path),
            "sha256": _sha256_file(path),
            "n_rows": int(matrix.shape[0]),
            "n_cols": int(matrix.shape[1]),
            "feature_cutoff": staged.get("feature_cutoff"),
            "feature_builder_version": staged.get("feature_builder_version"),
            "panel_read_sha256": panel_read_sha256,
            "status": STATUS_WRITTEN,
        }
        log.info(
            "serving_features: persisted %s (%d rows × %d cols, sha256=%s…)",
            path, record["n_rows"], record["n_cols"], record["sha256"][:12],
        )
        return _set_record(ctx, record)
    except Exception as exc:  # noqa: BLE001 — never raise into the decision path
        return _failure_record(ctx, exc, path=path, staged=staged)


def serving_features_bundle_block(ctx: Any) -> dict[str, Any] | None:
    """The additive ``serving_features`` sidecar block, or ``None`` when the
    recorder never fired (absent-tolerant — every pre-#250 context and every
    sequence-scorer run)."""
    if isinstance(ctx, dict):
        record = ctx.get(RECORD_ATTR)
    else:
        record = getattr(ctx, RECORD_ATTR, None)
    if not isinstance(record, dict) or not record:
        return None
    return dict(record)


__all__ = [
    "SERVING_FEATURES_BLOCK_KEY",
    "SERVING_FEATURES_FILENAME",
    "STATUS_WRITTEN",
    "STATUS_WRITE_FAILED",
    "serving_features_bundle_block",
    "stage_serving_features",
    "write_staged_serving_features",
]
