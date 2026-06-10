from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import renquant_pipeline.native_inference as mod
from renquant_pipeline import run_native_inference_snapshot


class FakePipeline:
    def __init__(self) -> None:
        self.seen = []

    def run(self, ctx) -> None:  # noqa: ANN001
        self.seen.append(ctx)
        ctx.orders = [{"ticker": "AAPL", "action": "buy", "shares": 2}]
        ctx.decision_trace = [{"ticker": "AAPL", "stage": "fake_native_pipeline"}]
        ctx.scores = {"AAPL": 0.8}


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        config={"watchlist": ["AAPL"]},
        market_snapshot={"as_of": "2026-06-09"},
        account_snapshot={"positions": {}},
    )


def test_run_native_inference_snapshot_runs_supplied_pipeline_and_writes_payload(
    tmp_path: Path,
) -> None:
    pipeline = FakePipeline()
    output = tmp_path / "native-inference.json"

    snapshot = run_native_inference_snapshot(_ctx(), pipeline=pipeline, output_json=output)

    assert pipeline.seen
    assert snapshot.order_intents == [{"ticker": "AAPL", "action": "buy", "shares": 2}]
    assert snapshot.decision_trace == [{"ticker": "AAPL", "stage": "fake_native_pipeline"}]
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["source"] == "renquant_pipeline.live_context_inference"
    assert payload["order_intents"] == snapshot.order_intents


def test_run_native_inference_snapshot_selects_sell_only_pipeline(monkeypatch) -> None:
    calls = []

    def fake_default_pipeline(*, sell_only: bool) -> FakePipeline:
        calls.append(sell_only)
        return FakePipeline()

    monkeypatch.setattr(mod, "_default_pipeline", fake_default_pipeline)

    snapshot = run_native_inference_snapshot(_ctx(), sell_only=True)

    assert calls == [True]
    assert snapshot.order_intents == [{"ticker": "AAPL", "action": "buy", "shares": 2}]


def test_native_inference_facade_does_not_import_umbrella_runner() -> None:
    src = (Path(mod.__file__).read_text(encoding="utf-8"))

    assert "live.runner" not in src
    assert "adapters.runner" not in src
    assert "RenQuant" not in src
