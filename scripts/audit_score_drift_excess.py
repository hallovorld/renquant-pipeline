#!/usr/bin/env python3
"""Excess-over-floor audit for a runs DB's `score_drift_audits` log.

Re-bands every historical row whose baseline can be proven identical to the
one it was originally audited against by `excess = psi / null_psi_floor(baseline,
n_current)`. Written as the runnable reproduction for the `[VERIFIED]` claims
in doc/progress/2026-08-07-score-drift-noise-floor.md (PR #279 review finding
2 — those tags cited a date/table name instead of a runnable command/file).

`null_psi_floor` conditions the null on the REAL baseline array (PR #279
review, P1): `psi()` bins on `np.quantile(expected, ...)`, so a tied baseline
collapses those edges and a same-size Gaussian floor is not that statistic's
null. `score_drift_audits` did not persist the raw baseline scores until PR
#280 added `baseline_run_ids_json` (the exact `run_id`s that made up the
baseline, in trailing-window order — see `kernel.score_drift.DriftReport
.baseline_run_ids`); before that this script inferred the window by taking
whatever prefix of currently-surviving full runs matched the stored
`n_baseline` *count*. That count-only check is NOT sufficient proof (PR #280
review, P1): `candidate_scores` prunes whole runs over time, and when an
original trailing run is pruned while an older, un-pruned run happens to
hold the same number of scores, the count-based reconstruction silently
substitutes the wrong run into the window and still reports a "verified"
match. This script now only scores a row when EVERY `run_id` in its
persisted `baseline_run_ids_json` still has raw scores in
`candidate_scores` — an exact identity match, not an inferred one — with the
resulting size checked against the stored `n_baseline` as a final sanity
backstop. Rows written before `baseline_run_ids_json` existed carry no
provenance at all and are `n_unreconstructable` unconditionally; there is no
way to retroactively prove which runs backed them. Coverage grows only as
new audits accrue under the new schema.

Read-only: opens the DB with mode=ro and never writes.

Usage (the runs DB lives in the umbrella repo, not this one):
  audit_score_drift_excess.py --db /Users/renhao/git/github/RenQuant/data/runs.alpaca.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from renquant_pipeline.kernel.score_drift import null_psi_floor  # noqa: E402

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


def _reconstruct_baseline(by_run: dict[str, list[float]],
                          baseline_run_ids: list[str] | None) -> np.ndarray | None:
    """The exact baseline a row was audited against, from its persisted
    `baseline_run_ids_json`. None when the row has no persisted provenance
    (predates PR #280), or any one of its constituent runs has since been
    pruned from `candidate_scores` — a partial or substitute reconstruction
    is not accepted (PR #280 review, P1)."""
    if not baseline_run_ids:
        return None
    if not all(rid in by_run for rid in baseline_run_ids):
        return None
    return np.array([s for rid in baseline_run_ids for s in by_run[rid]])


def audit(db_path: str) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # A DB opened mode=ro that predates PR #280's migration has no
        # baseline_run_ids_json column at all — this script never writes,
        # so it cannot ALTER TABLE to add it. Every row on such a DB has no
        # provenance and is unreconstructable; degrade the query instead of
        # crashing on "no such column".
        has_provenance = any(
            r[1] == "baseline_run_ids_json"
            for r in conn.execute("PRAGMA table_info(score_drift_audits)"))
        select = ("run_id, psi, severity, n_baseline, n_current, "
                  "baseline_run_ids_json" if has_provenance else
                  "run_id, psi, severity, n_baseline, n_current")
        rows = conn.execute(
            f"SELECT {select} FROM score_drift_audits").fetchall()
        by_run = _load_full_runs(conn)
    finally:
        conn.close()
    counts = {label: 0 for label, _, _ in _BANDS}
    excesses: list[float] = []
    critical_below_floor = 0
    n_unreconstructable = 0
    for row in rows:
        if has_provenance:
            run_id, psi_val, severity, n_baseline, n_current, baseline_ids_json = row
        else:
            run_id, psi_val, severity, n_baseline, n_current = row
            baseline_ids_json = None
        if psi_val is None or n_current is None or run_id is None:
            continue
        baseline_run_ids = json.loads(baseline_ids_json) if baseline_ids_json else None
        baseline = _reconstruct_baseline(by_run, baseline_run_ids)
        if baseline is None or n_baseline is None or baseline.size != int(n_baseline):
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
