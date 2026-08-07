#!/usr/bin/env python3
"""Excess-over-floor audit for a runs DB's `score_drift_audits` log.

Re-bands every historical row whose baseline can still be reconstructed from
`candidate_scores` by `excess = psi / null_psi_floor(baseline, n_current)`.
Written as the runnable reproduction for the `[VERIFIED]` claims in
doc/progress/2026-08-07-score-drift-noise-floor.md (PR #279 review finding 2
— those tags cited a date/table name instead of a runnable command/file).

`null_psi_floor` now conditions the null on the REAL baseline array (PR #279
review, P1): `psi()` bins on `np.quantile(expected, ...)`, so a tied baseline
collapses those edges and a same-size Gaussian floor is not that statistic's
null. `score_drift_audits` only ever persisted `n_baseline`/`n_current`
counts, never the raw scores, so this script rebuilds each row's baseline
from `candidate_scores` (keyed by `run_id`, same trailing-window logic as
`kernel.score_drift.load_score_drift_from_db`). `candidate_scores` is pruned
over time, so older rows' raw scores are gone; those are reported as
`n_unreconstructable` rather than silently reusing the old, now-known-wrong
shape-only approximation.

Read-only: opens the DB with mode=ro and never writes.

Usage:
  audit_score_drift_excess.py --db data/runs.alpaca.db
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from renquant_pipeline.kernel.score_drift import (  # noqa: E402
    MIN_SCORES_PER_RUN, null_psi_floor,
)

_BANDS = [("<1.0", 0.0, 1.0), ("1.0-1.5", 1.0, 1.5), ("1.5-2.0", 1.5, 2.0),
          ("2.0-3.0", 2.0, 3.0), (">=3.0", 3.0, float("inf"))]


def _load_full_runs(conn) -> dict[str, list[float]]:
    """run_id -> rank_score list, for every run still holding raw scores."""
    rows = conn.execute(
        "SELECT run_id, rank_score FROM candidate_scores "
        "WHERE rank_score IS NOT NULL").fetchall()
    by_run: dict[str, list[float]] = {}
    for run_id, score in rows:
        by_run.setdefault(str(run_id), []).append(float(score))
    return by_run


def _reconstruct_baseline(by_run: dict[str, list[float]], full_sorted: list[str],
                          run_id: str, *, trailing: int = 20) -> np.ndarray | None:
    """The trailing-N-full-run baseline `load_score_drift_from_db` would have
    built when `run_id` was the latest run. None when `run_id` itself isn't a
    full run still on disk, or it has no preceding full runs left."""
    if run_id not in full_sorted:
        return None
    idx = full_sorted.index(run_id)
    baseline_ids = full_sorted[max(0, idx - trailing):idx]
    if not baseline_ids:
        return None
    return np.array([s for rid in baseline_ids for s in by_run[rid]])


def audit(db_path: str, *, trailing: int = 20) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT run_id, psi, severity, n_current "
            "FROM score_drift_audits").fetchall()
        by_run = _load_full_runs(conn)
    finally:
        conn.close()
    full_sorted = sorted(rid for rid, vals in by_run.items()
                         if len(vals) >= MIN_SCORES_PER_RUN)
    counts = {label: 0 for label, _, _ in _BANDS}
    excesses: list[float] = []
    critical_below_floor = 0
    n_unreconstructable = 0
    for run_id, psi_val, severity, n_current in rows:
        if psi_val is None or n_current is None or run_id is None:
            continue
        baseline = _reconstruct_baseline(by_run, full_sorted, str(run_id),
                                         trailing=trailing)
        if baseline is None:
            n_unreconstructable += 1
            continue
        floor = null_psi_floor(baseline, int(n_current))
        if not (floor == floor and floor > 0):  # NaN-safe: skip unusable floors
            continue
        excess = psi_val / floor
        excesses.append(excess)
        for label, lo, hi in _BANDS:
            if lo <= excess < hi:
                counts[label] += 1
                break
        if severity == "CRITICAL" and excess < 1.0:
            critical_below_floor += 1
    return {
        "n_rows": len(rows),
        "n_unreconstructable": n_unreconstructable,
        "n_scored": len(excesses),
        "counts": counts,
        "median_excess": statistics.median(excesses) if excesses else float("nan"),
        "max_excess": max(excesses) if excesses else float("nan"),
        "critical_below_floor": critical_below_floor,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True, help="runs DB path")
    args = p.parse_args()
    result = audit(args.db)
    n = result["n_scored"]
    print(f"n_rows={result['n_rows']} "
          f"n_unreconstructable={result['n_unreconstructable']} n_scored={n}")
    for label, count in result["counts"].items():
        pct = 100 * count / n if n else float("nan")
        print(f"excess {label:8s} {count:5d}  {pct:5.1f}%")
    print(f"median {result['median_excess']:.2f}x  max {result['max_excess']:.1f}x")
    print(f"CRITICAL rows below floor: {result['critical_below_floor']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
