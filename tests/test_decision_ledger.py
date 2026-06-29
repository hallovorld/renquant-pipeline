"""decision_ledger append-only ledger tests (gate-validation prep, 2026-06-29).

Pins: table creation; writer idempotency (INSERT OR IGNORE on a duplicate
(run_id, ticker)); None-safety (NaN/inf/missing → NULL, conn/run_id None →
no-op); and the backfill JOIN populating fwd_60d + is_winner_60d from
ticker_forward_returns. Mirrors tests/test_gate_verdicts_ledger.py style.
"""
from __future__ import annotations

import datetime
import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from renquant_pipeline.kernel.persistence import (
    get_connection,
    record_decision_ledger,
    record_forward_returns,
)

RUN_DATE = datetime.date(2026, 4, 22)
_REPO = Path(__file__).resolve().parent.parent


def _conn(tmp_path):
    return get_connection({"persistence": {"enabled": True,
                                           "db_path": str(tmp_path / "runs.db")}})


def _cand(ticker, *, raw_score=None, mu=None, expected_return=None,
          rank_score=None):
    return SimpleNamespace(
        ticker=ticker, raw_score=raw_score, mu=mu,
        expected_return=expected_return, rank_score=rank_score,
    )


class TestTable:

    def test_table_and_index_created(self, tmp_path):
        conn = _conn(tmp_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decision_ledger)")}
        assert {
            "ledger_id", "run_id", "run_date", "ticker", "role", "raw_score",
            "mu", "expected_return", "rank_score", "selected", "blocked_by",
            "regime", "fwd_1d", "fwd_5d", "fwd_20d", "fwd_60d",
            "is_winner_60d", "created_at",
        } <= cols
        idx = {r[1] for r in conn.execute("PRAGMA index_list(decision_ledger)")}
        # The explicit (run_date, ticker) index + the UNIQUE(run_id, ticker).
        assert any("decision_ledger_date" in name for name in idx)


class TestWriter:

    def test_writes_one_row_per_candidate_and_holding(self, tmp_path):
        conn = _conn(tmp_path)
        cands = [
            _cand("MU", raw_score=-0.10, mu=0.02, expected_return=0.03,
                  rank_score=0.9),
            _cand("NFLX", raw_score=-0.30, mu=-0.01, expected_return=-0.02,
                  rank_score=0.1),
        ]
        holdings = {"AMZN": _cand("AMZN", mu=0.01, expected_return=0.015,
                                  rank_score=0.5)}
        n = record_decision_ledger(
            conn, "r1", RUN_DATE, cands, holdings,
            selected_tickers={"MU"}, blocked_map={"NFLX": "below_threshold"},
            regime="BULL_CALM",
        )
        assert n == 3
        rows = conn.execute(
            "SELECT ticker, role, raw_score, mu, expected_return, rank_score, "
            "selected, blocked_by, regime FROM decision_ledger "
            "ORDER BY ticker").fetchall()
        by_ticker = {r[0]: r for r in rows}
        # selected candidate
        assert by_ticker["MU"][1] == "candidate"
        assert by_ticker["MU"][6] == 1
        assert by_ticker["MU"][7] is None
        assert by_ticker["MU"][8] == "BULL_CALM"
        # blocked candidate carries the blocked_map reason
        assert by_ticker["NFLX"][6] == 0
        assert by_ticker["NFLX"][7] == "below_threshold"
        # holding row, raw_score NULL like candidate_scores' holding rows
        assert by_ticker["AMZN"][1] == "holding"
        assert by_ticker["AMZN"][2] is None
        # fwd_* + is_winner_60d are NULL at write time
        nulls = conn.execute(
            "SELECT COUNT(*) FROM decision_ledger "
            "WHERE fwd_60d IS NULL AND is_winner_60d IS NULL").fetchone()[0]
        assert nulls == 3

    def test_default_blocked_reason_for_unselected_candidate(self, tmp_path):
        conn = _conn(tmp_path)
        record_decision_ledger(
            conn, "r1", RUN_DATE, [_cand("XOM", raw_score=-0.2)], {},
            selected_tickers=set(),
        )
        reason = conn.execute(
            "SELECT blocked_by FROM decision_ledger WHERE ticker='XOM'"
        ).fetchone()[0]
        assert reason == "candidate_not_selected"

    def test_exclude_holdings_flag(self, tmp_path):
        conn = _conn(tmp_path)
        n = record_decision_ledger(
            conn, "r1", RUN_DATE, [_cand("MU", raw_score=-0.1)],
            {"AMZN": _cand("AMZN", mu=0.01)},
            selected_tickers={"MU"}, include_holdings=False,
        )
        assert n == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM decision_ledger WHERE role='holding'"
        ).fetchone()[0] == 0


class TestIdempotency:

    def test_insert_or_ignore_on_dup_run_id_ticker(self, tmp_path):
        conn = _conn(tmp_path)
        cands = [_cand("MU", raw_score=-0.10, mu=0.02)]
        record_decision_ledger(conn, "r1", RUN_DATE, cands, {},
                               selected_tickers={"MU"})
        # Re-run the SAME bar with a DIFFERENT mu — UNIQUE(run_id, ticker)
        # means the first write wins; the row count stays 1 and the value
        # is NOT overwritten.
        cands2 = [_cand("MU", raw_score=-0.99, mu=0.99)]
        record_decision_ledger(conn, "r1", RUN_DATE, cands2, {},
                               selected_tickers={"MU"})
        rows = conn.execute(
            "SELECT raw_score, mu FROM decision_ledger WHERE ticker='MU'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0] == (-0.10, 0.02)

    def test_distinct_runs_accumulate(self, tmp_path):
        conn = _conn(tmp_path)
        record_decision_ledger(conn, "r1", RUN_DATE, [_cand("MU", mu=0.1)], {},
                               selected_tickers=set())
        record_decision_ledger(conn, "r2", RUN_DATE, [_cand("MU", mu=0.1)], {},
                               selected_tickers=set())
        assert conn.execute(
            "SELECT COUNT(*) FROM decision_ledger").fetchone()[0] == 2


class TestNoneSafety:

    def test_none_conn(self):
        assert record_decision_ledger(
            None, "r1", RUN_DATE, [_cand("MU")], {}, selected_tickers=set()) == 0

    def test_none_run_id(self, tmp_path):
        assert record_decision_ledger(
            _conn(tmp_path), None, RUN_DATE, [_cand("MU")], {},
            selected_tickers=set()) == 0

    def test_empty_candidates_returns_zero(self, tmp_path):
        assert record_decision_ledger(
            _conn(tmp_path), "r1", RUN_DATE, [], {}, selected_tickers=set()) == 0

    def test_nan_inf_persist_as_null(self, tmp_path):
        conn = _conn(tmp_path)
        record_decision_ledger(
            conn, "r1", RUN_DATE,
            [_cand("MU", raw_score=float("nan"), mu=float("inf"),
                   expected_return=None)],
            {}, selected_tickers={"MU"})
        row = conn.execute(
            "SELECT raw_score, mu, expected_return FROM decision_ledger "
            "WHERE ticker='MU'").fetchone()
        assert row == (None, None, None)

    def test_none_run_date_ok(self, tmp_path):
        conn = _conn(tmp_path)
        record_decision_ledger(conn, "r1", None, [_cand("MU", mu=0.1)], {},
                               selected_tickers=set())
        assert conn.execute(
            "SELECT run_date FROM decision_ledger WHERE ticker='MU'"
        ).fetchone()[0] is None

    def test_candidate_without_ticker_skipped(self, tmp_path):
        conn = _conn(tmp_path)
        n = record_decision_ledger(
            conn, "r1", RUN_DATE, [SimpleNamespace(ticker=None, mu=0.1)], {},
            selected_tickers=set())
        assert n == 0


def _load_backfill_module():
    path = _REPO / "scripts" / "backfill_decision_ledger_returns.py"
    spec = importlib.util.spec_from_file_location("backfill_decision_ledger", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestBackfill:

    def test_backfill_populates_fwd_and_winner(self, tmp_path):
        db = tmp_path / "runs.db"
        conn = get_connection({"persistence": {"enabled": True,
                                               "db_path": str(db)}})
        # Two decisions on the same run_date: a winner (MU) and a loser (NFLX).
        record_decision_ledger(
            conn, "r1", RUN_DATE,
            [_cand("MU", raw_score=-0.10, mu=0.05),
             _cand("NFLX", raw_score=-0.30, mu=-0.02)],
            {}, selected_tickers={"MU"}, regime="BULL_CALM")
        # Realized forward returns land later (nightly backfill).
        record_forward_returns(conn, [
            {"as_of_date": RUN_DATE, "ticker": "MU", "fwd_1d": 0.01,
             "fwd_5d": 0.02, "fwd_20d": 0.04, "fwd_60d": 0.08},
            {"as_of_date": RUN_DATE, "ticker": "NFLX", "fwd_1d": -0.01,
             "fwd_5d": -0.02, "fwd_20d": -0.03, "fwd_60d": -0.05},
        ])
        conn.commit()
        conn.close()

        mod = _load_backfill_module()
        conn2 = sqlite3.connect(db)
        try:
            n = mod._backfill(conn2, since=None, dry_run=False)
        finally:
            conn2.close()
        assert n == 2

        conn3 = sqlite3.connect(db)
        rows = {r[0]: r for r in conn3.execute(
            "SELECT ticker, fwd_1d, fwd_5d, fwd_20d, fwd_60d, is_winner_60d "
            "FROM decision_ledger ORDER BY ticker")}
        conn3.close()
        assert rows["MU"][4] == 0.08
        assert rows["MU"][5] == 1        # winner
        assert rows["NFLX"][4] == -0.05
        assert rows["NFLX"][5] == 0      # loser

    def test_backfill_skips_unrealized_and_is_idempotent(self, tmp_path):
        db = tmp_path / "runs.db"
        conn = get_connection({"persistence": {"enabled": True,
                                               "db_path": str(db)}})
        record_decision_ledger(
            conn, "r1", RUN_DATE, [_cand("MU", mu=0.05)], {},
            selected_tickers={"MU"})
        # fwd_60d still NULL → row stays unrealized, not winner-tagged.
        record_forward_returns(conn, [
            {"as_of_date": RUN_DATE, "ticker": "MU", "fwd_1d": 0.01,
             "fwd_60d": None},
        ])
        conn.commit()
        conn.close()

        mod = _load_backfill_module()
        conn2 = sqlite3.connect(db)
        try:
            n_first = mod._backfill(conn2, since=None, dry_run=False)
            # No fwd_60d available → nothing updated.
            assert n_first == 0
            row = conn2.execute(
                "SELECT fwd_60d, is_winner_60d FROM decision_ledger "
                "WHERE ticker='MU'").fetchone()
            assert row == (None, None)
            # Second run is still a no-op (idempotent).
            assert mod._backfill(conn2, since=None, dry_run=False) == 0
        finally:
            conn2.close()
