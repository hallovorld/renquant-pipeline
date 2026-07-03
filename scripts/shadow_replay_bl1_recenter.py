#!/usr/bin/env python3
"""Shadow replay for BL-1 per-bar raw recentering (M4) — read-only evidence.

Replays the stored raw panel scores of the last N FULL production runs
(``score_distribution.raw_panel``, candidates only) through the live global
calibrator twice:

  BEFORE  er_b = ER(raw)                       (legacy prod path)
  AFTER   er_a = ER(raw − median(raws) + neutral_raw)
                                                (recenter_raw_per_bar=true)

and reports, per run:

  * ``calibrator_sign_laundered`` before (raw sign vs μ sign — the BL-2
    prod counter; cross-checkable against the live log: 44 on 2026-07-01,
    45 on 2026-07-02) and after (RECENTERED sign vs μ sign — the M4
    acceptance metric, expected single digits / ~0);
  * the admission-set delta at the conviction floor (names crossing
    ``mu_floor`` = 0.03 in either direction) — the floor itself is NOT
    changed, only μ moves;
  * the μ distribution shift (mean/median/std/p10/p90/min/max);
  * a fidelity check: max |stored prod mu − replayed er_b| over matched
    rows, so any calibrator-vintage drift between the replayed artifact and
    what actually ran that day is visible instead of silent.

STRICTLY READ-ONLY: the DB is opened with ``mode=ro`` and nothing is ever
written back. Output goes to stdout and (optionally) an evidence JSON.

Usage:
  scripts/shadow_replay_bl1_recenter.py \
      --db /path/to/runs.alpaca.db \
      --calibrator /path/to/panel-rank-calibration.json \
      [--runs 6] [--min-candidates 60] [--mu-floor 0.03] \
      [--json-out doc/evidence/....json]
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import numpy as np  # noqa: E402

from renquant_pipeline.kernel.panel_pipeline.global_calibrator import (  # noqa: E402
    GlobalPanelCalibration,
)


def _open_ro(path: str) -> sqlite3.Connection:
    """Read-only connection — replay evidence must never mutate the trade DB."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _full_runs(
    conn: sqlite3.Connection, n_runs: int, min_candidates: int
) -> list[tuple[str, str, int]]:
    """Most recent N runs with a full candidate cross-section."""
    rows = conn.execute(
        """
        SELECT run_id, date, COUNT(*) AS n
        FROM score_distribution
        WHERE is_holding = 0 AND raw_panel IS NOT NULL
        GROUP BY run_id, date
        HAVING n >= ?
        ORDER BY date DESC, run_id DESC
        LIMIT ?
        """,
        (min_candidates, n_runs),
    ).fetchall()
    return [(r[0], r[1], int(r[2])) for r in rows]


def _dist(values: list[float]) -> dict[str, float]:
    arr = np.asarray(values, dtype=float)
    return {
        "mean": float(arr.mean()),
        "median": float(np.median(arr)),
        "std": float(arr.std()),
        "p10": float(np.percentile(arr, 10)),
        "p90": float(np.percentile(arr, 90)),
        "min": float(arr.min()),
        "max": float(arr.max()),
    }


def replay_run(
    conn: sqlite3.Connection,
    cal: GlobalPanelCalibration,
    run_id: str,
    mu_floor: float,
) -> dict:
    rows = conn.execute(
        """
        SELECT ticker, raw_panel, mu
        FROM score_distribution
        WHERE run_id = ? AND is_holding = 0 AND raw_panel IS NOT NULL
        ORDER BY ticker
        """,
        (run_id,),
    ).fetchall()
    anchor = cal.neutral_raw
    if anchor is None:
        raise SystemExit(
            "calibrator has no ER=0 neutral_raw anchor — BL-1 recentering "
            "has nothing to align onto; replay is meaningless for this "
            "artifact."
        )

    tickers: list[str] = []
    raws: list[float] = []
    stored_mu: list[float | None] = []
    for t, raw, mu in rows:
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v):
            continue
        tickers.append(str(t))
        raws.append(v)
        stored_mu.append(float(mu) if mu is not None else None)

    center = float(np.median(raws))
    er_b = [cal.expected_return(r) for r in raws]
    er_a = [cal.expected_return(r - center + anchor) for r in raws]

    laundered_before = sum(1 for r, e in zip(raws, er_b) if r * e < 0.0)
    laundered_after = sum(
        1 for r, e in zip(raws, er_a) if (r - center) * e < 0.0
    )

    admit_b = {t for t, e in zip(tickers, er_b) if e >= mu_floor}
    admit_a = {t for t, e in zip(tickers, er_a) if e >= mu_floor}
    gained = sorted(admit_a - admit_b)
    lost = sorted(admit_b - admit_a)

    # Fidelity: the prod pipeline stored its calibrated mu per row
    # (use_calibrator_mu=true, horizon 60 == native 60). If the artifact we
    # replay is the one that ran that day, er_b reproduces it ~exactly.
    diffs = [
        abs(e - m) for e, m in zip(er_b, stored_mu) if m is not None
    ]
    fidelity = {
        "n_rows_with_stored_mu": len(diffs),
        "max_abs_diff_replayed_vs_stored_mu": (
            float(max(diffs)) if diffs else None
        ),
    }

    return {
        "run_id": run_id,
        "n_candidates": len(raws),
        "cross_section_center_median": center,
        "recenter_shift": anchor - center,
        "sign_laundered_before": laundered_before,
        "sign_laundered_after": laundered_after,
        "admitted_at_mu_floor_before": len(admit_b),
        "admitted_at_mu_floor_after": len(admit_a),
        "admission_gained": gained,
        "admission_lost": lost,
        "mu_distribution_before": _dist(er_b),
        "mu_distribution_after": _dist(er_a),
        "fidelity_check": fidelity,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, help="runs DB (opened READ-ONLY)")
    ap.add_argument("--calibrator", required=True,
                    help="global_panel_calibration JSON artifact")
    ap.add_argument("--runs", type=int, default=6,
                    help="number of most-recent full runs to replay")
    ap.add_argument("--min-candidates", type=int, default=60,
                    help="minimum candidate rows for a run to count as FULL")
    ap.add_argument("--mu-floor", type=float, default=0.03,
                    help="conviction floor for the admission-set delta")
    ap.add_argument("--json-out", default=None,
                    help="write the evidence JSON here (optional)")
    args = ap.parse_args(argv)

    cal = GlobalPanelCalibration.load(args.calibrator)
    cal_sha = hashlib.sha256(Path(args.calibrator).read_bytes()).hexdigest()
    conn = _open_ro(args.db)
    try:
        runs = _full_runs(conn, args.runs, args.min_candidates)
        if not runs:
            print("no full runs found", file=sys.stderr)
            return 2
        results = []
        for run_id, run_date, _n in runs:
            res = replay_run(conn, cal, run_id, args.mu_floor)
            res["date"] = run_date
            results.append(res)
    finally:
        conn.close()

    evidence = {
        "tool": "scripts/shadow_replay_bl1_recenter.py",
        "purpose": (
            "M4/BL-1 shadow replay: per-bar raw recentering "
            "(recenter_raw_per_bar) before/after on stored prod panel scores"
        ),
        "generated_at": datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(timespec="seconds"),
        "db": str(args.db),
        "calibrator": {
            "path": str(args.calibrator),
            "file_sha256": cal_sha,
            "trained_date": cal.metadata.get("trained_date"),
            "neutral_raw": cal.neutral_raw,
            "native_horizon_days": cal.metadata.get("lookahead_days_used"),
        },
        "mu_floor": args.mu_floor,
        "notes": (
            "READ-ONLY replay. BEFORE = ER(raw) as prod computes today "
            "(rotation horizon 60d == native 60d, no scaling). AFTER = "
            "ER(raw - median(cross-section) + neutral_raw), i.e. the "
            "recenter_raw_per_bar=true path. sign_laundered_after counts "
            "against the RECENTERED sign (the M4 acceptance metric). The "
            "fidelity_check compares BEFORE vs the mu the pipeline actually "
            "stored that day; a non-zero diff means the day ran a different "
            "weekly calibrator vintage than the replayed artifact."
        ),
        "runs": results,
    }

    # per-run table
    hdr = (
        f"{'date':<11}{'run_id':<28}{'n':>4}{'center':>9}"
        f"{'laund_b':>9}{'laund_a':>9}{'admit_b':>9}{'admit_a':>9}"
        f"{'gained':>8}{'lost':>6}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        print(
            f"{r['date']:<11}{r['run_id']:<28}{r['n_candidates']:>4}"
            f"{r['cross_section_center_median']:>9.4f}"
            f"{r['sign_laundered_before']:>9}{r['sign_laundered_after']:>9}"
            f"{r['admitted_at_mu_floor_before']:>9}"
            f"{r['admitted_at_mu_floor_after']:>9}"
            f"{len(r['admission_gained']):>8}{len(r['admission_lost']):>6}"
        )

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"\nevidence JSON -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
