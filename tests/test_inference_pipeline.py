from __future__ import annotations

import pytest

from renquant_common import Task
from renquant_pipeline import InferenceContext, RuntimeInferencePipeline


def _ctx() -> InferenceContext:
    return InferenceContext(
        strategy_config={"watchlist": ["AAPL", "MSFT"]},
        artifact_manifest={"artifact_id": "panel-ltr-prod"},
        market_snapshot={"as_of": "2026-05-25"},
    )


class ScoreTask(Task):
    def run(self, ctx: InferenceContext) -> bool | None:
        ctx.scores = {"AAPL": 0.7, "MSFT": 0.2}
        ctx.decision_trace.append({"stage": "score", "n": 2})
        return True


class SelectTask(Task):
    def run(self, ctx: InferenceContext) -> bool | None:
        ctx.order_intents.append({"ticker": "AAPL", "action": "buy"})
        ctx.decision_trace.append({"stage": "select", "selected": "AAPL"})
        return True


def test_runtime_pipeline_emits_order_intents_and_trace() -> None:
    ctx = _ctx()
    result = RuntimeInferencePipeline([ScoreTask(), SelectTask()]).run(ctx)

    assert result.ok is True
    assert result.name == "runtime-inference"
    assert ctx.scores["AAPL"] == pytest.approx(0.7)
    assert ctx.order_intents == [{"ticker": "AAPL", "action": "buy"}]
    assert [row["stage"] for row in ctx.decision_trace] == ["score", "select"]


def test_runtime_pipeline_requires_artifact_manifest() -> None:
    ctx = _ctx()
    ctx.artifact_manifest = {}

    with pytest.raises(ValueError, match="artifact_manifest missing"):
        RuntimeInferencePipeline([ScoreTask()]).run(ctx)
