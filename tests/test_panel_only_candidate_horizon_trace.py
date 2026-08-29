"""orch#1082 — a panel-only candidate dropped BEFORE calibration must not
persist an expected_return without its horizon.

Mechanism (shadow config: ``ranking.panel_scoring.candidate_universe =
"watchlist"`` + ``bypass_ticker_gate``): a watchlist ticker with no
tournament artifact becomes a "panel_pending" candidate in ``ScoreBuyTask``.
Its expected return is only stamped — together with its horizon — by
``ApplyGlobalCalibrationTask``. A candidate dropped earlier
(``RealizedVolGateTask`` → ``risk_gate_vol``; ``_drop_unscored_panel_candidates``
→ ``panel_score_missing``; ``_fail_closed_panel_scoring`` →
``panel_scorer_load_failed``) used to keep a ``0.0`` placeholder with a
``None`` horizon. That pair reached ``candidate_scores`` (via the full
candidate snapshot) and ``ticker_daily_state`` (via ``_ticker_score_snapshot``),
and ``decision_trace_integrity_report`` counted each as a horizon gap →
``RunnerAdapter.commit`` raised (5 gaps on 2026-08-29, 20 + 15 on 2026-08-25).

These tests drive the real tasks and the real persistence layer end to end
and assert on the validator's counters — the same comparison the live commit
makes.
"""
from __future__ import annotations

import datetime
import sqlite3
from types import SimpleNamespace

import numpy as np
import pandas as pd

from renquant_pipeline.context import InferenceContext
from renquant_pipeline.kernel.decision_trace import build_ticker_daily_state_rows
from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
    _fail_closed_panel_scoring,
)
from renquant_pipeline.kernel.persistence import (
    decision_trace_integrity_report,
    ensure_schema,
    record_candidate_scores,
    record_ticker_daily_state,
)
from renquant_pipeline.kernel.pipeline.task_candidates import (
    AssembleCandidateTask,
    ScoreBuyTask,
)
from renquant_pipeline.kernel.pipeline.task_risk_gates import RealizedVolGateTask
from renquant_pipeline.kernel.selection import CandidateResult

RUN_ID = "2026-08-29-test-horizon"
TODAY = datetime.date(2026, 8, 29)


def _config() -> dict:
    return {
        "watchlist": ["AAA", "BBB", "SPY"],
        "benchmark": "SPY",
        "sector_map": {"AAA": "TECH", "BBB": "TECH"},
        "ranking": {
            "panel_scoring": {
                "enabled": True,
                "bypass_ticker_gate": True,
                "candidate_universe": "watchlist",
            },
        },
        "risk_gates": {"realized_vol": {"enabled": True, "max_annualized": 0.60,
                                        "window_days": 20}},
    }


def _wild_ohlcv(n: int = 60) -> pd.DataFrame:
    # Alternating ±30% closes → realized vol far above the 60% cap.
    close = 100.0 * np.cumprod([1.3 if i % 2 else 0.7 for i in range(n)])
    idx = pd.bdate_range(end=pd.Timestamp(TODAY), periods=n)
    return pd.DataFrame({"close": close}, index=idx)


def _panel_only_tc(ticker: str, config: dict) -> SimpleNamespace:
    tc = SimpleNamespace(ticker=ticker, model=None, config=config, rs_score=0.0,
                         features=None)
    assert ScoreBuyTask().run(tc) is None
    assert tc.model_action == "panel_pending"
    AssembleCandidateTask().run(tc)
    return tc


def _snapshot(tc: SimpleNamespace) -> dict:
    # Mirrors the per-ticker score snapshot InferencePipeline.run builds from
    # the TickerInferenceContext (pp_inference.py, Phase 2b buy scan).
    return {
        "raw_score": getattr(tc, "_raw_score", None),
        "rank_score": getattr(tc, "_rank_score", None),
        "expected_return": getattr(tc, "_expected_return", None),
        "expected_return_horizon_days": getattr(
            tc, "_expected_return_horizon_days", None,
        ),
        "model_action": getattr(tc, "model_action", None),
    }


def _ctx(config: dict) -> InferenceContext:
    ctx = InferenceContext(config=config, today=TODAY)
    ctx.holdings = {}
    ctx.models = {"AAA": {"_metadata": {}}}
    ctx.pending_broker_tickers = set()
    ctx.candidates = []
    ctx.counters = {}
    ctx._ticker_score_snapshot = {}  # noqa: SLF001
    ctx._blocked_by_ticker = {}  # noqa: SLF001
    return ctx


def _persist_and_report(ctx: InferenceContext) -> tuple[dict, sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    blocked = dict(getattr(ctx, "_blocked_by_ticker", {}) or {})
    pool = list(getattr(ctx, "_full_candidate_snapshot", None) or ctx.candidates)
    record_candidate_scores(
        conn, RUN_ID, pool, ctx.holdings, set(), blocked,
        sector_map=ctx.config["sector_map"], model_types={},
    )
    rows = build_ticker_daily_state_rows(
        config=ctx.config, ctx=ctx, selected_tickers=set(), blocked_map=blocked,
        model_types={}, model_keys=set(ctx.models),
        sector_map=ctx.config["sector_map"],
    )
    record_ticker_daily_state(conn, run_date=TODAY, rows=rows, run_id=RUN_ID)
    report = decision_trace_integrity_report(
        conn, RUN_ID, expected_watchlist=ctx.config["watchlist"],
    )
    return report, conn


def _tds(conn: sqlite3.Connection, ticker: str) -> tuple:
    return conn.execute(
        "SELECT expected_return, expected_return_horizon_days, blocked_by, "
        "in_universe FROM ticker_daily_state WHERE run_id=? AND ticker=?",
        (RUN_ID, ticker),
    ).fetchone()


def test_panel_only_candidate_dropped_by_realized_vol_gate_has_no_horizon_gap():
    config = _config()
    ctx = _ctx(config)
    tc = _panel_only_tc("BBB", config)
    ctx._ticker_score_snapshot["BBB"] = _snapshot(tc)  # noqa: SLF001
    ctx.candidates = [tc.candidate]
    ctx.ohlcv = {"BBB": _wild_ohlcv()}

    assert RealizedVolGateTask().run(ctx) is True
    assert ctx.candidates == []
    assert ctx._blocked_by_ticker == {"BBB": "risk_gate_vol"}  # noqa: SLF001

    report, conn = _persist_and_report(ctx)
    assert report["decision_horizon_gaps"] == 0, report
    assert report["candidate_horizon_gaps"] == 0, report
    # The row still explains the drop; it just no longer claims a forecast.
    assert _tds(conn, "BBB") == (None, None, "risk_gate_vol", 0)


def test_panel_only_candidate_under_fail_closed_scoring_has_no_horizon_gap():
    config = _config()
    ctx = _ctx(config)
    tc = _panel_only_tc("BBB", config)
    ctx._ticker_score_snapshot["BBB"] = _snapshot(tc)  # noqa: SLF001
    ctx.candidates = [tc.candidate]

    # 2026-08-25 shape: the primary scorer failed to load; every candidate
    # is moved into the full snapshot pool and blocked.
    _fail_closed_panel_scoring(ctx, "panel_scorer_load_failed")
    assert ctx.candidates == [] and ctx.skip_buys
    assert [c.ticker for c in ctx._full_candidate_snapshot] == ["BBB"]  # noqa: SLF001

    report, conn = _persist_and_report(ctx)
    assert report["candidate_horizon_gaps"] == 0, report
    assert report["decision_horizon_gaps"] == 0, report
    cs = conn.execute(
        "SELECT expected_return, expected_return_horizon_days, blocked_by "
        "FROM candidate_scores WHERE run_id=? AND ticker='BBB'", (RUN_ID,),
    ).fetchone()
    assert cs == (None, None, "panel_scorer_load_failed")


def test_tournament_scored_candidate_still_persists_forecast_with_horizon():
    """The fix must not blank REAL forecasts: a per-ticker tournament candidate
    keeps its expected_return AND horizon through the same drop + persist."""
    config = _config()
    ctx = _ctx(config)
    cand = CandidateResult("AAA", 0.4, 0.6, 0.0, expected_return=0.031,
                           expected_return_horizon_days=20)
    ctx._ticker_score_snapshot["AAA"] = {  # noqa: SLF001
        "raw_score": 0.4, "rank_score": 0.6, "expected_return": 0.031,
        "expected_return_horizon_days": 20, "model_action": "buy",
    }
    ctx.candidates = [cand]
    ctx.ohlcv = {"AAA": _wild_ohlcv()}
    assert RealizedVolGateTask().run(ctx) is True
    assert ctx.candidates == []

    report, conn = _persist_and_report(ctx)
    assert report["decision_horizon_gaps"] == 0, report
    assert _tds(conn, "AAA") == (0.031, 20, "risk_gate_vol", 1)


def test_validator_still_counts_a_forecast_without_horizon():
    """Guard the guard: the pre-fix shape (0.0 placeholder, None horizon) IS a
    gap on both tables. If this stops failing, the validator went blind, not
    the pipeline clean."""
    config = _config()
    ctx = _ctx(config)
    placeholder = CandidateResult("BBB", 0.0, 0.0, 0.0, expected_return=0.0,
                                  expected_return_horizon_days=None)
    ctx._ticker_score_snapshot["BBB"] = {  # noqa: SLF001
        "raw_score": 0.0, "rank_score": 0.0, "expected_return": 0.0,
        "expected_return_horizon_days": None, "model_action": "panel_pending",
    }
    ctx._full_candidate_snapshot = [placeholder]  # noqa: SLF001
    ctx._blocked_by_ticker = {"BBB": "risk_gate_vol"}  # noqa: SLF001

    report, _ = _persist_and_report(ctx)
    assert report["decision_horizon_gaps"] == 1
    assert report["candidate_horizon_gaps"] == 1
    assert report["ok"] is False
