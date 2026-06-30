"""decision_outcomes VIEW + gate-validation query tests (rescope of PR #152).

Per Codex review: the decision/outcome join lives in a committed SQL VIEW
(`decision_outcomes`) over the three existing tables, and the experiment/label
logic lives OUTSIDE the schema in scripts/gate_validation_query.py. These tests
pin:

  * the view is created and exposes the decision factors + run context;
  * the (candidate_scores x pipeline_runs x ticker_forward_returns) join is
    correct and the per-horizon own forward returns surface independently
    (no waiting on fwd_60d);
  * the benchmark-relative column = own fwd_Nd - SPY fwd_Nd; and
  * the sim/live filter and the estimand of the validation query on a small
    synthetic fixture.
"""
from __future__ import annotations

import datetime
import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from renquant_pipeline.kernel.persistence import (
    get_connection,
    record_candidate_scores,
    record_forward_returns,
    record_pipeline_run,
)

RUN_DATE = datetime.date(2026, 4, 22)
_REPO = Path(__file__).resolve().parent.parent


def _conn(tmp_path):
    return get_connection({"persistence": {"enabled": True,
                                           "db_path": str(tmp_path / "runs.db")}})


def _cand(ticker, *, raw_score=None, mu=None, expected_return=None,
          rank_score=None, sigma=None, panel_score=None, rs_score=None):
    return SimpleNamespace(
        ticker=ticker, raw_score=raw_score, mu=mu,
        expected_return=expected_return, rank_score=rank_score,
        sigma=sigma, panel_score=panel_score, rs_score=rs_score,
    )


def _seed_run(conn, *, run_type, run_date, regime, cands, selected,
              blocked_map=None, holdings=None):
    run_id = record_pipeline_run(
        conn, run_type=run_type, run_date=run_date, strategy="renquant_104",
        regime=regime, run_id=f"{run_date.isoformat()}-{run_type}",
    )
    record_candidate_scores(
        conn, run_id, cands, holdings or {}, selected_tickers=selected,
        blocked_map=blocked_map or {},
    )
    return run_id


def _load_query_module():
    path = _REPO / "scripts" / "gate_validation_query.py"
    spec = importlib.util.spec_from_file_location("gate_validation_query", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestView:

    def test_view_created_and_columns(self, tmp_path):
        conn = _conn(tmp_path)
        # The object exists and is a VIEW (not a table).
        kind = conn.execute(
            "SELECT type FROM sqlite_master WHERE name='decision_outcomes'"
        ).fetchone()
        assert kind is not None and kind[0] == "view"
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decision_outcomes)")}
        assert {
            "run_id", "run_date", "run_type", "regime", "ticker", "role",
            "raw_score", "mu", "expected_return", "rank_score", "selected",
            "blocked_by", "fwd_1d", "fwd_5d", "fwd_20d", "fwd_60d",
            "rel_fwd_1d", "rel_fwd_5d", "rel_fwd_20d", "rel_fwd_60d",
        } <= cols
        # No decision_ledger TABLE remains.
        assert conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='decision_ledger'").fetchone() is None

    def test_join_surfaces_decision_factors_and_context(self, tmp_path):
        conn = _conn(tmp_path)
        _seed_run(
            conn, run_type="live", run_date=RUN_DATE, regime="BULL_CALM",
            cands=[_cand("MU", raw_score=-0.10, mu=0.02, expected_return=0.03,
                         rank_score=0.9),
                   _cand("NFLX", raw_score=-0.30, mu=-0.01,
                         expected_return=-0.02, rank_score=0.1)],
            selected={"MU"}, blocked_map={"NFLX": "below_threshold"},
        )
        row = conn.execute(
            "SELECT run_date, run_type, regime, raw_score, mu, expected_return, "
            "rank_score, selected, blocked_by FROM decision_outcomes "
            "WHERE ticker='MU' AND role='candidate'").fetchone()
        assert row == (RUN_DATE.isoformat(), "live", "BULL_CALM",
                       -0.10, 0.02, 0.03, 0.9, 1, None)
        blocked = conn.execute(
            "SELECT selected, blocked_by FROM decision_outcomes "
            "WHERE ticker='NFLX' AND role='candidate'").fetchone()
        assert blocked == (0, "below_threshold")

    def test_forward_returns_left_join_and_null_until_realized(self, tmp_path):
        conn = _conn(tmp_path)
        _seed_run(conn, run_type="live", run_date=RUN_DATE, regime="BULL_CALM",
                  cands=[_cand("MU", mu=0.05)], selected={"MU"})
        # Before any forward return: own fwd_* are NULL (LEFT JOIN miss).
        pre = conn.execute(
            "SELECT fwd_1d, fwd_60d, rel_fwd_1d FROM decision_outcomes "
            "WHERE ticker='MU'").fetchone()
        assert pre == (None, None, None)
        # Only the 1d/5d/20d horizons realize (fwd_60d still NULL) — they must
        # surface independently, no waiting on fwd_60d.
        record_forward_returns(conn, [
            {"as_of_date": RUN_DATE, "ticker": "MU", "fwd_1d": 0.01,
             "fwd_5d": 0.02, "fwd_20d": 0.04, "fwd_60d": None},
        ])
        post = conn.execute(
            "SELECT fwd_1d, fwd_5d, fwd_20d, fwd_60d FROM decision_outcomes "
            "WHERE ticker='MU'").fetchone()
        assert post == (0.01, 0.02, 0.04, None)

    def test_benchmark_relative_is_own_minus_spy(self, tmp_path):
        conn = _conn(tmp_path)
        _seed_run(conn, run_type="live", run_date=RUN_DATE, regime="BULL_CALM",
                  cands=[_cand("MU", mu=0.05)], selected={"MU"})
        record_forward_returns(conn, [
            {"as_of_date": RUN_DATE, "ticker": "MU", "fwd_1d": 0.03,
             "fwd_5d": 0.05, "fwd_20d": 0.08, "fwd_60d": 0.10},
            {"as_of_date": RUN_DATE, "ticker": "SPY", "fwd_1d": 0.01,
             "fwd_5d": 0.02, "fwd_20d": 0.03, "fwd_60d": 0.04},
        ])
        rel = conn.execute(
            "SELECT rel_fwd_1d, rel_fwd_5d, rel_fwd_20d, rel_fwd_60d "
            "FROM decision_outcomes WHERE ticker='MU'").fetchone()
        # own - SPY, element-wise, with float tolerance.
        for got, exp in zip(rel, (0.02, 0.03, 0.05, 0.06)):
            assert abs(got - exp) < 1e-9

    def test_rel_is_null_when_spy_missing(self, tmp_path):
        conn = _conn(tmp_path)
        _seed_run(conn, run_type="live", run_date=RUN_DATE, regime="BULL_CALM",
                  cands=[_cand("MU", mu=0.05)], selected={"MU"})
        record_forward_returns(conn, [
            {"as_of_date": RUN_DATE, "ticker": "MU", "fwd_60d": 0.10},
        ])  # no SPY row this date
        rel = conn.execute(
            "SELECT fwd_60d, rel_fwd_60d FROM decision_outcomes "
            "WHERE ticker='MU'").fetchone()
        assert rel[0] == 0.10
        assert rel[1] is None

    def test_sim_and_live_both_visible_but_distinguishable(self, tmp_path):
        conn = _conn(tmp_path)
        _seed_run(conn, run_type="live", run_date=RUN_DATE, regime="BULL_CALM",
                  cands=[_cand("MU", mu=0.05)], selected={"MU"})
        _seed_run(conn, run_type="sim", run_date=RUN_DATE, regime="BULL_CALM",
                  cands=[_cand("MU", mu=0.05)], selected={"MU"})
        counts = dict(conn.execute(
            "SELECT run_type, COUNT(*) FROM decision_outcomes "
            "WHERE ticker='MU' GROUP BY run_type").fetchall())
        assert counts == {"live": 1, "sim": 1}


class TestValidationQuery:

    def _seed_two_arms(self, conn, *, run_type="live"):
        """Seed several non-overlapping run_dates, each with one above-mu name
        that out-performs SPY and one below-mu name that under-performs."""
        # 1d horizon, spaced > 1 day apart so they are non-overlapping cohorts.
        for i, d in enumerate([
            datetime.date(2026, 4, 22), datetime.date(2026, 4, 24),
            datetime.date(2026, 4, 28), datetime.date(2026, 4, 30),
            datetime.date(2026, 5, 4),  datetime.date(2026, 5, 6),
            datetime.date(2026, 5, 8),  datetime.date(2026, 5, 12),
            datetime.date(2026, 5, 14), datetime.date(2026, 5, 18),
        ]):
            _seed_run(conn, run_type=run_type, run_date=d, regime="BULL_CALM",
                      cands=[_cand("WIN", mu=0.05), _cand("LOSE", mu=-0.05)],
                      selected={"WIN"})
            record_forward_returns(conn, [
                # above-mu name beats SPY by +200 bps; below-mu lags by -200 bps
                {"as_of_date": d, "ticker": "WIN", "fwd_1d": 0.03},
                {"as_of_date": d, "ticker": "LOSE", "fwd_1d": -0.01},
                {"as_of_date": d, "ticker": "SPY", "fwd_1d": 0.01},
            ])

    def test_estimand_separates_above_below(self, tmp_path):
        conn = _conn(tmp_path)
        self._seed_two_arms(conn)
        mod = _load_query_module()
        res = mod.estimate_gate_separation(
            conn, horizon=1, mu_thresh=0.0, cost_bps=0.0,
            min_cohorts=5, decision_bps=25.0,
        )
        # above-mu rel = +0.02 (0.03 - 0.01); below-mu rel = -0.02 (-0.01 - 0.01)
        assert abs(res["arms"]["above"]["mean_rel_net"] - 0.02) < 1e-9
        assert abs(res["arms"]["below"]["mean_rel_net"] - (-0.02)) < 1e-9
        # Delta = +0.04 = +400 bps, well above the 25 bps decision threshold.
        assert abs(res["delta_rel_net"] - 0.04) < 1e-9
        assert res["verdict"] == "PASS"

    def test_cost_reduces_per_arm_mean_symmetrically(self, tmp_path):
        conn = _conn(tmp_path)
        self._seed_two_arms(conn)
        mod = _load_query_module()
        res = mod.estimate_gate_separation(
            conn, horizon=1, mu_thresh=0.0, cost_bps=50.0,  # 50 bps
            min_cohorts=5, decision_bps=25.0,
        )
        # Each arm's net mean drops by exactly the 50 bps cost.
        assert abs(res["arms"]["above"]["mean_rel_net"] - (0.02 - 0.005)) < 1e-9
        assert abs(res["arms"]["below"]["mean_rel_net"] - (-0.02 - 0.005)) < 1e-9
        # Cost cancels in the difference (symmetric per arm).
        assert abs(res["delta_rel_net"] - 0.04) < 1e-9

    def test_underpowered_when_too_few_cohorts(self, tmp_path):
        conn = _conn(tmp_path)
        self._seed_two_arms(conn)
        mod = _load_query_module()
        res = mod.estimate_gate_separation(
            conn, horizon=1, mu_thresh=0.0, cost_bps=0.0,
            min_cohorts=50, decision_bps=25.0,  # demand more cohorts than exist
        )
        assert res["verdict"] == "UNDERPOWERED"
        assert res["effective_cohorts"] < 50

    def test_live_only_excludes_sim(self, tmp_path):
        conn = _conn(tmp_path)
        # Seed ONLY sim data; the default live-only filter must see nothing.
        self._seed_two_arms(conn, run_type="sim")
        mod = _load_query_module()
        live = mod.estimate_gate_separation(
            conn, horizon=1, mu_thresh=0.0, cost_bps=0.0,
            min_cohorts=1, decision_bps=25.0, include_sim=False,
        )
        assert live["effective_cohorts"] == 0
        assert live["verdict"] == "UNDERPOWERED"
        # include_sim=True (debug) sees the sim cohorts.
        with_sim = mod.estimate_gate_separation(
            conn, horizon=1, mu_thresh=0.0, cost_bps=0.0,
            min_cohorts=5, decision_bps=25.0, include_sim=True,
        )
        assert with_sim["effective_cohorts"] >= 5
        assert with_sim["verdict"] == "PASS"

    def test_non_overlapping_cohort_selection_for_long_horizon(self, tmp_path):
        conn = _conn(tmp_path)
        # Two run_dates only 1 calendar day apart; at horizon 60 they overlap,
        # so only ONE non-overlapping cohort survives per arm.
        for d in [datetime.date(2026, 4, 22), datetime.date(2026, 4, 23)]:
            _seed_run(conn, run_type="live", run_date=d, regime="BULL_CALM",
                      cands=[_cand("WIN", mu=0.05), _cand("LOSE", mu=-0.05)],
                      selected={"WIN"})
            record_forward_returns(conn, [
                {"as_of_date": d, "ticker": "WIN", "fwd_60d": 0.10},
                {"as_of_date": d, "ticker": "LOSE", "fwd_60d": -0.05},
                {"as_of_date": d, "ticker": "SPY", "fwd_60d": 0.04},
            ])
        mod = _load_query_module()
        res = mod.estimate_gate_separation(
            conn, horizon=60, mu_thresh=0.0, cost_bps=0.0,
            min_cohorts=1, decision_bps=25.0,
        )
        assert res["arms"]["above"]["n_cohorts"] == 1
        assert res["arms"]["below"]["n_cohorts"] == 1
