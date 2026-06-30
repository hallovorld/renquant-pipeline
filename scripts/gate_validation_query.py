#!/usr/bin/env python3
"""Conviction-gate validation query — read-only estimand over decision_outcomes.

Per Codex review on PR #152: the experiment/label logic lives HERE (in the
query layer), NOT in the persistence schema. The `decision_outcomes` VIEW
(see kernel.persistence) pre-joins each per-name decision to its realized
forward return and its benchmark-relative return (own fwd_Nd - SPY fwd_Nd).
This script computes the predefined estimand on top of that view.

THE ESTIMAND
------------
For a chosen horizon N in {1, 5, 20, 60} trading days, the conviction gate's
mu threshold is validated by the **benchmark-relative, net-of-cost mean
forward-return DIFFERENCE between names above vs below the mu threshold**::

    Delta_N = E[ rel_fwd_Nd - cost | mu >= mu_thresh ]
            - E[ rel_fwd_Nd - cost | mu <  mu_thresh ]

where ``rel_fwd_Nd = own_fwd_Nd - SPY_fwd_Nd`` (benchmark-relative, because the
gate is documented as calibrated E[R - SPY]) and ``cost`` is a flat round-trip
cost applied symmetrically (so it cancels in the difference unless the two
arms have different selection rates — it is carried explicitly for honesty and
for the absolute per-arm means). A POSITIVE, economically meaningful Delta_N
is the evidence the gate's mu threshold separates realized out-performers from
under-performers.

GUARDS (per Codex point 6 — selection bias, overlap, sample, threshold)
-----------------------------------------------------------------------
* run_type filter: **live only** by default. Sim is look-ahead-contaminated
  (IC grows with horizon); --include-sim is offered only for debugging.
* Date-level clustering: each run_date contributes ONE cohort mean per arm
  (the cross-section within a day is highly dependent), and the per-arm
  estimate is the mean over cohort-day means. The reported standard error is
  the cross-cohort (between-day) SE, so within-day dependence does not inflate
  the effective sample.
* Non-overlapping cohorts: for horizon N, overlapping windows make adjacent
  run_dates' labels mechanically dependent. We greedily keep run_dates spaced
  >= N trading days apart (a non-overlapping cohort set) before aggregating.
* Minimum effective-sample guard: requires at least ``--min-cohorts``
  non-overlapping cohort-days populated in BOTH arms; below that the result is
  reported as UNDERPOWERED and no decision is emitted.
* Predefined decision threshold: a verdict of PASS is emitted only when
  Delta_N >= ``--decision-bps`` (economic magnitude, in basis points) AND the
  effective-sample guard is met. This is a screening estimate, not a
  significance test; treat a PASS as "worth a proper validation", not proof.

This script is READ-ONLY: it opens the DB with ``mode=ro`` and never writes.

Usage::

    python scripts/gate_validation_query.py --db data/runs.alpaca.db
    python scripts/gate_validation_query.py --horizon 20 --mu-thresh 0.0
    python scripts/gate_validation_query.py --cost-bps 10 --decision-bps 25 --json

Exit codes
----------
  0  — query ran (verdict in stdout; PASS/FAIL/UNDERPOWERED is informational)
  1  — invalid args / DB missing / unreadable
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

_HORIZONS = (1, 5, 20, 60)


def _rel_col(horizon: int) -> str:
    if horizon not in _HORIZONS:
        raise ValueError(f"horizon must be one of {_HORIZONS}, got {horizon}")
    return f"rel_fwd_{horizon}d"


def _non_overlapping_dates(dates: list[str], min_gap_days: int) -> list[str]:
    """Greedily keep run_dates spaced >= ``min_gap_days`` calendar days apart.

    Calendar days are a conservative proxy for trading days (gap >= N calendar
    days implies <= N trading days, i.e. it never UNDER-spaces; it may drop a
    few extra dates, which only shrinks — never inflates — the effective
    sample). Dates are ISO 'YYYY-MM-DD'; input order is ascending.
    """
    import datetime as _dt

    kept: list[str] = []
    last: _dt.date | None = None
    for d in dates:
        cur = _dt.date.fromisoformat(d)
        if last is None or (cur - last).days >= min_gap_days:
            kept.append(d)
            last = cur
    return kept


def estimate_gate_separation(
    conn: sqlite3.Connection,
    *,
    horizon: int = 60,
    mu_thresh: float = 0.0,
    cost_bps: float = 10.0,
    include_sim: bool = False,
    role: str = "candidate",
    min_cohorts: int = 8,
    decision_bps: float = 25.0,
) -> dict:
    """Compute the benchmark-relative, net-of-cost above/below-mu separation.

    Returns a dict with the estimand, both per-arm means, the effective sample
    (non-overlapping cohort-days in each arm), and a predefined verdict. Pure
    read function over the ``decision_outcomes`` view — does not write.
    """
    rel = _rel_col(horizon)
    cost = cost_bps / 1e4
    run_type_clause = "" if include_sim else "AND run_type = 'live'"

    # One cohort mean per (run_date, arm). Date-level clustering: the daily
    # cross-section is collapsed to a single mean per arm so within-day
    # dependence does not inflate the sample. Only rows with a realized
    # benchmark-relative return at this horizon participate.
    sql = f"""
        SELECT run_date,
               CASE WHEN mu >= ? THEN 'above' ELSE 'below' END AS arm,
               AVG({rel}) AS cohort_mean,
               COUNT(*)   AS n_names
        FROM decision_outcomes
        WHERE role = ?
          AND mu IS NOT NULL
          AND {rel} IS NOT NULL
          {run_type_clause}
        GROUP BY run_date, arm
        ORDER BY run_date
    """
    cohorts: dict[str, dict[str, float]] = {"above": {}, "below": {}}
    for run_date, arm, cohort_mean, _n in conn.execute(sql, (mu_thresh, role)):
        if run_date is None or cohort_mean is None:
            continue
        cohorts[arm][run_date] = float(cohort_mean)

    # Non-overlapping cohort selection per arm (independent labels at horizon N).
    out: dict = {
        "horizon_days": horizon,
        "mu_thresh": mu_thresh,
        "cost_bps": cost_bps,
        "role": role,
        "run_type": "live+sim" if include_sim else "live",
        "min_cohorts": min_cohorts,
        "decision_bps": decision_bps,
    }
    arms_stats: dict[str, dict] = {}
    for arm in ("above", "below"):
        dates = sorted(cohorts[arm])
        kept = _non_overlapping_dates(dates, min_gap_days=horizon)
        means = [cohorts[arm][d] for d in kept]
        n = len(means)
        mean_gross = sum(means) / n if n else None
        # Net of a flat round-trip cost (applied symmetrically per arm).
        mean_net = (mean_gross - cost) if mean_gross is not None else None
        arms_stats[arm] = {
            "n_cohorts": n,
            "mean_rel_gross": mean_gross,
            "mean_rel_net": mean_net,
        }
    out["arms"] = arms_stats

    n_above = arms_stats["above"]["n_cohorts"]
    n_below = arms_stats["below"]["n_cohorts"]
    eff = min(n_above, n_below)
    out["effective_cohorts"] = eff

    a = arms_stats["above"]["mean_rel_net"]
    b = arms_stats["below"]["mean_rel_net"]
    delta = (a - b) if (a is not None and b is not None) else None
    out["delta_rel_net"] = delta
    out["delta_rel_net_bps"] = (delta * 1e4) if delta is not None else None

    if eff < min_cohorts or delta is None:
        out["verdict"] = "UNDERPOWERED"
    elif delta * 1e4 >= decision_bps:
        out["verdict"] = "PASS"
    else:
        out["verdict"] = "FAIL"
    return out


def _open_ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--db", default="data/runs.alpaca.db",
                   help="Path to the runs SQLite DB (opened read-only).")
    p.add_argument("--horizon", type=int, default=60, choices=_HORIZONS,
                   help="Forward-return horizon in trading days.")
    p.add_argument("--mu-thresh", type=float, default=0.0,
                   help="Conviction-gate mu threshold (above vs below).")
    p.add_argument("--cost-bps", type=float, default=10.0,
                   help="Flat round-trip cost in bps, applied per arm.")
    p.add_argument("--role", default="candidate",
                   help="decision_outcomes role to analyze (candidate|holding).")
    p.add_argument("--min-cohorts", type=int, default=8,
                   help="Minimum non-overlapping cohort-days per arm.")
    p.add_argument("--decision-bps", type=float, default=25.0,
                   help="Predefined economic decision threshold (Delta, bps).")
    p.add_argument("--include-sim", action="store_true",
                   help="DEBUG ONLY: include sim runs (look-ahead-contaminated).")
    p.add_argument("--json", action="store_true",
                   help="Emit the result as JSON.")
    args = p.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[ERROR] DB not found: {db_path}", file=sys.stderr)
        return 1
    try:
        conn = _open_ro(str(db_path))
    except sqlite3.Error as exc:
        print(f"[ERROR] cannot open DB read-only: {exc}", file=sys.stderr)
        return 1
    try:
        result = estimate_gate_separation(
            conn,
            horizon=args.horizon,
            mu_thresh=args.mu_thresh,
            cost_bps=args.cost_bps,
            include_sim=args.include_sim,
            role=args.role,
            min_cohorts=args.min_cohorts,
            decision_bps=args.decision_bps,
        )
    finally:
        conn.close()

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0

    print()
    print("=" * 64)
    print("  CONVICTION-GATE VALIDATION  (benchmark-relative, net-of-cost)")
    print("=" * 64)
    print(f"  horizon              {result['horizon_days']}d")
    print(f"  mu threshold         {result['mu_thresh']}")
    print(f"  cost                 {result['cost_bps']} bps")
    print(f"  run_type             {result['run_type']}  role={result['role']}")
    above, below = result["arms"]["above"], result["arms"]["below"]
    print(f"  above-mu  cohorts={above['n_cohorts']:>4}  "
          f"mean rel-net={_fmt_bps(above['mean_rel_net'])}")
    print(f"  below-mu  cohorts={below['n_cohorts']:>4}  "
          f"mean rel-net={_fmt_bps(below['mean_rel_net'])}")
    print(f"  effective cohorts    {result['effective_cohorts']} "
          f"(min {result['min_cohorts']})")
    print(f"  Delta (above-below)  {_fmt_bps(result['delta_rel_net'])}")
    print(f"  decision threshold   {result['decision_bps']} bps")
    print(f"  VERDICT              {result['verdict']}")
    print("=" * 64)
    return 0


def _fmt_bps(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 1e4:+.1f} bps"


if __name__ == "__main__":
    sys.exit(main())
