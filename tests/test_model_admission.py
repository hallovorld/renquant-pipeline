from __future__ import annotations

from renquant_artifacts import hash_jsonable

from renquant_pipeline import evaluate_model_admission


def _artifact(metrics: dict | None = None, **extra) -> dict:
    base = {
        "artifact_id": "panel-prod",
        "model_family": "gbdt-panel-ltr",
        "fingerprint": "sha256:model",
        "uri": "object://renquant-artifacts/panel-prod.json",
        "promotion_status": "prod",
        "metrics": {
            "accepted": True,
            "oos_mean_ic": 0.04,
            "wf_sharpe": 1.2,
            "spy_sharpe": 0.7,
            "wf_apy": 0.12,
            "spy_apy": 0.08,
            "config_fingerprint": "sha256:cfg",
            "regime_metrics": {
                "BULL_CALM": {"ic": 0.03, "sharpe": 1.1, "n_obs": 90},
                "BEAR": {"ic": 0.02, "sharpe": 0.8, "n_obs": 50},
            },
            "calibration": {"ece": 0.04, "brier": 0.18, "slope": 0.95},
        },
    }
    if metrics is not None:
        base["metrics"] = metrics
    base.update(extra)
    return base


def _strategy() -> dict:
    sector_map = {"AAPL": "TECH", "MSFT": "TECH"}
    return {
        "watchlist": ["AAPL", "MSFT"],
        "sector_map": sector_map,
        "config_fingerprint": "sha256:cfg",
    }


def _cfg(**overrides) -> dict:
    cfg = {
        "enabled": True,
        "min_oos_mean_ic": 0.01,
        "min_wf_sharpe": 0.0,
        "min_spy_relative_sharpe": 0.1,
        "min_spy_relative_apy": 0.0,
        "require_regime_evidence": True,
        "regimes": "current",
        "min_regime_ic": 0.01,
        "min_regime_obs": 30,
        "require_calibration": True,
        "max_calibration_ece": 0.10,
        "max_brier": 0.25,
        "min_calibration_slope": 0.5,
        "max_calibration_slope": 1.5,
        "require_config_fingerprint": True,
    }
    cfg.update(overrides)
    return cfg


def _eval(artifact=None, cfg=None):
    return evaluate_model_admission(
        strategy_config=_strategy(),
        artifact_manifest=artifact or _artifact(),
        market_snapshot={"regime": "BULL_CALM"},
        admission_config=cfg or _cfg(),
    )


def test_model_admission_passes_full_strict_contract() -> None:
    result = _eval()

    assert result.ok is True
    assert result.details["spy_relative_sharpe"] == 0.5
    assert result.details["regimes_checked"] == ["BULL_CALM"]
    assert result.details["calibration_checked"] is True


def test_model_admission_blocks_when_spy_comparison_missing() -> None:
    metrics = dict(_artifact()["metrics"])
    metrics.pop("spy_sharpe")
    metrics.pop("spy_apy")

    result = _eval(_artifact(metrics=metrics))

    assert result.ok is False
    assert result.reason == "missing_spy_sharpe_comparison"


def test_model_admission_blocks_when_spy_relative_sharpe_loses() -> None:
    metrics = dict(_artifact()["metrics"])
    metrics["spy_sharpe"] = 1.3

    result = _eval(_artifact(metrics=metrics))

    assert result.ok is False
    assert result.reason == "model_spy_relative_sharpe_below_floor"


def test_model_admission_blocks_missing_current_regime_evidence() -> None:
    metrics = dict(_artifact()["metrics"])
    metrics["regime_metrics"] = {"BEAR": {"ic": 0.02, "n_obs": 40}}

    result = _eval(_artifact(metrics=metrics))

    assert result.ok is False
    assert result.reason == "missing_regime_evidence:BULL_CALM"


def test_model_admission_blocks_bad_calibration() -> None:
    metrics = dict(_artifact()["metrics"])
    metrics["calibration"] = {"ece": 0.4, "brier": 0.18, "slope": 1.0}

    result = _eval(_artifact(metrics=metrics))

    assert result.ok is False
    assert result.reason == "calibration_ece_above_floor"


def test_model_admission_blocks_config_fingerprint_mismatch() -> None:
    result = _eval(_artifact(config_fingerprint="sha256:other"))

    assert result.ok is False
    assert result.reason == "config_fingerprint_mismatch"


def test_model_admission_blocks_sector_fingerprint_mismatch() -> None:
    artifact = _artifact(sector_fingerprint="sha256:old")
    result = evaluate_model_admission(
        strategy_config=_strategy(),
        artifact_manifest=artifact,
        market_snapshot={"regime": "BULL_CALM"},
        admission_config=_cfg(require_sector_fingerprint=True),
    )

    assert result.ok is False
    assert result.reason == "sector_fingerprint_mismatch"


def test_model_admission_accepts_matching_sector_fingerprint() -> None:
    strategy = _strategy()
    artifact = _artifact(sector_fingerprint=hash_jsonable(strategy["sector_map"]))

    result = evaluate_model_admission(
        strategy_config=strategy,
        artifact_manifest=artifact,
        market_snapshot={"regime": "BULL_CALM"},
        admission_config=_cfg(require_sector_fingerprint=True),
    )

    assert result.ok is True
