"""Active-scorer identity attribution contract (2026-06-07 audit follow-up).

Runtime telemetry, order attribution, candidate/trade DB rows, score
distribution, and decision traces must stamp the ACTIVE panel scorer identity
(`ranking.panel_scoring.kind`, e.g. `hf_patchtst`) as `model_type` instead of
silently inheriting the stale per-ticker XGB-era label
(Manual/XGBoost/QLearning/Classification). The legacy per-ticker label is
preserved separately as `legacy_model_type`; `active_scorer` records the
selecting scorer explicitly (NULL when panel scoring is off).
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from types import SimpleNamespace

import pytest

from renquant_pipeline.context import InferenceContext
from renquant_pipeline.kernel.decision_trace import (
    active_scorer_identity,
    build_ticker_daily_state_rows,
    resolve_model_attribution,
)
from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
    _annotate_panel_model,
)
from renquant_pipeline.kernel.persistence import (
    ensure_schema,
    record_candidate_scores,
    record_ticker_daily_state,
    record_trades,
)
from renquant_pipeline.kernel.pipeline.order_attribution import (
    stamp_order_attribution,
)
from renquant_pipeline.kernel.pipeline.task_score_distribution import (
    RecordScoreDistributionTask,
)
from renquant_pipeline.kernel.selection import CandidateResult
from renquant_pipeline.kernel.trade_events import (
    build_buy_trade_event,
    build_sell_trade_event,
)


def _config(kind: str | None) -> dict:
    cfg: dict = {
        "watchlist": ["AAPL", "MSFT"],
        "sector_map": {"AAPL": "TECH", "MSFT": "TECH"},
    }
    if kind is not None:
        cfg["ranking"] = {"panel_scoring": {"enabled": True, "kind": kind}}
    return cfg


def _ctx(kind: str | None) -> InferenceContext:
    ctx = InferenceContext(
        config=_config(kind),
        today=dt.date(2026, 6, 9),
        models={
            "AAPL": {"_metadata": {"best_approach": "XGBoost"}},
            "MSFT": {"_metadata": {"best_approach": "QLearning"}},
        },
        candidates=[
            CandidateResult("AAPL", 0.1, 0.1, 0.0),
            CandidateResult("MSFT", 0.1, 0.1, 0.0),
        ],
        holdings={},
    )
    if kind is not None:
        ctx._active_panel_model_type = kind  # what LoadScorerTask stamps
    return ctx


# ── identity helpers ───────────────────────────────────────────────────────────


def test_active_scorer_identity_reads_panel_scoring_kind() -> None:
    assert active_scorer_identity(_config("hf_patchtst")) == "hf_patchtst"
    assert active_scorer_identity(_config("xgb")) == "xgb"
    # No panel scoring configured → no active scorer (per-ticker labels win).
    assert active_scorer_identity(_config(None)) is None
    disabled = {"ranking": {"panel_scoring": {"enabled": False, "kind": "xgb"}}}
    assert active_scorer_identity(disabled) is None


def test_resolve_model_attribution_prefers_active_scorer() -> None:
    ident = resolve_model_attribution(
        _config("hf_patchtst"), None, legacy_model_type="XGBoost",
    )
    assert ident == {
        "model_type": "hf_patchtst",
        "active_scorer": "hf_patchtst",
        "legacy_model_type": "XGBoost",
    }
    ident = resolve_model_attribution(_config(None), None, legacy_model_type="Manual")
    assert ident["model_type"] == "Manual"
    assert ident["active_scorer"] is None


def test_annotate_panel_model_preserves_legacy_label() -> None:
    ctx = SimpleNamespace(_active_panel_model_type="hf_patchtst")
    cand = SimpleNamespace(ticker="AAPL", model_type="XGBoost")
    _annotate_panel_model(cand, ctx)
    assert cand.model_type == "hf_patchtst"
    assert cand.legacy_model_type == "XGBoost"


# ── order attribution ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["hf_patchtst", "xgb"])
def test_order_attribution_stamps_active_scorer_over_stale_label(kind: str) -> None:
    ctx = _ctx(kind)
    order = stamp_order_attribution(
        {
            "ticker": "AAPL",
            "order_type": "market",
            "model_type": "XGBoost",  # stale per-ticker label pre-set by emitter
        },
        ctx=ctx,
        source_job="JointPortfolioQPJob",
        source_task="EmitBuy",
        acceptance_reason="unit_test",
    )
    assert order["model_type"] == kind
    assert order["score_snapshot"]["model_type"] == kind
    assert order["score_snapshot"]["active_scorer"] == kind
    # Legacy per-ticker label preserved, not silently dropped.
    assert order["score_snapshot"]["legacy_model_type"] == "XGBoost"
    assert order["legacy_model_type"] == "XGBoost"


def test_order_attribution_keeps_per_ticker_label_without_panel_scoring() -> None:
    ctx = _ctx(None)
    order = stamp_order_attribution(
        {"ticker": "AAPL", "order_type": "market"},
        ctx=ctx,
        source_job="JointPortfolioQPJob",
        source_task="EmitBuy",
        acceptance_reason="unit_test",
    )
    # No active scorer → per-ticker artifact label is still the model_type.
    assert order["score_snapshot"]["model_type"] == "XGBoost"
    assert order["score_snapshot"]["active_scorer"] is None


# ── trade events ──────────────────────────────────────────────────────────────


def test_buy_trade_event_carries_active_scorer_fields() -> None:
    ctx = _ctx("hf_patchtst")
    order = stamp_order_attribution(
        {"ticker": "AAPL", "order_type": "market", "model_type": "XGBoost"},
        ctx=ctx,
        source_job="JointPortfolioQPJob",
        source_task="EmitBuy",
        acceptance_reason="unit_test",
    )
    event = build_buy_trade_event(order, date=dt.date(2026, 6, 9))
    assert event["model_type"] == "hf_patchtst"
    assert event["active_scorer"] == "hf_patchtst"
    assert event["legacy_model_type"] == "XGBoost"
    assert event["score_snapshot"]["model_type"] == "hf_patchtst"


@pytest.mark.parametrize("kind", ["hf_patchtst", "xgb"])
def test_sell_trade_event_stamps_active_scorer(kind: str) -> None:
    holding = SimpleNamespace(
        ticker="AAPL",
        entry_price=100.0,
        entry_date=dt.date(2026, 6, 1),
        shares=10.0,
        model_type="XGBoost",
        sector="TECH",
    )
    sig = SimpleNamespace(exit_type="stop_loss", reason="unit", quantity=10.0)
    event = build_sell_trade_event(
        ticker="AAPL",
        sig=sig,
        holding=holding,
        price=110.0,
        today=dt.date(2026, 6, 9),
        regime="BULL_CALM",
        confidence=0.9,
        regime_params={},
        config=_config(kind),
    )
    assert event["model_type"] == kind
    assert event["active_scorer"] == kind
    assert event["legacy_model_type"] == "XGBoost"
    assert event["score_snapshot"]["model_type"] == kind


def test_sell_trade_event_without_panel_scoring_keeps_holding_label() -> None:
    holding = SimpleNamespace(
        ticker="AAPL", entry_price=100.0, entry_date=dt.date(2026, 6, 1),
        shares=10.0, model_type="Manual",
    )
    sig = SimpleNamespace(exit_type="take_profit", reason="unit", quantity=10.0)
    event = build_sell_trade_event(
        ticker="AAPL", sig=sig, holding=holding, price=110.0,
        today=dt.date(2026, 6, 9), regime="BULL_CALM", confidence=0.9,
        regime_params={}, config=_config(None),
    )
    assert event["model_type"] == "Manual"
    assert event["active_scorer"] is None


# ── decision trace rows ───────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["hf_patchtst", "xgb"])
def test_ticker_daily_state_rows_stamp_active_scorer(kind: str) -> None:
    ctx = _ctx(kind)
    rows = build_ticker_daily_state_rows(
        config=ctx.config,
        ctx=ctx,
        selected_tickers={"AAPL"},
        blocked_map={},
        model_types={"AAPL": "XGBoost", "MSFT": "QLearning"},
    )
    by_ticker = {row["ticker"]: row for row in rows}
    assert by_ticker["AAPL"]["model_type"] == kind
    assert by_ticker["AAPL"]["active_scorer"] == kind
    assert by_ticker["AAPL"]["legacy_model_type"] == "XGBoost"
    assert by_ticker["MSFT"]["model_type"] == kind
    assert by_ticker["MSFT"]["legacy_model_type"] == "QLearning"


def test_ticker_daily_state_rows_without_panel_scoring_keep_legacy() -> None:
    ctx = _ctx(None)
    rows = build_ticker_daily_state_rows(
        config=ctx.config,
        ctx=ctx,
        selected_tickers=set(),
        blocked_map={},
        model_types={"AAPL": "XGBoost", "MSFT": "QLearning"},
    )
    by_ticker = {row["ticker"]: row for row in rows}
    assert by_ticker["AAPL"]["model_type"] == "XGBoost"
    assert by_ticker["AAPL"]["active_scorer"] is None
    assert by_ticker["MSFT"]["model_type"] == "QLearning"


# ── DB persistence round-trips ───────────────────────────────────────────────


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    return conn


def test_candidate_scores_rows_carry_active_scorer() -> None:
    conn = _db()
    cand = CandidateResult("AAPL", 0.1, 0.2, 0.0)
    cand.model_type = "hf_patchtst"  # annotated by ApplyScoresTask
    cand.legacy_model_type = "XGBoost"
    record_candidate_scores(
        conn, "run-1", [cand], {}, {"AAPL"},
        model_types={"AAPL": "XGBoost"},
        active_scorer="hf_patchtst",
    )
    row = conn.execute(
        "SELECT model_type, active_scorer, legacy_model_type"
        " FROM candidate_scores WHERE ticker='AAPL'"
    ).fetchone()
    assert row == ("hf_patchtst", "hf_patchtst", "XGBoost")


def test_candidate_scores_annotated_candidate_wins_over_stale_map() -> None:
    """Even without the explicit kwarg, panel-annotated rows beat the map."""
    conn = _db()
    cand = CandidateResult("AAPL", 0.1, 0.2, 0.0)
    cand.model_type = "hf_patchtst"
    cand.legacy_model_type = "XGBoost"
    record_candidate_scores(
        conn, "run-1", [cand], {}, set(), model_types={"AAPL": "XGBoost"},
    )
    row = conn.execute(
        "SELECT model_type, legacy_model_type FROM candidate_scores"
    ).fetchone()
    assert row == ("hf_patchtst", "XGBoost")


def test_candidate_scores_legacy_only_without_panel_scoring() -> None:
    conn = _db()
    record_candidate_scores(
        conn, "run-1", [CandidateResult("AAPL", 0.1, 0.2, 0.0)], {}, set(),
        model_types={"AAPL": "Manual"},
    )
    row = conn.execute(
        "SELECT model_type, active_scorer FROM candidate_scores"
    ).fetchone()
    assert row == ("Manual", None)


def test_record_trades_persists_active_scorer_columns() -> None:
    conn = _db()
    ctx = _ctx("hf_patchtst")
    order = stamp_order_attribution(
        {"ticker": "AAPL", "order_type": "market", "model_type": "XGBoost",
         "shares": 10, "price": 100.0},
        ctx=ctx,
        source_job="JointPortfolioQPJob",
        source_task="EmitBuy",
        acceptance_reason="unit_test",
    )
    event = build_buy_trade_event(order, date="2026-06-09")
    record_trades(conn, "run-1", [event])
    row = conn.execute(
        "SELECT model_type, active_scorer, legacy_model_type FROM trades"
    ).fetchone()
    assert row == ("hf_patchtst", "hf_patchtst", "XGBoost")


def test_record_ticker_daily_state_persists_active_scorer_columns() -> None:
    conn = _db()
    ctx = _ctx("hf_patchtst")
    rows = build_ticker_daily_state_rows(
        config=ctx.config,
        ctx=ctx,
        selected_tickers={"AAPL"},
        blocked_map={},
        model_types={"AAPL": "XGBoost", "MSFT": "QLearning"},
    )
    assert record_ticker_daily_state(
        conn, run_date=dt.date(2026, 6, 9), rows=rows, run_id="run-1",
    ) == 2
    got = {
        ticker: (model_type, scorer, legacy)
        for ticker, model_type, scorer, legacy in conn.execute(
            "SELECT ticker, model_type, active_scorer, legacy_model_type"
            " FROM ticker_daily_state"
        )
    }
    assert got["AAPL"] == ("hf_patchtst", "hf_patchtst", "XGBoost")
    assert got["MSFT"] == ("hf_patchtst", "hf_patchtst", "QLearning")


@pytest.mark.parametrize("kind", ["hf_patchtst", "xgb"])
def test_score_distribution_rows_stamp_active_scorer(kind: str) -> None:
    conn = _db()
    ctx = _ctx(kind)
    ctx.config["score_db"] = {"enabled": True}
    ctx.regime = "BULL_CALM"
    ctx.run_id = "run-1"
    ctx._db = conn
    for cand in ctx.candidates:
        cand.rank_score = 0.5
    RecordScoreDistributionTask().run(ctx)
    rows = conn.execute(
        "SELECT ticker, model_type, active_scorer, legacy_model_type"
        " FROM score_distribution ORDER BY ticker"
    ).fetchall()
    assert rows == [
        ("AAPL", kind, kind, "XGBoost"),
        ("MSFT", kind, kind, "QLearning"),
    ]


def test_score_distribution_legacy_behavior_without_panel_scoring() -> None:
    conn = _db()
    ctx = _ctx(None)
    ctx.config["score_db"] = {"enabled": True}
    ctx.regime = "BULL_CALM"
    ctx.run_id = "run-1"
    ctx._db = conn
    RecordScoreDistributionTask().run(ctx)
    rows = conn.execute(
        "SELECT ticker, model_type, active_scorer FROM score_distribution"
        " ORDER BY ticker"
    ).fetchall()
    assert rows == [("AAPL", "XGBoost", None), ("MSFT", "QLearning", None)]


# ── top-level live-bridge surfaces ───────────────────────────────────────────


def test_live_bridge_score_snapshot_stamps_active_scorer() -> None:
    from renquant_pipeline.order_attribution import (
        score_snapshot as live_score_snapshot,
    )

    ctx = SimpleNamespace(
        strategy_config=_config("hf_patchtst"),
        scores={"AAPL": 0.5},
        blocked_by={},
        artifact_manifest={},
    )
    source = SimpleNamespace(ticker="AAPL", model_type="XGBoost")
    snap = live_score_snapshot({"ticker": "AAPL"}, ctx, source_obj=source)
    assert snap["model_type"] == "hf_patchtst"
    assert snap["active_scorer"] == "hf_patchtst"
    assert snap["legacy_model_type"] == "XGBoost"


def test_live_bridge_daily_state_rows_stamp_active_scorer() -> None:
    from renquant_pipeline.decision_trace import (
        build_ticker_daily_state_rows as live_build_rows,
    )

    config = _config("hf_patchtst")
    ctx = SimpleNamespace(
        scores={"AAPL": 0.5, "MSFT": 0.4},
        account_snapshot={},
        market_snapshot={},
        regime="BULL_CALM",
        confidence=0.9,
    )
    rows = live_build_rows(
        config,
        ctx,
        selected_tickers=["AAPL"],
        model_types={"AAPL": "XGBoost", "MSFT": "QLearning"},
    )
    by_ticker = {row["ticker"]: row for row in rows}
    assert by_ticker["AAPL"]["model_type"] == "hf_patchtst"
    assert by_ticker["AAPL"]["active_scorer"] == "hf_patchtst"
    assert by_ticker["AAPL"]["legacy_model_type"] == "XGBoost"
    assert by_ticker["MSFT"]["model_type"] == "hf_patchtst"
    assert by_ticker["MSFT"]["legacy_model_type"] == "QLearning"
