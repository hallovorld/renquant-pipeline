"""Strict model-admission checks for runtime buy eligibility."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from renquant_artifacts import hash_jsonable


@dataclass(frozen=True)
class ModelAdmissionResult:
    ok: bool
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def evaluate_model_admission(
    *,
    strategy_config: dict[str, Any],
    artifact_manifest: dict[str, Any],
    market_snapshot: dict[str, Any],
    admission_config: dict[str, Any] | None = None,
) -> ModelAdmissionResult:
    """Evaluate whether the model artifact can feed buy/QP paths."""
    cfg = admission_config or {}
    artifact = artifact_manifest or {}
    metrics = artifact.get("metrics") if isinstance(artifact.get("metrics"), dict) else {}

    if metrics.get("accepted") is False:
        return _reject("model_not_accepted")
    if not cfg.get("enabled", False):
        return ModelAdmissionResult(ok=True, details={"enabled": False})

    checks = (
        _check_oos_ic(cfg, artifact, metrics),
        _check_wf_sharpe(cfg, artifact, metrics),
        _check_spy_relative(cfg, artifact, metrics),
        _check_regime_evidence(cfg, artifact, metrics, market_snapshot),
        _check_calibration(cfg, artifact, metrics),
        _check_config_fingerprint(cfg, strategy_config, artifact, metrics),
        _check_sector_fingerprint(cfg, strategy_config, artifact, metrics),
    )
    details: dict[str, Any] = {"enabled": True}
    for result in checks:
        details.update(result.details)
        if not result.ok:
            return ModelAdmissionResult(ok=False, reason=result.reason, details=details)
    return ModelAdmissionResult(ok=True, details=details)


def _check_oos_ic(
    cfg: dict[str, Any],
    artifact: dict[str, Any],
    metrics: dict[str, Any],
) -> ModelAdmissionResult:
    floor = cfg.get("min_oos_mean_ic")
    if floor is None:
        return _ok()
    value = _metric("oos_mean_ic", artifact, metrics)
    if value is None:
        return _reject("missing_oos_mean_ic")
    return _floor(value, floor, "model_oos_ic_below_floor", "oos_mean_ic")


def _check_wf_sharpe(
    cfg: dict[str, Any],
    artifact: dict[str, Any],
    metrics: dict[str, Any],
) -> ModelAdmissionResult:
    floor = cfg.get("min_wf_sharpe")
    if floor is None:
        return _ok()
    value = _first_metric(("wf_sharpe", "walkforward_sharpe", "mean_wf_sharpe"), artifact, metrics)
    if value is None:
        return _reject("missing_wf_sharpe")
    return _floor(value, floor, "model_wf_sharpe_below_floor", "wf_sharpe")


def _check_spy_relative(
    cfg: dict[str, Any],
    artifact: dict[str, Any],
    metrics: dict[str, Any],
) -> ModelAdmissionResult:
    sharpe_floor = cfg.get("min_spy_relative_sharpe")
    apy_floor = cfg.get("min_spy_relative_apy")
    if sharpe_floor is None and apy_floor is None:
        return _ok()
    details: dict[str, Any] = {}
    if sharpe_floor is not None:
        relative = _first_metric(("spy_relative_sharpe", "sharpe_vs_spy"), artifact, metrics)
        if relative is None:
            wf = _first_metric(("wf_sharpe", "walkforward_sharpe", "mean_wf_sharpe"), artifact, metrics)
            spy = _first_metric(("spy_sharpe", "benchmark_sharpe"), artifact, metrics)
            if wf is None or spy is None:
                return _reject("missing_spy_sharpe_comparison")
            relative = wf - spy
        details["spy_relative_sharpe"] = relative
        if relative < float(sharpe_floor):
            return _reject("model_spy_relative_sharpe_below_floor", **details)
    if apy_floor is not None:
        relative_apy = _first_metric(("spy_relative_apy", "apy_vs_spy"), artifact, metrics)
        if relative_apy is None:
            apy = _first_metric(("wf_apy", "apy", "annual_return"), artifact, metrics)
            spy_apy = _first_metric(("spy_apy", "benchmark_apy"), artifact, metrics)
            if apy is None or spy_apy is None:
                return _reject("missing_spy_apy_comparison", **details)
            relative_apy = apy - spy_apy
        details["spy_relative_apy"] = relative_apy
        if relative_apy < float(apy_floor):
            return _reject("model_spy_relative_apy_below_floor", **details)
    return ModelAdmissionResult(ok=True, details=details)


def _check_regime_evidence(
    cfg: dict[str, Any],
    artifact: dict[str, Any],
    metrics: dict[str, Any],
    market_snapshot: dict[str, Any],
) -> ModelAdmissionResult:
    if not cfg.get("require_regime_evidence", False):
        return _ok()
    regime_metrics = _regime_metrics(artifact, metrics)
    if not regime_metrics:
        return _reject("missing_regime_evidence")
    regimes = cfg.get("regimes")
    if regimes == "current" or regimes is None:
        current = market_snapshot.get("regime") or cfg.get("current_regime")
        if not current:
            return _reject("missing_current_regime")
        regimes_to_check = [str(current)]
    else:
        regimes_to_check = [str(regime) for regime in regimes]
    min_ic = cfg.get("min_regime_ic")
    min_sharpe = cfg.get("min_regime_sharpe")
    min_obs = cfg.get("min_regime_obs")
    for regime in regimes_to_check:
        row = regime_metrics.get(regime)
        if not isinstance(row, dict):
            return _reject(f"missing_regime_evidence:{regime}")
        if min_obs is not None:
            obs = _numeric(row.get("n_obs", row.get("count")))
            if obs is None or obs < float(min_obs):
                return _reject(f"regime_obs_below_floor:{regime}", regime=regime, n_obs=obs)
        if min_ic is not None:
            ic = _numeric(row.get("ic", row.get("mean_ic", row.get("rank_ic"))))
            if ic is None or ic < float(min_ic):
                return _reject(f"regime_ic_below_floor:{regime}", regime=regime, ic=ic)
        if min_sharpe is not None:
            sharpe = _numeric(row.get("sharpe", row.get("wf_sharpe")))
            if sharpe is None or sharpe < float(min_sharpe):
                return _reject(f"regime_sharpe_below_floor:{regime}", regime=regime, sharpe=sharpe)
    return ModelAdmissionResult(ok=True, details={"regimes_checked": regimes_to_check})


def _check_calibration(
    cfg: dict[str, Any],
    artifact: dict[str, Any],
    metrics: dict[str, Any],
) -> ModelAdmissionResult:
    if not cfg.get("require_calibration", False):
        return _ok()
    calibration = _calibration_payload(artifact, metrics)
    if not calibration:
        return _reject("missing_calibration_evidence")
    max_ece = cfg.get("max_calibration_ece")
    if max_ece is not None:
        ece = _numeric(calibration.get("ece", calibration.get("expected_calibration_error")))
        if ece is None or ece > float(max_ece):
            return _reject("calibration_ece_above_floor", calibration_ece=ece)
    max_brier = cfg.get("max_brier")
    if max_brier is not None:
        brier = _numeric(calibration.get("brier", calibration.get("brier_score")))
        if brier is None or brier > float(max_brier):
            return _reject("calibration_brier_above_floor", brier=brier)
    min_slope = cfg.get("min_calibration_slope")
    max_slope = cfg.get("max_calibration_slope")
    if min_slope is not None or max_slope is not None:
        slope = _numeric(calibration.get("slope"))
        if slope is None:
            return _reject("missing_calibration_slope")
        if min_slope is not None and slope < float(min_slope):
            return _reject("calibration_slope_below_floor", slope=slope)
        if max_slope is not None and slope > float(max_slope):
            return _reject("calibration_slope_above_floor", slope=slope)
    return ModelAdmissionResult(ok=True, details={"calibration_checked": True})


def _check_config_fingerprint(
    cfg: dict[str, Any],
    strategy_config: dict[str, Any],
    artifact: dict[str, Any],
    metrics: dict[str, Any],
) -> ModelAdmissionResult:
    if not cfg.get("require_config_fingerprint", False):
        return _ok()
    actual = artifact.get("config_fingerprint") or metrics.get("config_fingerprint")
    if not actual:
        return _reject("missing_config_fingerprint")
    expected = cfg.get("expected_config_fingerprint") or strategy_config.get("config_fingerprint")
    if expected is not None and str(actual) != str(expected):
        return _reject("config_fingerprint_mismatch", expected=expected, actual=actual)
    return ModelAdmissionResult(ok=True, details={"config_fingerprint": actual})


def _check_sector_fingerprint(
    cfg: dict[str, Any],
    strategy_config: dict[str, Any],
    artifact: dict[str, Any],
    metrics: dict[str, Any],
) -> ModelAdmissionResult:
    if not cfg.get("require_sector_fingerprint", False):
        return _ok()
    actual = (
        artifact.get("sector_fingerprint")
        or artifact.get("sector_map_fingerprint")
        or metrics.get("sector_fingerprint")
        or metrics.get("sector_map_fingerprint")
    )
    if not actual:
        return _reject("missing_sector_fingerprint")
    expected = cfg.get("expected_sector_fingerprint") or hash_jsonable(strategy_config.get("sector_map") or {})
    if str(actual) != str(expected):
        return _reject("sector_fingerprint_mismatch", expected=expected, actual=actual)
    return ModelAdmissionResult(ok=True, details={"sector_fingerprint": actual})


def _regime_metrics(artifact: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    for source in (metrics, artifact):
        for key in ("regime_metrics", "regime_evidence", "regime_ic"):
            value = source.get(key)
            if isinstance(value, dict):
                return value
    return {}


def _calibration_payload(artifact: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    for source in (metrics, artifact):
        for key in ("calibration", "calibration_metrics", "global_calibration"):
            value = source.get(key)
            if isinstance(value, dict):
                return value
    return {}


def _metric(key: str, artifact: dict[str, Any], metrics: dict[str, Any]) -> float | None:
    return _first_metric((key,), artifact, metrics)


def _first_metric(
    keys: tuple[str, ...],
    artifact: dict[str, Any],
    metrics: dict[str, Any],
) -> float | None:
    for source in (metrics, artifact):
        for key in keys:
            value = _numeric(source.get(key))
            if value is not None:
                return value
    return None


def _floor(value: float, floor: Any, reason: str, detail_key: str) -> ModelAdmissionResult:
    details = {detail_key: value}
    if value < float(floor):
        return _reject(reason, **details)
    return ModelAdmissionResult(ok=True, details=details)


def _numeric(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _ok() -> ModelAdmissionResult:
    return ModelAdmissionResult(ok=True)


def _reject(reason: str, **details: Any) -> ModelAdmissionResult:
    return ModelAdmissionResult(ok=False, reason=reason, details=details)
