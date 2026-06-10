from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from renquant_common import Task
from renquant_pipeline import (
    InferenceContext,
    LiveContextSnapshot,
    RuntimeInferencePipeline,
    live_context_snapshot_from_live_context,
    runtime_inference_payload,
    runtime_inference_payload_from_live_context,
    write_runtime_inference_payload,
    write_runtime_inference_payload_from_live_context,
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


def test_runtime_inference_payload_from_live_context_prefers_existing_trace() -> None:
    ctx = {
        "config": {"watchlist": ["AAPL"]},
        "market_snapshot": {"as_of": "2026-06-08"},
        "decision_trace": [{"ticker": "AAPL", "stage": "score"}],
        "orders": [{"ticker": "AAPL", "action": "buy", "quantity": 1}],
        "_blocked_by_ticker": {"MSFT": "not_in_watchlist"},
        "_ticker_score_snapshot": {"AAPL": {"rank_score": 0.72}},
    }

    payload = runtime_inference_payload_from_live_context(ctx)

    assert payload["source"] == "renquant_pipeline.live_context_inference"
    assert payload["market_as_of"] == "2026-06-08"
    assert payload["decision_trace"] == [{"ticker": "AAPL", "stage": "score"}]
    assert payload["order_intents"] == [{"ticker": "AAPL", "action": "buy", "quantity": 1}]
    assert payload["blocked_by"] == {"MSFT": "not_in_watchlist"}
    assert payload["scores"] == {"AAPL": 0.72}


def test_live_context_snapshot_is_native_payload_contract() -> None:
    ctx = {
        "config": {"watchlist": ["AAPL"]},
        "market_snapshot": {"as_of": "2026-06-08"},
        "decision_trace": [{"ticker": "AAPL", "stage": "score"}],
        "orders": [{"ticker": "AAPL", "action": "buy", "quantity": 1}],
        "_blocked_by_ticker": {"MSFT": "not_in_watchlist"},
        "_ticker_score_snapshot": {"AAPL": {"rank_score": 0.72}},
        "buy_blocked": True,
    }

    snapshot = live_context_snapshot_from_live_context(ctx)

    assert isinstance(snapshot, LiveContextSnapshot)
    assert snapshot.strategy_config == {"watchlist": ["AAPL"]}
    assert snapshot.market_snapshot == {"as_of": "2026-06-08"}
    assert snapshot.account_snapshot == {}
    assert snapshot.market_as_of == "2026-06-08"
    assert snapshot.decision_trace == [{"ticker": "AAPL", "stage": "score"}]
    assert snapshot.order_intents == [{"ticker": "AAPL", "action": "buy", "quantity": 1}]
    assert snapshot.scores == {"AAPL": 0.72}
    assert snapshot.blocked_by == {"MSFT": "not_in_watchlist"}
    assert snapshot.pending_broker_tickers == []
    assert snapshot.buy_blocked is True
    assert snapshot.to_runtime_payload() == runtime_inference_payload_from_live_context(ctx)


def test_live_context_snapshot_derives_account_snapshot_from_legacy_holdings() -> None:
    class LiveCtx:
        config = {
            "watchlist": ["AAPL", "MSFT"],
            "sector_map": {"AAPL": "TECH", "MSFT": "TECH"},
        }
        market_snapshot = {"as_of": "2026-06-08", "regime": "BULL_CALM"}
        holdings = {"MSFT": SimpleNamespace(shares=5, avg_entry_price=101.5)}
        prices = {"MSFT": 120.0}
        cash = 1000.0
        portfolio_value = 1600.0
        orders = [{"ticker": "AAPL", "action": "buy", "quantity": 1}]
        pending_broker_tickers = ("AAPL",)
        _ticker_score_snapshot = {
            "AAPL": {"panel_score": 0.8},
            "MSFT": {"panel_score": 0.3},
        }

    snapshot = live_context_snapshot_from_live_context(LiveCtx())

    assert snapshot.account_snapshot == {
        "positions": {
            "MSFT": {
                "avg_entry_price": 101.5,
                "price": 120.0,
                "quantity": 5,
                "ticker": "MSFT",
            }
        },
        "cash": 1000.0,
        "portfolio_value": 1600.0,
    }
    assert snapshot.pending_broker_tickers == ["AAPL"]
    rows = {row["ticker"]: row for row in snapshot.decision_trace}
    assert rows["MSFT"]["has_position"] is True
    assert rows["AAPL"]["pending_at_broker"] is True


def test_live_context_snapshot_prefers_explicit_account_snapshot() -> None:
    class LiveCtx:
        config = {"watchlist": ["AAPL", "MSFT"]}
        market_snapshot = {"as_of": "2026-06-08"}
        account_snapshot = {"positions": {"AAPL": {"quantity": 2}}}
        holdings = {"MSFT": SimpleNamespace(shares=5)}

    snapshot = live_context_snapshot_from_live_context(LiveCtx())

    assert snapshot.account_snapshot == {"positions": {"AAPL": {"quantity": 2}}}
    rows = {row["ticker"]: row for row in snapshot.decision_trace}
    assert rows["AAPL"]["has_position"] is True
    assert rows["MSFT"]["has_position"] is False


def test_live_context_snapshot_normalizes_legacy_position_aliases() -> None:
    snapshot = live_context_snapshot_from_live_context({
        "config": {"watchlist": ["AAPL"]},
        "market_snapshot": {"as_of": "2026-06-08"},
        "holdings": {"AAPL": {"qty": 2, "shares": 2, "cost_basis": 101.0}},
        "prices": {"AAPL": 120.0},
    })

    assert snapshot.account_snapshot["positions"]["AAPL"] == {
        "cost_basis": 101.0,
        "price": 120.0,
        "quantity": 2,
        "ticker": "AAPL",
    }


def test_live_context_snapshot_rejects_bad_account_snapshot() -> None:
    with pytest.raises(ValueError, match="account_snapshot"):
        live_context_snapshot_from_live_context({
            "config": {"watchlist": ["AAPL"]},
            "market_snapshot": {"as_of": "2026-06-08"},
            "account_snapshot": ["not", "a", "dict"],
        })


def test_runtime_payload_from_legacy_runner_shape_keeps_schema_v1() -> None:
    payload = runtime_inference_payload_from_live_context({
        "config": {"watchlist": ["AAPL"]},
        "market_snapshot": {"as_of": "2026-06-08"},
        "holdings": {"AAPL": {"quantity": 2}},
        "pending_broker_tickers": ["AAPL"],
    })

    assert set(payload) == {
        "blocked_by",
        "buy_blocked",
        "decision_trace",
        "market_as_of",
        "order_intents",
        "schema_version",
        "scores",
        "source",
    }
    assert payload["schema_version"] == 1
    assert payload["source"] == "renquant_pipeline.live_context_inference"


def test_write_runtime_inference_payload_from_live_context_writes_json(tmp_path) -> None:
    output = tmp_path / "native-inference.json"
    ctx = {
        "config": {"watchlist": ["AAPL"]},
        "decision_trace": [],
        "orders": [],
        "today": "2026-06-08",
    }

    written = write_runtime_inference_payload_from_live_context(
        ctx,
        output,
        market_snapshot={"as_of": "2026-06-08"},
    )

    assert written == output
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["source"] == "renquant_pipeline.live_context_inference"
    assert payload["market_as_of"] == "2026-06-08"
    assert payload["decision_trace"] == []
    assert payload["order_intents"] == []


def test_runtime_inference_payload_from_live_context_builds_fallback_trace() -> None:
    class LiveCtx:
        config = {
            "watchlist": ["AAPL", "MSFT"],
            "sector_map": {"AAPL": "TECH", "MSFT": "TECH"},
        }
        market_snapshot = {"as_of": "2026-06-08", "regime": "BULL_CALM"}
        account_snapshot = {"positions": {"MSFT": {"quantity": 5}}}
        orders = [{"ticker": "AAPL", "action": "buy", "quantity": 1}]
        pending_broker_tickers = ["AAPL"]
        _blocked_by_ticker = {"MSFT": "risk_gate"}
        _ticker_score_snapshot = {
            "AAPL": {"panel_score": 0.8},
            "MSFT": {"panel_score": 0.3},
        }

    payload = runtime_inference_payload_from_live_context(LiveCtx())

    assert payload["order_intents"] == [{"ticker": "AAPL", "action": "buy", "quantity": 1}]
    assert payload["scores"] == {"AAPL": 0.8, "MSFT": 0.3}
    rows = {row["ticker"]: row for row in payload["decision_trace"]}
    assert rows["AAPL"]["selected"] is True
    assert rows["AAPL"]["pending_at_broker"] is True
    assert rows["MSFT"]["has_position"] is True
    assert rows["MSFT"]["blocked_by"] == "risk_gate"


def test_runtime_inference_payload_from_live_context_rejects_bad_fields() -> None:
    with pytest.raises(ValueError, match="decision_trace"):
        runtime_inference_payload_from_live_context({
            "config": {"watchlist": ["AAPL"]},
            "market_snapshot": {"as_of": "2026-06-08"},
            "decision_trace": {"ticker": "AAPL"},
        })


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
