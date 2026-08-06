"""Persist the AS-SERVED inference feature matrix as a materialized snapshot.

Why this exists
---------------
The 94x172 inference matrix is assembled every run by
``AssembleInferenceMatrixTask``, filtered by ``RowCoverageGateTask`` /
``DriftGuardTask``, handed to the scorer, and then **discarded**. Nothing on
disk and no table in ``runs.alpaca.db`` holds the feature values a score was
produced from — ``candidate_scores`` persists the outputs (raw/rank/panel score,
mu, sigma) but never the inputs.

Two consequences, both measured 2026-08-06:

  * Score attribution is impossible after the fact. Given a score you cannot
    recover the features that produced it.
  * ``renquant-orchestrator``'s rq105 shadow real-time serving requires a
    ``--feature-snapshot-json`` carrying frozen T-1 feature values (Codex #221:
    "a bare watchlist / strategy-config reference is NOT a valid feature
    snapshot"). No producer for that file has ever existed, so
    ``run_shadow_serving.sh`` has skipped with ``EXIT_NOT_WIRED=4`` every
    scheduled day and rq105 has emitted no intraday decision since 2026-07-14.

The tempting shortcut is to REBUILD a T-1 matrix in a standalone producer. That
is the one thing this module must not do: a rebuilt matrix is not necessarily
the matrix the scorer saw (different data vintage, different NaN handling,
different coverage filtering), so its digest would bind every downstream row to
a feature state that was never served. That is precisely the substitution #221
exists to prevent. This module therefore writes **the object already in memory,
after every gate that can still modify it**, and writes nothing when there is
nothing genuine to write.

Contract
--------
Emits exactly the shape ``renquant_orchestrator.realtime_data_plane
.FeatureSnapshot.from_mapping`` validates:

    {"feature_cutoff": str,            # data as-of the values were frozen at
     "feature_builder_version": str,   # feature-construction identity
     "features": {TICKER: {col: value}}}

The consumer computes the digest itself over (cutoff, builder_version,
features), so this module deliberately does NOT invent one — a digest written
here could drift from the one the consumer derives, and the mismatch would be
silent.

Safety
------
Disabled unless a destination is configured, and **fail-open in every path**:
this runs inside the live scoring pipeline that places real orders, so no
snapshot-write failure may ever change a scoring outcome. A raised exception
here would be a self-inflicted trading outage in exchange for an observability
artifact.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import math
import os
import tempfile
from typing import Any

log = logging.getLogger("kernel.panel_pipeline.feature_snapshot")

#: Env fallback for the destination directory. A config key requires editing a
#: production ``strategy_config.json``, which agent PRs may not touch; the
#: reviewed launchd wrapper can export this instead. Config wins when both are
#: set.
ENV_DIR = "RQ_FEATURE_SNAPSHOT_DIR"

BUILDER_NAME = "renquant_pipeline.kernel.panel_pipeline.feature_matrix"


def resolve_output_dir(config: Any) -> str | None:
    """Destination directory, or None when snapshot writing is disabled.

    Resolution order: ``ranking.panel_scoring.feature_snapshot_dir`` in config,
    then ``$RQ_FEATURE_SNAPSHOT_DIR``. Absent/blank in both ⇒ disabled, which is
    the default and a silent no-op.
    """
    try:
        cfg = (config or {}).get("ranking", {}).get("panel_scoring", {})
        value = str(cfg.get("feature_snapshot_dir") or "").strip()
    except Exception:  # noqa: BLE001 - config shape is not this module's contract
        value = ""
    if not value:
        value = str(os.environ.get(ENV_DIR) or "").strip()
    return value or None


def builder_version(feature_cols: Any) -> str:
    """Identity of the feature construction, not merely of this file.

    Two runs whose feature COLUMNS differ built different features even when the
    builder module is byte-identical, so the column set is part of the identity.
    Hashing the ordered columns keeps the string bounded while staying sensitive
    to order, which matters: the matrix is positional at score time.
    """
    cols = [str(c) for c in (feature_cols or [])]
    h = hashlib.sha256("\n".join(cols).encode("utf-8")).hexdigest()[:16]
    return f"{BUILDER_NAME}@cols:{len(cols)}:sha256:{h}"


def _jsonable(value: Any) -> Any:
    """NaN/Inf are not JSON; they become null rather than crashing the writer.

    A NaN feature is real information (the coverage gate tolerates some), so it
    is preserved as an explicit null instead of being dropped — dropping would
    make an incomplete row indistinguishable from a complete one.
    """
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, (str, bool, int)) or value is None:
        return value
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return None if (math.isnan(f) or math.isinf(f)) else f


def matrix_to_features(matrix: Any) -> dict[str, dict[str, Any]]:
    """``DataFrame`` (or mapping) → ``{TICKER: {col: value}}``.

    Accepts both shapes because the assemble task produces a ticker-indexed
    DataFrame while ``panel_scoring``'s own path builds a plain dict; a writer
    that only understood one of them would silently emit nothing for the other.
    """
    if matrix is None:
        return {}
    to_dict = getattr(matrix, "to_dict", None)
    if to_dict is not None and hasattr(matrix, "index") and hasattr(matrix, "columns"):
        rows = to_dict(orient="index")  # type: ignore[call-arg]
    elif isinstance(matrix, dict):
        rows = matrix
    else:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for ticker, row in (rows or {}).items():
        key = str(ticker).strip().upper()
        if not key or not isinstance(row, dict):
            continue
        out[key] = {str(c): _jsonable(v) for c, v in row.items()}
    return out


def _atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    """temp + fsync + rename, so a reader never observes a half-written file."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".feature_snapshot.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_snapshot(
    out_dir: str,
    session_date: str,
    cutoff: str,
    feature_cols: Any,
    matrix: Any,
) -> str | None:
    """Write ``feature_snapshot_<session_date>.json``; return the path or None.

    Returns None — writing nothing — when the matrix yields no rows. An empty
    ``features`` map is rejected by the consumer anyway, and a file that exists
    but cannot be served is worse than an absent one: the wrapper's "not wired"
    skip is honest, a fail-closed serve is not.
    """
    features = matrix_to_features(matrix)
    if not features:
        log.warning("feature snapshot: matrix produced 0 rows — nothing written")
        return None
    payload = {
        "feature_cutoff": str(cutoff),
        "feature_builder_version": builder_version(feature_cols),
        "features": features,
    }
    path = os.path.join(out_dir, f"feature_snapshot_{session_date}.json")
    _atomic_write_json(path, payload)
    n_cols = len(next(iter(features.values())))
    log.info(
        "feature snapshot: wrote %s  tickers=%d cols=%d cutoff=%s",
        path, len(features), n_cols, cutoff,
    )
    return path


def persist_from_context(ctx: Any) -> str | None:
    """Best-effort snapshot write from a scoring context. Never raises.

    Every failure mode returns None after logging. This is called from the live
    order-placing pipeline; an observability artifact must not be able to stop a
    trading run.
    """
    try:
        out_dir = resolve_output_dir(getattr(ctx, "config", None))
        if not out_dir:
            return None
        matrix = getattr(ctx, "_panel_matrix", None)
        if matrix is None:
            log.info("feature snapshot: no matrix on context (gated to None) — skipped")
            return None
        scorer = getattr(ctx, "_panel_scorer", None)
        if scorer is None:
            # Without a scorer we cannot state which columns were actually part
            # of its contract; falling back to feature_cols=[] would still write
            # the matrix's real columns under a builder_version hashed from an
            # empty list — a mislabelled snapshot, not an absent one. Refuse,
            # same as the missing-cutoff case above.
            log.warning("feature snapshot: no panel scorer on context — refusing to write")
            return None
        feature_cols = getattr(scorer, "feature_cols", []) or []
        inputs = getattr(ctx, "_fm_inputs", None) or {}
        cutoff = str(inputs.get("today_ts") or "").strip()
        if not cutoff:
            # The consumer REQUIRES a non-empty cutoff. Emitting today's date as a
            # stand-in would stamp an unverified as-of onto real feature values,
            # so refuse instead — an absent snapshot is recoverable, a mislabelled
            # one silently corrupts every downstream provenance check.
            log.warning("feature snapshot: no feature cutoff on context — refusing to write")
            return None
        session_date = str(
            getattr(ctx, "session_date", "")
            or getattr(ctx, "run_date", "")
            or datetime.date.today().isoformat()
        )[:10]
        return write_snapshot(out_dir, session_date, cutoff, feature_cols, matrix)
    except Exception as exc:  # noqa: BLE001 - fail-open by design; see module docstring
        log.warning("feature snapshot: write failed (%s: %s) — run continues", type(exc).__name__, exc)
        return None


__all__ = [
    "ENV_DIR",
    "BUILDER_NAME",
    "resolve_output_dir",
    "builder_version",
    "matrix_to_features",
    "write_snapshot",
    "persist_from_context",
]
