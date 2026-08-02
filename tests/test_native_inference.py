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


# ── pipeline#250 rollout step 2 (codex on #252): this facade is the surface
# renquant-orchestrator's native_live_inference consumes — a staged serving
# matrix must be finalized here, not dropped. ──────────────────────────────


def test_run_native_inference_snapshot_finalizes_staged_serving_features(
    tmp_path: Path,
) -> None:
    import datetime
    import hashlib

    import pandas as pd

    from renquant_pipeline.serving_features import (
        SERVING_FEATURES_BLOCK_KEY,
        SERVING_FEATURES_FILENAME,
        STAGED_ATTR,
        stage_serving_features,
    )

    matrix = pd.DataFrame(
        {"f1": [1.5, -2.0], "f2": [0.25, 4.0]}, index=["AAA", "BBB"],
    )

    class StagingPipeline(FakePipeline):
        def run(self, ctx) -> None:  # noqa: ANN001
            super().run(ctx)
            scorer = SimpleNamespace(
                feature_cols=["f1", "f2"],
                metadata={"feature_preprocess_version": 2},
            )
            stage_serving_features(ctx, matrix, scorer)

    ctx = _ctx()
    ctx.today = datetime.date(2026, 8, 2)
    output = tmp_path / "native-inference.json"

    snapshot = run_native_inference_snapshot(
        ctx, pipeline=StagingPipeline(), output_json=output,
    )

    # The codex probe's four false flags, flipped true:
    # 1. the snapshot itself carries the completed block
    assert snapshot.serving_features is not None
    assert snapshot.serving_features["status"] == "written"
    # 2. the written payload carries the block
    payload = json.loads(output.read_text(encoding="utf-8"))
    block = payload[SERVING_FEATURES_BLOCK_KEY]
    assert block["status"] == "written"
    assert block["n_rows"] == 2 and block["n_cols"] == 2
    assert block["feature_cutoff"] == "2026-08-02"
    assert block["feature_builder_version"] == "2"
    # 3. the parquet exists next to output_json and is the consumed matrix
    parquet = output.parent / SERVING_FEATURES_FILENAME
    assert parquet.exists()
    assert block["path"] == str(parquet)
    read_back = pd.read_parquet(parquet)
    assert list(read_back.columns) == ["ticker", "f1", "f2"]
    assert (
        read_back[["f1", "f2"]].to_numpy().tobytes()
        == matrix.to_numpy().tobytes()
    )
    # 4. the recorded sha256 matches the file bytes
    assert block["sha256"] == hashlib.sha256(parquet.read_bytes()).hexdigest()
    # and the staged state is consumed once the write completed
    assert getattr(ctx, STAGED_ATTR, None) is None


def test_run_native_inference_snapshot_without_staging_stays_byte_identical(
    tmp_path: Path,
) -> None:
    from renquant_pipeline.serving_features import (
        SERVING_FEATURES_BLOCK_KEY,
        SERVING_FEATURES_FILENAME,
    )

    output = tmp_path / "native-inference.json"
    snapshot = run_native_inference_snapshot(
        _ctx(), pipeline=FakePipeline(), output_json=output,
    )

    assert snapshot.serving_features is None
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert SERVING_FEATURES_BLOCK_KEY not in payload
    assert not (output.parent / SERVING_FEATURES_FILENAME).exists()
