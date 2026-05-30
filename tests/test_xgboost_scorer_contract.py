from __future__ import annotations

import json

import pytest

from renquant_pipeline import InferenceContext, PanelScoringJob, RuntimeInferencePipeline


pytest.importorskip("xgboost")
pytest.importorskip("renquant_model_gbdt")


def _xgb_artifact(tmp_path) -> dict:
    import xgboost as xgb

    dtrain = xgb.DMatrix(
        [[1.0, 0.2], [0.8, 0.1], [-1.0, 0.0], [-0.7, -0.1]],
        label=[1.0, 0.8, -1.0, -0.8],
    )
    booster = xgb.train(
        {
            "objective": "reg:squarederror",
            "max_depth": 1,
            "eta": 1.0,
            "nthread": 1,
            "verbosity": 0,
            "seed": 7,
        },
        dtrain,
        num_boost_round=4,
        verbose_eval=False,
    )
    payload = {
        "version": 2,
        "kind": "panel_ltr_xgboost",
        "artifact_id": "unit-xgb-panel",
        "model_family": "gbdt-panel-ltr",
        "strategy": "renquant_104",
        "fingerprint": "sha256:model",
        "uri": "object://renquant-artifacts/unit-xgb-panel.json",
        "promotion_status": "prod",
        "feature_cols": ["alpha_1", "alpha_2"],
        "metrics": {"accepted": True},
        "booster_raw_json": bytes(booster.save_raw(raw_format="json")).decode("utf-8"),
    }
    path = tmp_path / "unit-xgb-panel.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    manifest = dict(payload)
    manifest.pop("booster_raw_json")
    manifest["local_artifact_path"] = str(path)
    return manifest


def _ctx(artifact: dict) -> InferenceContext:
    return InferenceContext(
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
        artifact_manifest=artifact,
        market_snapshot={
            "as_of": "2026-05-25",
            "feature_frame": {
                "AAPL": {"alpha_1": 1.0, "alpha_2": 0.2},
                "MSFT": {"alpha_1": -1.0, "alpha_2": 0.0},
            },
        },
    )


def test_panel_scoring_loads_real_xgboost_artifact_without_explicit_scores(tmp_path) -> None:
    ctx = _ctx(_xgb_artifact(tmp_path))

    RuntimeInferencePipeline([PanelScoringJob(emit_orders=True)]).run(ctx)

    assert ctx.scores["AAPL"] > ctx.scores["MSFT"]
    assert [row["ticker"] for row in ctx.accepted_candidates] == ["AAPL"]
    assert ctx.order_intents[0]["attribution"]["score_snapshot"]["artifact_id"] == "unit-xgb-panel"


def test_broken_local_xgboost_artifact_fails_closed(tmp_path) -> None:
    artifact = _xgb_artifact(tmp_path)
    artifact["local_artifact_path"] = str(tmp_path / "missing.json")
    ctx = _ctx(artifact)

    RuntimeInferencePipeline([PanelScoringJob(emit_orders=True)]).run(ctx)

    assert ctx.buy_blocked is True
    assert ctx.order_intents == []
    assert ctx.blocked_by["AAPL"].startswith("panel_scorer_load_failed:")
