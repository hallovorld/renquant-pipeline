from __future__ import annotations

import datetime as dt

import pytest

from renquant_pipeline import (
    build_run_bundle,
    hash_jsonable,
    live_state_path,
    resolve_live_state_read,
    runs_db_path,
    validate_feature_contract,
    validate_panel_artifact_contract,
)
from renquant_pipeline.context import (
    InferenceContext as PipelineInferenceContext,
    TickerInferenceContext,
)


def _valid_panel_payload() -> dict:
    return {
        "feature_cols": ["alpha_1", "alpha_2"],
        "trained_date": "2026-05-25",
        "config_fingerprint": "sha256:cfg",
        "panel_shape": {"rows": 1000, "cols": 2},
        "lookahead_days": 5,
        "train_run_id": "run-1",
        "oos_mean_ic": 0.04,
        "oos_std_ic": 0.02,
        "oos_per_fold_ic": [0.03, 0.05, 0.04],
        "cv_method": "purged-walk-forward",
        "cv_embargo_days": 5,
    }


def test_state_paths_are_broker_isolated_and_idempotent(tmp_path) -> None:
    assert live_state_path(tmp_path, "alpaca-paper").name == "live_state.alpaca_paper.json"
    assert runs_db_path(tmp_path / "runs.db", "alpaca").name == "runs.alpaca.db"
    assert runs_db_path(tmp_path / "runs.alpaca.db", "alpaca").name == "runs.alpaca.db"

    with pytest.raises(ValueError, match="Unknown broker_name"):
        live_state_path(tmp_path, "../alpaca")


def test_state_read_uses_legacy_only_as_read_fallback(tmp_path) -> None:
    legacy = tmp_path / "live_state.json"
    legacy.write_text("{}", encoding="utf-8")

    path, is_legacy = resolve_live_state_read(tmp_path, "alpaca")

    assert path == legacy
    assert is_legacy is True


def test_pipeline_context_defaults_are_independent() -> None:
    today = dt.date(2026, 5, 25)
    first = PipelineInferenceContext(config={"watchlist": ["AAPL"]}, today=today)
    second = PipelineInferenceContext(config={"watchlist": ["MSFT"]}, today=today)

    first.candidates.append("AAPL")
    first.counters["candidate"] = 1

    assert first.regime == "BULL_CALM"
    assert first.confidence == pytest.approx(0.5)
    assert second.candidates == []
    assert second.counters == {}


def test_ticker_context_captures_block_reason() -> None:
    tctx = TickerInferenceContext(
        ticker="BAC",
        ohlcv={},
        model={"model_type": "panel_ltr"},
        config={},
        today=dt.date(2026, 5, 25),
        regime="BULL_CALM",
        regime_params={},
        exit_params={},
    )

    tctx.blocked_by = "panel_score_below_buy_floor"

    assert tctx.model_action == "hold"
    assert tctx.blocked_by == "panel_score_below_buy_floor"


def test_panel_artifact_contract_requires_strict_oos_evidence() -> None:
    result = validate_panel_artifact_contract(_valid_panel_payload(), strict=True)

    assert result.ok is True
    assert result.details["n_features"] == 2
    assert result.details["oos_mean_ic"] == pytest.approx(0.04)


def test_panel_artifact_contract_rejects_missing_purged_cv_metadata() -> None:
    payload = _valid_panel_payload()
    payload.pop("cv_embargo_days")

    result = validate_panel_artifact_contract(payload, strict=True)

    assert result.ok is False
    assert "missing cv_embargo_days" in result.errors


def test_panel_artifact_contract_blocks_sentiment_without_runtime_gate() -> None:
    payload = _valid_panel_payload()
    payload["feature_cols"] = ["alpha_1", "mean_sentiment"]
    runtime_config = {
        "ranking": {
            "panel_scoring": {
                "sentiment": {
                    "enabled": True,
                    "regime_policy": {"BULL_CALM": False},
                }
            }
        }
    }

    missing_gate = validate_panel_artifact_contract(
        payload,
        strict=True,
        runtime_config=runtime_config,
    )
    payload["metadata"] = {"sentiment_runtime_gate_contract": "runtime_zeroing"}
    with_gate = validate_panel_artifact_contract(
        payload,
        strict=True,
        runtime_config=runtime_config,
    )

    assert missing_gate.ok is False
    assert any("missing sentiment_runtime_gate_contract" in err for err in missing_gate.errors)
    assert with_gate.ok is True


def test_hash_jsonable_ignores_volatile_runtime_config() -> None:
    left = {"a": 1, "_strategy_dir": "/tmp/one", "nested": {"b": 2}}
    right = {"a": 1, "_strategy_dir": "/tmp/two", "nested": {"b": 2}}

    assert hash_jsonable(left) == hash_jsonable(right)


def test_feature_contract_can_error_or_warn() -> None:
    error_result = validate_feature_contract(["a", "b"], ["a"], policy="error")
    warn_result = validate_feature_contract(["a", "b"], ["a"], policy="warn")

    assert error_result.ok is False
    assert error_result.details["missing"] == ["b"]
    assert warn_result.ok is True
    assert warn_result.warnings == ["missing 1 feature column(s)"]


def test_run_bundle_records_config_artifacts_and_pipeline_flags(tmp_path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    panel_path = artifact_dir / "panel.json"
    panel_path.write_text(
        """
        {
          "feature_cols": ["alpha_1", "alpha_2"],
          "trained_date": "2026-05-25",
          "config_fingerprint": "sha256:cfg",
          "panel_shape": {"rows": 1000, "cols": 2},
          "lookahead_days": 5,
          "train_run_id": "run-1",
          "oos_mean_ic": 0.04,
          "oos_std_ic": 0.02,
          "oos_per_fold_ic": [0.03, 0.05],
          "cv_method": "purged-walk-forward",
          "cv_embargo_days": 5
        }
        """,
        encoding="utf-8",
    )
    ctx = PipelineInferenceContext(
        config={},
        today=dt.date(2026, 5, 25),
        buy_blocked=True,
        bear_only=False,
        regime="BULL_CALM",
        confidence=0.8,
    )
    config = {
        "watchlist": ["MSFT", "AAPL"],
        "ranking": {"panel_scoring": {"artifact_path": str(panel_path)}},
    }

    bundle = build_run_bundle(
        config,
        tmp_path,
        run_id="daily-1",
        run_type="daily_full",
        ctx=ctx,
        broker_mode="alpaca",
    )

    assert bundle["watchlist_size"] == 2
    assert bundle["artifact_hashes"]["panel"].startswith("sha256:")
    assert bundle["panel_contract"]["ok"] is True
    assert bundle["pipeline_flags"]["buy_blocked"] is True
    assert bundle["pipeline_flags"]["confidence"] == pytest.approx(0.8)
