from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pandas as pd

from renquant_pipeline.context import InferenceContext
from renquant_pipeline.kernel.decision_trace import build_ticker_daily_state_rows
from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
    ApplyScoresTask,
    LoadScorerTask,
)
from renquant_pipeline.kernel.pipeline.order_attribution import stamp_order_attribution
from renquant_pipeline.kernel.selection import CandidateResult


class _FakePatchTSTScorer:
    feature_cols = ["alpha"]
    seq_len = 2
    requires_history = True
    metadata = {}

    def score_with_history(self, panel_history, target_tickers):
        assert len(panel_history) >= 2
        return pd.Series(
            {ticker: 0.7 - i * 0.1 for i, ticker in enumerate(target_tickers)},
            name="panel_score",
        )


class _FakePatchTSTHandler:
    @classmethod
    def scorer_loader(cls, artifact_path, config):
        assert str(artifact_path).endswith("hf_patchtst_all_seed44_model.pt")
        return _FakePatchTSTScorer()


def _ctx(tmp_path) -> InferenceContext:
    artifact = tmp_path / "hf_patchtst_all_seed44_model.pt"
    artifact.write_bytes(b"checkpoint")
    return InferenceContext(
        config={
            "_strategy_dir": str(tmp_path),
            "watchlist": ["AAPL", "MSFT"],
            "sector_map": {"AAPL": "TECH", "MSFT": "TECH"},
            "ranking": {
                "panel_scoring": {
                    "enabled": True,
                    "kind": "hf_patchtst",
                    "artifact_path": artifact.name,
                    "strict_config_consistency": False,
                    "shadow_models": [
                        {
                            "name": "xgb_alpha158_fund_previous_primary",
                            "kind": "xgb",
                            "artifact_path": "artifacts/prod/panel-ltr.alpha158_fund.json",
                        }
                    ],
                }
            },
        },
        today=dt.date(2026, 6, 7),
        candidates=[
            CandidateResult("AAPL", 0.1, 0.1, 0.0),
            CandidateResult("MSFT", 0.1, 0.1, 0.0),
        ],
        holdings={
            "AAPL": SimpleNamespace(ticker="AAPL", panel_score=None, rank_score=None),
        },
    )


def test_patchtst_primary_stamps_active_scorer_and_trace(monkeypatch, tmp_path) -> None:
    from renquant_pipeline.kernel.panel_pipeline.model_registry import registry

    monkeypatch.setitem(registry._handlers, "hf_patchtst", _FakePatchTSTHandler)
    ctx = _ctx(tmp_path)
    ctx._panel_matrix = pd.DataFrame({"alpha": [1.0, 2.0]}, index=["AAPL", "MSFT"])
    ctx._panel_history = pd.DataFrame(
        {
            "date": [pd.Timestamp("2026-06-04"), pd.Timestamp("2026-06-05")],
            "ticker": ["AAPL", "MSFT"],
            "alpha": [1.0, 2.0],
        }
    )

    assert LoadScorerTask().run(ctx) is None
    assert ctx._active_panel_model_type == "hf_patchtst"
    assert ctx._active_panel_scorer["shadow_models"] == [
        {
            "name": "xgb_alpha158_fund_previous_primary",
            "kind": "xgb",
            "artifact_path": "artifacts/prod/panel-ltr.alpha158_fund.json",
        }
    ]

    assert ApplyScoresTask().run(ctx) is None

    assert {c.ticker: c.model_type for c in ctx.candidates} == {
        "AAPL": "hf_patchtst",
        "MSFT": "hf_patchtst",
    }
    assert ctx.holdings["AAPL"].model_type == "hf_patchtst"
    rows = build_ticker_daily_state_rows(
        config=ctx.config,
        ctx=ctx,
        selected_tickers={"AAPL"},
        blocked_map={},
        model_types={},
    )
    assert {row["ticker"]: row["model_type"] for row in rows} == {
        "AAPL": "hf_patchtst",
        "MSFT": "hf_patchtst",
    }


def test_order_attribution_falls_back_to_active_panel_model_type(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    ctx._active_panel_model_type = "hf_patchtst"

    order = stamp_order_attribution(
        {
            "ticker": "AAPL",
            "action": "buy",
            "quantity": 1,
            "order_type": "market",
        },
        ctx=ctx,
        source_job="SizeAndEmitTask",
        source_task="EmitBuy",
        acceptance_reason="unit_test",
    )

    assert order["model_type"] == "hf_patchtst"
    assert order["score_snapshot"]["model_type"] == "hf_patchtst"
