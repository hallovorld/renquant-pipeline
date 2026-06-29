#!/usr/bin/env python3
"""Backfill decision_ledger.fwd_* + is_winner_60d from ticker_forward_returns.

Conviction-gate validation prep (2026-06-29). The decision_ledger writer
(`record_decision_ledger`) appends one APPEND-ONLY row per (run, ticker)
decision with its factors (raw_score / mu / expected_return / selected /
blocked_by / regime), leaving the realized forward-return columns NULL.

This script is the out-of-band backfill cron: it JOINs decision_ledger ×
ticker_forward_returns on (ticker, run_date == as_of_date) and UPDATEs each
row's fwd_1d / fwd_5d / fwd_20d / fwd_60d, then derives is_winner_60d
(1 if fwd_60d > 0 else 0; NULL while fwd_60d is unrealized).

Mirrors scripts/backfill_forward_returns.py / backfill_trade_evaluations.py:
reads ticker_forward_returns, writes only decision_ledger. Idempotent — re-run
any time; rows whose forward window has not yet elapsed stay NULL and get
filled on a later run. Production safety: never mutates ticker_forward_returns,
candidate_scores, or any live state file. Read-only against price-derived data.

Usage::

    python scripts/backfill_decision_ledger_returns.py
    python scripts/backfill_decision_ledger_returns.py --db data/runs.alpaca.db
    python scripts/backfill_decision_ledger_returns.py --since 2026-04-01
    python scripts/backfill_decision_ledger_returns.py --dry-run

Exit codes
----------
  0  — backfill completed (n_updated reported in stdout)
  1  — invalid args / DB missing
"""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("backfill-decision-ledger")


def _backfill(
    conn: sqlite3.Connection,
    since: str | None,
    dry_run: bool,
) -> int:
    """UPDATE decision_ledger fwd_* + is_winner_60d from ticker_forward_returns.

    The join key is (ticker, run_date == as_of_date). Only rows that still
    have a NULL fwd_60d AND a matching realized forward return are touched, so
    re-runs are cheap and idempotent. is_winner_60d is derived in the same
    statement: 1 if fwd_60d > 0, 0 if fwd_60d <= 0, NULL while fwd_60d is
    still unrealized.

    Returns the number of decision_ledger rows updated.
    """
    where_since = ""
    params: list = []
    if since:
        where_since = "AND dl.run_date >= ?"
        params.append(since)

    # Count what we WOULD touch (dry-run) / DID touch (real run).
    count_sql = f"""
        SELECT COUNT(*)
        FROM decision_ledger dl
        JOIN ticker_forward_returns tfr
          ON tfr.ticker     = dl.ticker
         AND tfr.as_of_date = dl.run_date
        WHERE dl.fwd_60d IS NULL
          AND tfr.fwd_60d IS NOT NULL
          {where_since}
    """
    n_target = conn.execute(count_sql, params).fetchone()[0]

    if dry_run:
        log.info("--dry-run: %d decision_ledger row(s) WOULD be updated", n_target)
        return n_target

    # Correlated UPDATE: fill the forward-return columns from the matching
    # ticker_forward_returns row, then derive is_winner_60d. We refresh ALL
    # fwd_* whenever fwd_60d becomes available so a row's shorter-horizon
    # columns are populated in the same pass.
    update_sql = f"""
        UPDATE decision_ledger AS dl
        SET fwd_1d = (
                SELECT tfr.fwd_1d FROM ticker_forward_returns tfr
                WHERE tfr.ticker = dl.ticker AND tfr.as_of_date = dl.run_date),
            fwd_5d = (
                SELECT tfr.fwd_5d FROM ticker_forward_returns tfr
                WHERE tfr.ticker = dl.ticker AND tfr.as_of_date = dl.run_date),
            fwd_20d = (
                SELECT tfr.fwd_20d FROM ticker_forward_returns tfr
                WHERE tfr.ticker = dl.ticker AND tfr.as_of_date = dl.run_date),
            fwd_60d = (
                SELECT tfr.fwd_60d FROM ticker_forward_returns tfr
                WHERE tfr.ticker = dl.ticker AND tfr.as_of_date = dl.run_date),
            is_winner_60d = (
                SELECT CASE WHEN tfr.fwd_60d IS NULL THEN NULL
                            WHEN tfr.fwd_60d > 0 THEN 1 ELSE 0 END
                FROM ticker_forward_returns tfr
                WHERE tfr.ticker = dl.ticker AND tfr.as_of_date = dl.run_date)
        WHERE dl.fwd_60d IS NULL
          AND EXISTS (
                SELECT 1 FROM ticker_forward_returns tfr
                WHERE tfr.ticker = dl.ticker
                  AND tfr.as_of_date = dl.run_date
                  AND tfr.fwd_60d IS NOT NULL)
          {where_since}
    """
    cur = conn.execute(update_sql, params)
    conn.commit()
    return cur.rowcount


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--db", default="data/runs.alpaca.db",
                   help="Path to the runs SQLite DB.")
    p.add_argument("--since", default=None,
                   help="Only backfill ledger rows whose run_date is on or "
                        "after this ISO date (e.g. '2026-04-01'). Default: all.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would be updated without writing.")
    args = p.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        log.error("DB not found: %s", db_path)
        return 1

    log.info("Backfilling decision_ledger forward returns (db=%s, since=%s)",
             db_path, args.since or "all")

    started = _dt.datetime.now(_dt.timezone.utc)
    conn = sqlite3.connect(db_path)
    try:
        # Ensure the table exists (a fresh DB may predate this feature).
        from renquant_pipeline.kernel.persistence import ensure_schema  # noqa: PLC0415
        ensure_schema(conn)
        n_updated = _backfill(conn, since=args.since, dry_run=args.dry_run)
    finally:
        conn.close()

    finished = _dt.datetime.now(_dt.timezone.utc)
    print()
    print("=" * 60)
    print("  DECISION LEDGER FORWARD-RETURN BACKFILL")
    print("=" * 60)
    print(f"  rows {'to update' if args.dry_run else 'updated'}      {n_updated}")
    print(f"  since                {args.since or 'all'}")
    print(f"  dry-run              {args.dry_run}")
    print(f"  wall seconds         {(finished - started).total_seconds():.1f}")
    print(f"  db                   {db_path}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
