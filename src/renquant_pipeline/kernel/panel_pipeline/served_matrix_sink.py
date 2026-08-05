"""Persist the SERVED feature matrix and the per-name decision surface.

WHY THIS EXISTS (orch#703, measured 2026-08-01, priority raised 2026-08-04):
``build_inference_matrix`` produces the matrix that decides every trade, and
nothing ever writes it down — ``job_panel_scoring`` reads it as
``ctx._panel_matrix``, an in-memory attribute that ceases to exist when the run
ends. The consequence is not abstract: with the GOAL-9 fleet live, five lanes
now pick DIFFERENT names on the same day (2026-08-04: prod NVDA/GOOG/WELL/VLO,
RC AMZN, RSs SPG, RCS BWXT), and there is no way to answer "why" the next
morning, because the inputs are gone. Any comparison between lanes, or between
a model and its replacement, has to be a reconstruction rather than the thing
that was actually scored.

This module is a SINK, not a gate. It reads what the pipeline already computed
and writes it next to the run. It must never change a decision and never break
a run: every entry point is wrapped by the caller's fail-open guard, and this
module raises only ``ServedMatrixSinkError``, which the task swallows.

Layout, one directory per session::

    <strategy_dir>/logs/served_matrix/<YYYY-MM-DD>/<lane>__<run_id>.parquet
    <strategy_dir>/logs/served_matrix/<YYYY-MM-DD>/<lane>__<run_id>.json

The parquet carries one row per scored ticker: every served feature column,
plus the decision surface (``panel_score``, ``rank_score``, ``mu``, ``sigma``,
``kelly_target_pct``) and role flags. The JSON carries the identity needed to
make the parquet interpretable years later — scorer kind, content digest,
config fingerprint, trained date, and, for a blend, each component's identity.

Growth: ~145 rows x ~172 float columns is ~100 KB per lane-run; six lanes daily
is ~0.6 MB/day, ~0.2 GB/year. Nothing here deletes anything — retention is an
operator decision, not a side effect of a logging path.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_VERSION = "served-matrix-1"
DEFAULT_SERVED_MATRIX_RELDIR = Path("logs") / "served_matrix"

# The decision surface worth keeping beside the features. Every one of these is
# already computed by the time the sink runs; none is recomputed here.
DECISION_FIELDS = ("panel_score", "rank_score", "mu", "sigma", "kelly_target_pct")


class ServedMatrixSinkError(RuntimeError):
    """Anything that goes wrong while persisting. Never propagates past the task."""


def served_matrix_cfg(config: dict | None) -> dict:
    raw = (config or {}).get("served_matrix")
    return raw if isinstance(raw, dict) else {}


def served_matrix_dir(config: dict | None) -> Path:
    """Resolve the per-session output directory.

    Default ``<config["_strategy_dir"]>/logs/served_matrix``, mirroring the
    shadow-health sink convention so a lane that logs health also logs inputs.
    Overridable via ``config["served_matrix"]["dir"]``.
    """
    override = served_matrix_cfg(config).get("dir")
    if override:
        return Path(str(override))
    strategy_dir = (config or {}).get("_strategy_dir")
    base = Path(str(strategy_dir)) if strategy_dir else Path(".")
    return base / DEFAULT_SERVED_MATRIX_RELDIR


def served_matrix_sink_defined(config: dict | None) -> bool:
    """True when an output location is explicitly configured.

    Same predicate shape as ``shadow_health_sink_defined``: without a
    ``_strategy_dir`` or an explicit override, the writer SKIPS rather than
    scatter parquet files into a bare cwd (a sim or unit test).
    """
    if served_matrix_cfg(config).get("enabled") is False:
        return False
    return bool(served_matrix_cfg(config).get("dir")) or bool(
        (config or {}).get("_strategy_dir"))


def _clean(value: Any) -> Any:
    """Coerce to something json/parquet will accept, or None. Never raises."""
    if value is None:
        return None
    if isinstance(value, (bool, int, str)):
        return value
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return None if f != f else f  # NaN -> None, so "absent" is not a number


def _scorer_identity(scorer: Any) -> dict:
    """Read identity off the loaded scorer WITHOUT inventing keys.

    Anything not present is recorded as None. A missing field must read as
    missing, never as a default that looks like a measurement.
    """
    meta = getattr(scorer, "metadata", None)
    meta = meta if isinstance(meta, dict) else {}
    identity = {
        "kind": _clean(meta.get("kind")),
        "trained_date": _clean(meta.get("trained_date")),
        "config_fingerprint": _clean(meta.get("config_fingerprint")),
        "content_sha256": _clean(meta.get("content_sha256")),
        "n_feature_cols": len(getattr(scorer, "feature_cols", []) or []),
    }
    components = getattr(scorer, "components", None)
    if isinstance(components, (list, tuple)) and components:
        identity["components"] = [
            {
                "kind": _clean(getattr(c, "kind", None)
                               or (c.get("kind") if isinstance(c, dict) else None)),
                "content_sha256": _clean(getattr(c, "content_sha256", None)
                                         or (c.get("content_sha256")
                                             if isinstance(c, dict) else None)),
                "config_fingerprint": _clean(
                    getattr(c, "config_fingerprint", None)
                    or (c.get("config_fingerprint") if isinstance(c, dict) else None)),
            }
            for c in components
        ]
    return identity


def build_records(ctx: Any) -> tuple[list[dict], dict]:
    """Assemble (rows, manifest) from a finished panel-scoring context.

    Pure: reads only, computes nothing the pipeline did not already compute.
    """
    X = getattr(ctx, "_panel_matrix", None)
    if X is None or not hasattr(X, "index"):
        raise ServedMatrixSinkError("no served matrix on the context")

    candidates = {str(c.ticker): c for c in (getattr(ctx, "candidates", None) or [])}
    holdings = {str(k): v for k, v in (getattr(ctx, "holdings", None) or {}).items()}

    feature_cols = [str(c) for c in getattr(X, "columns", [])]
    rows: list[dict] = []
    for ticker in [str(t) for t in X.index]:
        try:
            feats = X.loc[ticker]
        except Exception:  # noqa: BLE001 - a row that cannot be read is skipped, loudly
            log.warning("served_matrix: could not read row %s; skipped", ticker)
            continue
        row: dict[str, Any] = {"ticker": ticker}
        for col in feature_cols:
            try:
                row[col] = _clean(feats[col])
            except Exception:  # noqa: BLE001
                row[col] = None
        obj = candidates.get(ticker) or holdings.get(ticker)
        for field in DECISION_FIELDS:
            row[field] = _clean(getattr(obj, field, None)) if obj is not None else None
        row["is_candidate"] = ticker in candidates
        row["is_holding"] = ticker in holdings
        rows.append(row)

    # run_id / lane resolution follows the PROVEN idiom in
    # kernel/pipeline/task_decision_ledger.py:56 and the broker-isolation tag
    # `ctx.broker_name` (context.py:34, set by RunnerAdapter). Neither is
    # invented here: an unresolvable value is recorded as the literal
    # "unscoped"/"unlaned" so a reader can tell it was ABSENT, not defaulted.
    today = getattr(ctx, "today", None)
    date_iso = today.isoformat() if hasattr(today, "isoformat") else str(today)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": _clean(getattr(ctx, "run_id", None)
                         or getattr(ctx, "_run_id", None)
                         or f"{date_iso}-unscoped"),
        "as_of_date": date_iso,
        "lane": _clean(getattr(ctx, "broker_name", None)),
        "n_rows": len(rows),
        "n_feature_cols": len(feature_cols),
        "feature_cols": feature_cols,
        "n_candidates": len(candidates),
        "n_holdings": len(holdings),
        "buy_blocked": bool(getattr(ctx, "buy_blocked", False)),
        "scorer": _scorer_identity(getattr(ctx, "_panel_scorer", None)),
    }
    return rows, manifest


def write_served_matrix(out_dir: Path, rows: list[dict], manifest: dict) -> Path:
    """Write ``<out_dir>/<date>/<lane>__<run_id>.{parquet,json}``; return the parquet.

    PAIR ATOMICITY. Both temps are materialised before anything is swapped;
    then the stale sidecar is dropped, the parquet is replaced, and the sidecar
    is replaced last. The only intermediate state a reader can observe is
    "parquet present, sidecar absent" — which the contract defines as
    INCOMPLETE. A reader must therefore treat a parquet with no sidecar as
    unusable, and never sees a NEW parquet paired with an OLD sidecar.
    """
    import os  # noqa: PLC0415
    import pandas as pd  # noqa: PLC0415

    if not rows:
        raise ServedMatrixSinkError("nothing to persist (0 rows)")
    date = str(manifest.get("as_of_date") or "undated")[:10]
    lane = str(manifest.get("lane") or "unlaned")
    run_id = str(manifest.get("run_id") or "unrun")
    stem = f"{lane}__{run_id}".replace("/", "_")
    day_dir = Path(out_dir) / date
    day_dir.mkdir(parents=True, exist_ok=True)

    parquet = day_dir / f"{stem}.parquet"
    sidecar = day_dir / f"{stem}.json"
    tmp_parquet = parquet.with_suffix(".parquet.incoming")
    tmp_json = sidecar.with_suffix(".json.incoming")
    try:
        # BOTH temps are fully materialised BEFORE anything is swapped, so a
        # failure while building either one cannot touch what is already served.
        pd.DataFrame(rows).to_parquet(tmp_parquet, index=False)
        tmp_json.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str),
                            encoding="utf-8")
        # [codex on orch#268] Swapping the parquet first left a REWRITE of the
        # same <lane>__<run_id> incoherent if the sidecar swap then failed: a new
        # parquet paired with the PREVIOUS run's sidecar — a mismatched pair that
        # reads as evidence. Dropping the stale sidecar first makes the only
        # reachable intermediate state "parquet present, sidecar absent", which
        # the contract already defines as INCOMPLETE.
        sidecar.unlink(missing_ok=True)
        os.replace(tmp_parquet, parquet)
        os.replace(tmp_json, sidecar)
    finally:
        Path(tmp_parquet).unlink(missing_ok=True)
        Path(tmp_json).unlink(missing_ok=True)
    return parquet


__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_SERVED_MATRIX_RELDIR",
    "DECISION_FIELDS",
    "ServedMatrixSinkError",
    "served_matrix_cfg",
    "served_matrix_dir",
    "served_matrix_sink_defined",
    "build_records",
    "write_served_matrix",
]
