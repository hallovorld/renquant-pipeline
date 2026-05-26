from __future__ import annotations

import pytest

from renquant_pipeline import (
    InferenceContext,
    PanelScoringJob,
    RuntimeInferencePipeline,
    transform_feature_rows,
)


def test_transform_raw_feature_rows_uses_artifact_normalization_and_clipping() -> None:
    rows = {"AAPL": {"alpha": 12.0, "fund": 250.0, "identity": -9.0}}
    metadata = {
        "feature_means": [10.0, 100.0, 0.0],
        "feature_stds": [2.0, 10.0, 1.0],
        "feature_norm_kind": ["legacy_full_z", "robust_z", "identity"],
        "feature_raw_clip_low": [0.0, 0.0, -10.0],
        "feature_raw_clip_high": [20.0, 200.0, 10.0],
    }

    out = transform_feature_rows(
        rows,
        ["alpha", "fund", "identity"],
        metadata,
        source_space="raw",
        clip=5.0,
    )

    assert out["AAPL"]["alpha"] == pytest.approx(1.0)
    assert out["AAPL"]["fund"] == pytest.approx(5.0)
    assert out["AAPL"]["identity"] == pytest.approx(-5.0)


def test_transform_panel_feature_rows_only_normalizes_panel_raw_columns() -> None:
    rows = {"AAPL": {"alpha": 12.0, "fund": 120.0, "pead": 2.0}}
    metadata = {
        "feature_means": [10.0, 100.0, 1.0],
        "feature_stds": [2.0, 10.0, 1.0],
        "feature_norm_kind": ["legacy_full_z", "robust_z", "panel_raw_z"],
    }

    out = transform_feature_rows(
        rows,
        ["alpha", "fund", "pead"],
        metadata,
        source_space="panel",
        clip=0,
    )

    assert out["AAPL"]["alpha"] == pytest.approx(12.0)
    assert out["AAPL"]["fund"] == pytest.approx(2.0)
    assert out["AAPL"]["pead"] == pytest.approx(1.0)


def test_transform_raw_feature_rows_requires_metadata() -> None:
    with pytest.raises(ValueError, match="normalization metadata"):
        transform_feature_rows(
            {"AAPL": {"alpha": 1.0}},
            ["alpha"],
            {},
            source_space="raw",
        )


def test_panel_scoring_accepts_raw_feature_frame_after_transform() -> None:
    ctx = InferenceContext(
        strategy_config={
            "watchlist": ["AAPL", "MSFT"],
            "sector_map": {"AAPL": "TECH", "MSFT": "TECH"},
            "ranking": {"panel_scoring": {"enabled": True, "buy_floor": 0.0}},
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
            "artifact_id": "linear-panel",
            "model_family": "gbdt-panel-ltr",
            "strategy": "renquant_104",
            "fingerprint": "sha256:model",
            "uri": "object://renquant-artifacts/linear-panel.json",
            "promotion_status": "prod",
            "feature_cols": ["alpha_1", "alpha_2"],
            "feature_means": [10.0, 100.0],
            "feature_stds": [2.0, 10.0],
            "feature_norm_kind": ["legacy_full_z", "robust_z"],
            "linear_weights": {"alpha_1": 1.0, "alpha_2": 0.1},
            "metrics": {"accepted": True},
        },
        market_snapshot={
            "as_of": "2026-05-25",
            "raw_feature_frame": {
                "AAPL": {"alpha_1": 12.0, "alpha_2": 120.0},
                "MSFT": {"alpha_1": 8.0, "alpha_2": 100.0},
            },
            "order_quantity_by_ticker": {"AAPL": 1, "MSFT": 1},
        },
    )

    RuntimeInferencePipeline([PanelScoringJob(emit_orders=True)]).run(ctx)

    assert ctx.scores["AAPL"] == pytest.approx(1.2)
    assert ctx.scores["MSFT"] == pytest.approx(-1.0)
    assert [order["ticker"] for order in ctx.order_intents] == ["AAPL"]


def test_panel_scoring_blocks_raw_feature_frame_without_metadata() -> None:
    ctx = InferenceContext(
        strategy_config={
            "watchlist": ["AAPL"],
            "ranking": {"panel_scoring": {"enabled": True}},
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
            "artifact_id": "linear-panel",
            "model_family": "gbdt-panel-ltr",
            "strategy": "renquant_104",
            "fingerprint": "sha256:model",
            "uri": "object://renquant-artifacts/linear-panel.json",
            "promotion_status": "prod",
            "feature_cols": ["alpha_1"],
            "linear_weights": {"alpha_1": 1.0},
            "metrics": {"accepted": True},
        },
        market_snapshot={
            "as_of": "2026-05-25",
            "raw_feature_frame": {"AAPL": {"alpha_1": 1.0}},
            "order_quantity_by_ticker": {"AAPL": 1},
        },
    )

    RuntimeInferencePipeline([PanelScoringJob(emit_orders=True)]).run(ctx)

    assert ctx.buy_blocked is True
    assert ctx.order_intents == []
    assert ctx.blocked_by["AAPL"].startswith("feature_transform_failed:")
