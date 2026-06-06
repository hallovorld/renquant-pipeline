from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

from renquant_pipeline.kernel.persistence import ensure_schema, record_ticker_daily_state
from renquant_pipeline import (
    EmitAttributedOrderIntentsTask,
    InferenceContext,
    PanelScoringJob,
    RuntimeInferencePipeline,
    build_ticker_daily_state_rows,
    stamp_order_attribution,
    validate_order_attribution,
)


def _ctx(*, feature_frame=None, panel_scores=None, artifact_extra=None) -> InferenceContext:
    artifact = {
        "artifact_id": "panel-ltr-prod",
        "model_family": "gbdt-panel-ltr",
        "strategy": "renquant_104",
        "fingerprint": "sha256:model",
        "uri": "object://renquant-artifacts/panel-ltr-prod.json",
        "promotion_status": "prod",
        "feature_cols": ["alpha_1", "alpha_2"],
        "metrics": {
            "accepted": True,
            "oos_mean_ic": 0.04,
            "wf_sharpe": 1.5,
            "spy_relative_sharpe": 0.3,
            "spy_relative_apy": 0.02,
            "config_fingerprint": "sha256:cfg",
        },
    }
    artifact.update(artifact_extra or {})
    return InferenceContext(
        strategy_config={
            "watchlist": ["AAPL", "MSFT"],
            "config_fingerprint": "sha256:cfg",
            "sector_map": {"AAPL": "TECH", "MSFT": "TECH"},
            "ranking": {
                "panel_scoring": {
                    "enabled": True,
                    "buy_floor": 0.5,
                    "model_admission": {
                        "enabled": True,
                        "min_oos_mean_ic": 0.01,
                        "min_wf_sharpe": 0.0,
                        "min_spy_relative_sharpe": 0.0,
                        "require_config_fingerprint": True,
                    },
                }
            },
        },
        data_manifest={
            "dataset_id": "daily-fixture",
            "schema_version": "fixture-v1",
            "fingerprint": "sha256:data",
            "uri": "object://renquant-data/daily-fixture.parquet",
            "asset_class": "equity",
        },
        artifact_manifest=artifact,
        market_snapshot={
            "as_of": "2026-05-25",
            "feature_frame": feature_frame
            or {
                "AAPL": {"alpha_1": 1.0, "alpha_2": 0.5},
                "MSFT": {"alpha_1": -1.0, "alpha_2": 0.1},
            },
            "panel_scores": (
                {"AAPL": 0.72, "MSFT": 0.21}
                if panel_scores is None
                else panel_scores
            ),
            "order_quantity_by_ticker": {"AAPL": 3, "MSFT": 2},
        },
    )


def test_panel_scoring_job_admits_strong_candidates_and_records_trace() -> None:
    ctx = _ctx()

    result = RuntimeInferencePipeline([PanelScoringJob()]).run(ctx)

    assert result.ok is True
    assert ctx.scores == {"AAPL": pytest.approx(0.72), "MSFT": pytest.approx(0.21)}
    assert ctx.accepted_candidates == [
        {
            "ticker": "AAPL",
            "panel_score": pytest.approx(0.72),
            "rank_score": pytest.approx(0.72),
            "blocked_by": None,
            "sector": "TECH",
            "model_type": "gbdt-panel-ltr",
        }
    ]
    assert ctx.blocked_by == {"MSFT": "panel_score_below_buy_floor"}
    latest = [row for row in ctx.decision_trace if row.get("ticker") == "MSFT"][-1]
    assert latest["sector"] == "TECH"
    assert latest["model_type"] == "gbdt-panel-ltr"
    assert latest["blocked_by"] == "panel_score_below_buy_floor"
    assert latest["panel_score"] == pytest.approx(0.21)
    assert latest["model_admission_ok"] is True
    assert latest["model_admission_reason"] is None


def test_model_admission_rejection_is_recorded_in_runtime_trace() -> None:
    ctx = _ctx(
        artifact_extra={
            "metrics": {
                "accepted": True,
                "oos_mean_ic": 0.04,
                "wf_sharpe": 1.5,
                "spy_relative_sharpe": -0.2,
                "spy_relative_apy": 0.02,
                "config_fingerprint": "sha256:cfg",
            }
        }
    )

    RuntimeInferencePipeline([PanelScoringJob(emit_orders=True)]).run(ctx)

    assert ctx.buy_blocked is True
    assert ctx.order_intents == []
    latest = [row for row in ctx.decision_trace if row.get("ticker") == "AAPL"][-1]
    assert latest["blocked_by"] == "model_spy_relative_sharpe_below_floor"
    assert latest["model_admission_ok"] is False
    assert latest["model_admission_reason"] == "model_spy_relative_sharpe_below_floor"


def test_runtime_regime_admission_is_recorded_in_decision_trace() -> None:
    ctx = _ctx()
    ctx.regime = "BULL_CALM"
    ctx._regime_model_admission = {
        "ok": False,
        "reason": "regime_admission:failed:BULL_CALM",
        "regime": "BULL_CALM",
    }

    rows = build_ticker_daily_state_rows(
        config=ctx.strategy_config,
        ctx=ctx,
        selected_tickers=set(),
        blocked_map={"AAPL": "regime_admission:failed:BULL_CALM"},
        model_types={"AAPL": "gbdt-panel-ltr"},
    )

    latest = next(row for row in rows if row["ticker"] == "AAPL")
    assert latest["model_admission_ok"] is False
    assert latest["model_admission_reason"] == "regime_admission:failed:BULL_CALM"
    assert latest["current_regime_admitted"] is False
    assert latest["current_regime_admission_reason"] == "regime_admission:failed:BULL_CALM"
    assert json.loads(latest["admitted_regimes"]) == []
    assert json.loads(latest["blocked_regimes"]) == ["BULL_CALM"]


def test_panel_scoring_job_task_order_is_explicit() -> None:
    job = PanelScoringJob(emit_orders=True)

    assert [task.name for task in job.tasks] == [
        "LoadScorerTask",
        "BuildFeatureMatrixTask",
        "ApplyScoresTask",
        "ApplyGlobalCalibrationTask",
        "RegimeModelAdmissionTask",
        "VetoWeakBuysTask",
        "EmitAttributedOrderIntentsTask",
    ]


def test_missing_feature_contract_fails_closed_without_orders() -> None:
    ctx = _ctx(feature_frame={"AAPL": {"alpha_1": 1.0}, "MSFT": {"alpha_1": 0.2}})

    RuntimeInferencePipeline([PanelScoringJob(emit_orders=True)]).run(ctx)

    assert ctx.buy_blocked is True
    assert ctx.order_intents == []
    assert ctx.blocked_by == {
        "AAPL": "feature_contract_missing:alpha_2",
        "MSFT": "feature_contract_missing:alpha_2",
    }


def test_missing_panel_scores_fail_closed_without_silent_fallback() -> None:
    ctx = _ctx(panel_scores={})

    RuntimeInferencePipeline([PanelScoringJob(emit_orders=True)]).run(ctx)

    assert ctx.buy_blocked is True
    assert ctx.order_intents == []
    assert ctx.blocked_by == {"AAPL": "missing_panel_score", "MSFT": "missing_panel_score"}


def test_attributed_order_intents_include_model_sector_and_decision_inputs() -> None:
    ctx = _ctx()

    RuntimeInferencePipeline([PanelScoringJob(emit_orders=True)]).run(ctx)

    assert len(ctx.order_intents) == 1
    order = ctx.order_intents[0]
    validate_order_attribution(order)
    assert order["ticker"] == "AAPL"
    assert order["quantity"] == 3
    attribution = order["attribution"]
    assert attribution["source_job"] == "PanelScoringJob"
    assert attribution["score_snapshot"]["model_type"] == "gbdt-panel-ltr"
    assert attribution["score_snapshot"]["sector"] == "TECH"
    assert attribution["score_snapshot"]["panel_score"] == pytest.approx(0.72)
    assert attribution["decision_inputs"]["buy_floor"] == pytest.approx(0.5)


def test_attribution_validation_rejects_unexplained_order() -> None:
    with pytest.raises(ValueError, match="order missing attribution"):
        validate_order_attribution({"ticker": "AAPL", "action": "buy", "quantity": 1})


def test_stamp_order_attribution_requires_quantity() -> None:
    ctx = _ctx()

    with pytest.raises(ValueError, match="order missing quantity"):
        stamp_order_attribution(
            {"ticker": "AAPL", "action": "buy"},
            ctx,
            source_job="job",
            source_task="task",
            acceptance_reason="test",
        )


def test_decision_trace_builder_includes_qp_and_broker_state() -> None:
    ctx = _ctx()
    ctx.scores = {"AAPL": 0.7, "MSFT": 0.2}
    ctx.account_snapshot = {"positions": {"MSFT": {"quantity": 5}}}

    rows = build_ticker_daily_state_rows(
        ctx.strategy_config,
        ctx,
        selected_tickers=["AAPL"],
        pending_broker_tickers=["AAPL"],
        qp_delta_by_ticker={"AAPL": 0.1, "MSFT": -0.2},
        qp_target_by_ticker={"AAPL": 0.15, "MSFT": 0.0},
        qp_status="solved",
    )

    aapl = next(row for row in rows if row["ticker"] == "AAPL")
    msft = next(row for row in rows if row["ticker"] == "MSFT")
    assert aapl["selected"] is True
    assert aapl["pending_at_broker"] is True
    assert aapl["qp_delta"] == pytest.approx(0.1)
    assert msft["has_position"] is True
    assert msft["qp_target"] == pytest.approx(0.0)


def test_ticker_daily_state_persists_model_admission_trace() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)

    n_rows = record_ticker_daily_state(
        conn,
        run_date=dt.date(2026, 6, 2),
        run_id="daily-full-shadow-20260602",
        rows=[
            {
                "ticker": "AAPL",
                "selected": 0,
                "blocked_by": "model_spy_relative_sharpe_below_floor",
                "model_admission_ok": 0,
                "model_admission_reason": "model_spy_relative_sharpe_below_floor",
                "current_regime_admitted": 0,
                "current_regime_admission_reason": "regime_admission:failed:BULL_CALM",
                "admitted_regimes": "[]",
                "blocked_regimes": "[\"BULL_CALM\"]",
            }
        ],
    )

    assert n_rows == 1
    row = conn.execute(
        """SELECT model_admission_ok, model_admission_reason,
                  current_regime_admitted, current_regime_admission_reason,
                  admitted_regimes, blocked_regimes
             FROM ticker_daily_state
            WHERE run_id = ? AND ticker = ?""",
        ("daily-full-shadow-20260602", "AAPL"),
    ).fetchone()
    assert row == (
        0,
        "model_spy_relative_sharpe_below_floor",
        0,
        "regime_admission:failed:BULL_CALM",
        "[]",
        "[\"BULL_CALM\"]",
    )
