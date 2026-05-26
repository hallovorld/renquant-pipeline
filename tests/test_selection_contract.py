from __future__ import annotations

import pytest

from renquant_pipeline import InferenceContext, PanelScoringJob, RuntimeInferencePipeline, SelectionJob


def _ctx() -> InferenceContext:
    return InferenceContext(
        strategy_config={
            "watchlist": ["AAPL", "MSFT", "IBM"],
            "sector_map": {"AAPL": "TECH", "MSFT": "TECH", "IBM": "TECH"},
            "ranking": {
                "panel_scoring": {"enabled": True, "buy_floor": 0.5},
                "selection": {"enabled": True, "max_new_positions": 1},
            },
            "execution": {"default_quantity": 1},
        },
        data_manifest={
            "dataset_id": "daily-fixture",
            "schema_version": "fixture-v1",
            "fingerprint": "sha256:data",
            "uri": "object://renquant-data/daily-fixture.parquet",
            "asset_class": "equity",
        },
        artifact_manifest={
            "artifact_id": "panel-ltr-prod",
            "model_family": "gbdt-panel-ltr",
            "strategy": "renquant_104",
            "fingerprint": "sha256:model",
            "uri": "object://renquant-artifacts/panel-ltr-prod.json",
            "promotion_status": "prod",
            "feature_cols": ["alpha_1"],
            "metrics": {"accepted": True},
        },
        market_snapshot={
            "as_of": "2026-05-25",
            "feature_frame": {
                "AAPL": {"alpha_1": 1.0},
                "MSFT": {"alpha_1": 1.0},
                "IBM": {"alpha_1": 1.0},
            },
            "panel_scores": {"AAPL": 0.7, "MSFT": 0.9, "IBM": 0.2},
            "order_quantity_by_ticker": {"AAPL": 1, "MSFT": 1, "IBM": 1},
        },
    )


def test_selection_job_only_selects_top_accepted_candidate() -> None:
    ctx = _ctx()

    RuntimeInferencePipeline([PanelScoringJob(), SelectionJob()]).run(ctx)

    assert [row["ticker"] for row in ctx.accepted_candidates] == ["AAPL", "MSFT"]
    assert [row["ticker"] for row in ctx.selected_candidates] == ["MSFT"]
    assert ctx.blocked_by == {"IBM": "panel_score_below_buy_floor"}


def test_emit_orders_uses_selected_candidates_when_selection_runs_first() -> None:
    ctx = _ctx()

    RuntimeInferencePipeline([PanelScoringJob(), SelectionJob(), PanelScoringJob(emit_orders=True)]).run(ctx)

    assert [order["ticker"] for order in ctx.order_intents] == ["MSFT"]


def test_selection_rejects_promoted_candidate_not_in_alpha_set() -> None:
    ctx = _ctx()
    RuntimeInferencePipeline([PanelScoringJob()]).run(ctx)
    ctx.selected_candidates = [{"ticker": "IBM"}]

    with pytest.raises(ValueError, match="non-accepted"):
        SelectionJob().tasks[1].run(ctx)


def test_selection_rejects_blocked_candidate_even_if_manually_selected() -> None:
    ctx = _ctx()
    RuntimeInferencePipeline([PanelScoringJob()]).run(ctx)
    ctx.selected_candidates = [{"ticker": "IBM"}]

    with pytest.raises(ValueError, match="non-accepted"):
        SelectionJob().tasks[1].run(ctx)
