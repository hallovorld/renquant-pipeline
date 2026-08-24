"""S3-P1: persist the SERVED daily feature panel (orch#1026, RFC #208 Stage-3).

WHY. Nothing persists the feature vectors the panel scorer actually served —
verified 2026-08-23: no feature file exists anywhere under the umbrella
``data/`` tree. Three consumers are blocked on the same absence:

  * rq105 Stage-3: ``run_shadow_serving.sh`` has skipped every session since
    2026-08-12 with ``SKIP not-wired: no producer exists for
    feature_snapshot_<date>.json`` — the intraday snapshot producer needs a
    T-1 frozen feature panel to overlay intraday quotes onto;
  * post-hoc score attribution (the #17 gap): once the panel cutoff passes,
    a served score can no longer be explained;
  * G-K: the daily feature panel cannot be shared across lanes because it
    never exists as an artifact.

WHAT. After the primary scorer's matrix is final, write

    data/rq105/feature_panel_<date>.json        {"feature_cutoff", "builder_version", "features"}
    data/rq105/feature_panel_<date>.meta.json   provenance + content sha256

The payload keys mirror ``FeatureSnapshot.from_mapping`` in
renquant-orchestrator (``shadow_realtime_serving.py:619``): ``feature_cutoff``
(non-empty), ``builder_version`` (non-empty), ``features`` (non-empty mapping).
The contract is mirrored, NOT imported — this repo must not depend on the
orchestrator; the orchestrator-side consumer validates on read, and this
module's tests pin the same three requirements so a drift fails on both sides.

OBSERVE-ONLY / FAIL-OPEN. This is an export of state the run already computed.
It must never fail the scoring chain: every error path logs a WARNING and
returns None (continue). The three skip guards are deliberate:

  * readonly/shadow lanes (``RENQUANT_READONLY_TAG`` set) never write — the
    prod lane owns the artifact, and per-lane writes would collide on the
    same date-keyed filename;
  * candidate-less runs never write — the intraday sell-only cycles run this
    job for holdings (n_candidates=0 on every intraday run, measured) and
    would otherwise overwrite the daily panel ~35x/session with a
    holdings-only matrix;
  * an empty/missing matrix never writes — an empty ``features`` mapping is
    rejected by the consumer contract, so writing one would produce a file
    that exists but cannot be loaded: worse than absence.

Writes are atomic (tmp + ``os.replace``) so a reader never sees a torn file.
A same-day rerun of the prod lane overwrites — deliberately: the freshest
serving state wins, and ``generated_at``/``run_id`` in the meta record which
run produced the surviving artifact.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from pathlib import Path
from typing import Any

import pandas as pd

from renquant_pipeline.kernel.pipeline.pipeline import Task

from ._data_root import data_root

log = logging.getLogger("kernel.panel_pipeline.feature_panel_export")

BUILDER_VERSION = "feature_panel_export_v1"


def _clean(v: Any) -> Any:
    """JSON-safe cell: non-finite floats become None (JSON has no NaN)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def write_feature_panel(
    X: pd.DataFrame,
    *,
    as_of: str,
    out_dir: Path,
    scorer_kind: str,
    run_id: str | None,
) -> tuple[Path, Path]:
    """Pure writer. Raises on invalid input; the Task wrapper is the fail-open layer."""
    if X is None or len(X) == 0 or len(X.columns) == 0:
        raise ValueError("refusing to write an empty feature panel — an empty "
                         "'features' mapping is rejected by the consumer contract")
    if not str(as_of).strip():
        raise ValueError("as_of (feature_cutoff) must be non-empty")
    features = {
        str(t): {str(c): _clean(row[c]) for c in X.columns}
        for t, row in X.iterrows()
    }
    payload = {
        "feature_cutoff": str(as_of),
        "builder_version": f"{BUILDER_VERSION}+{scorer_kind}",
        "features": features,
    }
    body = json.dumps(payload, sort_keys=True, allow_nan=False)
    sha = hashlib.sha256(body.encode()).hexdigest()
    meta = {
        "feature_cutoff": str(as_of),
        "builder_version": payload["builder_version"],
        "content_sha256": f"sha256:{sha}",
        "n_tickers": len(features),
        "n_columns": len(X.columns),
        "columns": [str(c) for c in X.columns],
        "run_id": run_id,
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "null_cells": sum(1 for r in features.values() for v in r.values() if v is None),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    panel_path = out_dir / f"feature_panel_{as_of}.json"
    meta_path = out_dir / f"feature_panel_{as_of}.meta.json"
    for path, text in ((panel_path, body), (meta_path, json.dumps(meta, sort_keys=True, indent=1))):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    return panel_path, meta_path


class ExportFeaturePanelTask(Task):
    """Observe-only export of the served feature matrix. NEVER fails the chain."""

    def run(self, ctx: Any) -> bool | None:  # noqa: ANN401
        try:
            if os.environ.get("RENQUANT_READONLY_TAG"):
                return None                     # shadow/readonly lane: prod owns the artifact
            if os.environ.get("RENQUANT_DISABLE_FEATURE_PANEL_EXPORT") == "1":
                return None                     # operator kill switch
            if not getattr(ctx, "candidates", None):
                return None                     # intraday holdings-only cycle
            X = getattr(ctx, "_panel_matrix", None)
            if X is None or len(getattr(X, "columns", [])) == 0 or len(X) == 0:
                return None                     # matrix-less scorer or empty frame
            today = getattr(ctx, "today", None)
            if today is None:
                log.warning("feature-panel export skipped: ctx.today missing")
                return None
            as_of = str(pd.Timestamp(today).date())
            stamp = getattr(ctx, "_active_panel_scorer", None) or {}
            panel_path, _ = write_feature_panel(
                X,
                as_of=as_of,
                out_dir=data_root() / "data" / "rq105",
                scorer_kind=str(stamp.get("kind") or "unknown"),
                run_id=getattr(ctx, "run_id", None),
            )
            log.info("feature panel exported: %s (%d tickers x %d cols)",
                     panel_path, len(X), len(X.columns))
        except Exception as exc:  # noqa: BLE001 — observe-only: never fail scoring
            log.warning("feature-panel export FAILED (scoring unaffected): %s: %s",
                        type(exc).__name__, exc)
        return None
