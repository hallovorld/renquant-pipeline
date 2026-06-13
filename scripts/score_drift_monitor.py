#!/usr/bin/env python3
"""Score-drift monitor — CLI companion to kernel.score_drift (L6 sidecar).

Design: renquant-orchestrator
doc/research/2026-06-12-engineering-architecture-deep-plan.md §L6 audit
sidecar + the operator's "self-audit task … early detect data abnormal"
mandate. Reads one or more runs DBs (read-only), computes the latest
full-run PSI vs the trailing baseline, prints a report, and signals via
exit code so an operator launchd/wrapper can fire the alert (the pipeline
library stays alerting-agnostic — ntfy lives in the umbrella).

Exit codes (worst across all DBs):
  0  all INFO (stable)
  1  any WARN or CRITICAL drift
  2  any DB has too few full runs to baseline (cannot measure = a signal)
  3  a DB path is unreadable

Usage:
  score_drift_monitor.py --db data/runs.alpaca.db [--db data/runs.alpaca_shadow.db]
                         [--trailing 20] [--json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from renquant_pipeline.kernel.score_drift import (  # noqa: E402
    DriftReport,
    load_score_drift_from_db,
)


def _open_ro(path: str) -> sqlite3.Connection:
    """Read-only connection — a monitor must never mutate the trade DB."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def monitor(db_paths: list[str], *, trailing: int = 20) -> tuple[int, list[dict]]:
    worst = 0
    out: list[dict] = []
    for path in db_paths:
        entry: dict = {"db": path}
        try:
            conn = _open_ro(path)
        except sqlite3.OperationalError as exc:
            entry.update(status="UNREADABLE", error=str(exc))
            out.append(entry)
            worst = max(worst, 3)
            continue
        try:
            report: DriftReport | None = load_score_drift_from_db(
                conn, trailing=trailing)
        finally:
            conn.close()
        if report is None:
            entry.update(status="INSUFFICIENT_DATA")
            worst = max(worst, 2)
        else:
            entry.update(status=report.severity, psi=report.psi,
                         n_baseline=report.n_baseline,
                         n_current=report.n_current, ok=report.ok)
            if not report.ok:
                worst = max(worst, 1)
        out.append(entry)
    return worst, out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", action="append", required=True,
                   help="runs DB path (repeatable)")
    p.add_argument("--trailing", type=int, default=20)
    p.add_argument("--json", action="store_true", help="emit JSON")
    args = p.parse_args()
    code, reports = monitor(args.db, trailing=args.trailing)
    if args.json:
        print(json.dumps({"exit_code": code, "reports": reports}, indent=2))
    else:
        for r in reports:
            if "psi" in r:
                print(f"{r['status']:14s} {r['db']}  PSI={r['psi']:.4f}  "
                      f"(baseline={r['n_baseline']} current={r['n_current']})")
            else:
                print(f"{r['status']:14s} {r['db']}  "
                      f"{r.get('error', '')}")
        print(f"exit={code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
