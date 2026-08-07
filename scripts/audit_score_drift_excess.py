#!/usr/bin/env python3
"""Excess-over-floor audit for a runs DB's `score_drift_audits` log.

Re-bands every historical row by `excess = psi / null_psi_floor(n_baseline,
n_current)` instead of raw PSI. Written as the runnable reproduction for the
`[VERIFIED]` claims in doc/progress/2026-08-07-score-drift-noise-floor.md
(PR #279 review finding 2 — those tags cited a date/table name instead of a
runnable command/file).

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

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from renquant_pipeline.kernel.score_drift import null_psi_floor  # noqa: E402

_BANDS = [("<1.0", 0.0, 1.0), ("1.0-1.5", 1.0, 1.5), ("1.5-2.0", 1.5, 2.0),
          ("2.0-3.0", 2.0, 3.0), (">=3.0", 3.0, float("inf"))]


def audit(db_path: str) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT psi, severity, n_baseline, n_current "
            "FROM score_drift_audits").fetchall()
    finally:
        conn.close()
    counts = {label: 0 for label, _, _ in _BANDS}
    excesses: list[float] = []
    critical_below_floor = 0
    for psi_val, severity, n_baseline, n_current in rows:
        if psi_val is None or n_baseline is None or n_current is None:
            continue
        floor = null_psi_floor(int(n_baseline), int(n_current))
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
    print(f"n_rows={result['n_rows']} n_scored={n}")
    for label, count in result["counts"].items():
        pct = 100 * count / n if n else float("nan")
        print(f"excess {label:8s} {count:5d}  {pct:5.1f}%")
    print(f"median {result['median_excess']:.2f}x  max {result['max_excess']:.1f}x")
    print(f"CRITICAL rows below floor: {result['critical_below_floor']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
