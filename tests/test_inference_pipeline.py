from __future__ import annotations

import json

import pytest

from renquant_common import Task
from renquant_pipeline import (
    InferenceContext,
    RuntimeInferencePipeline,
    runtime_inference_payload,
    write_runtime_inference_payload,
)


def _ctx() -> InferenceContext:
    return InferenceContext(
        strategy_config={"watchlist": ["AAPL", "MSFT"]},
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
            "metrics": {"accepted": True},
        },
        market_snapshot={"as_of": "2026-05-25"},
    )


class ScoreTask(Task):
    def run(self, ctx: InferenceContext) -> bool | None:
        ctx.scores = {"AAPL": 0.7, "MSFT": 0.2}
        ctx.decision_trace.append({"stage": "score", "n": 2})
        return True


class SelectTask(Task):
    def run(self, ctx: InferenceContext) -> bool | None:
        ctx.order_intents.append({"ticker": "AAPL", "action": "buy", "quantity": 1})
        ctx.decision_trace.append({"stage": "select", "selected": "AAPL"})
        return True


def test_runtime_pipeline_emits_order_intents_and_trace() -> None:
    ctx = _ctx()
    result = RuntimeInferencePipeline([ScoreTask(), SelectTask()]).run(ctx)

    assert result.ok is True
    assert result.name == "runtime-inference"
    assert ctx.scores["AAPL"] == pytest.approx(0.7)
    assert ctx.order_intents == [{"ticker": "AAPL", "action": "buy", "quantity": 1}]
    assert [row["stage"] for row in ctx.decision_trace] == ["score", "select"]


def test_runtime_inference_payload_is_native_bundle_ready(tmp_path) -> None:
    ctx = _ctx()
    RuntimeInferencePipeline([ScoreTask(), SelectTask()]).run(ctx)
    out = tmp_path / "inference.json"

    payload = runtime_inference_payload(ctx)
    written = write_runtime_inference_payload(ctx, out)

    assert payload["source"] == "renquant_pipeline.runtime_inference"
    assert payload["market_as_of"] == "2026-05-25"
    assert payload["decision_trace"] == ctx.decision_trace
    assert payload["order_intents"] == ctx.order_intents
    assert payload["scores"] == ctx.scores
    assert payload["blocked_by"] == {}
    assert payload["buy_blocked"] is False
    assert written == out
    assert json.loads(out.read_text(encoding="utf-8")) == payload


def test_runtime_pipeline_requires_artifact_manifest() -> None:
    ctx = _ctx()
    ctx.artifact_manifest = {}

    with pytest.raises(ValueError, match="artifact manifest missing"):
        RuntimeInferencePipeline([ScoreTask()]).run(ctx)


def test_runtime_pipeline_requires_data_manifest() -> None:
    ctx = _ctx()
    ctx.data_manifest = {}

    with pytest.raises(ValueError, match="data manifest missing"):
        RuntimeInferencePipeline([ScoreTask()]).run(ctx)
