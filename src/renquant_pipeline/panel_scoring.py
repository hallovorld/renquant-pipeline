"""Panel-score admission tasks for runtime inference.

This module owns the strict runtime contract around panel scores. It does not
train models and it does not import model libraries at module import time.
"""
from __future__ import annotations

import math
from typing import Any

from renquant_artifacts import validate_feature_contract
from renquant_common import Job, Task

from .decision_trace import append_ticker_daily_state_rows
from .order_attribution import stamp_order_attribution
from .xgboost_scorer import load_xgboost_panel_scorer


class LoadScorerTask(Task):
    """Validate that a panel artifact contract exists before scoring."""

    def run(self, ctx: Any) -> bool | None:
        artifact = getattr(ctx, "artifact_manifest", {}) or {}
        if not artifact.get("artifact_id") and not artifact.get("uri"):
            _block_all(ctx, "missing_panel_artifact")
            _trace(ctx)
            return False
        feature_cols = _feature_cols(artifact)
        if not feature_cols:
            _block_all(ctx, "missing_panel_feature_contract")
            _trace(ctx)
            return False
        setattr(ctx, "panel_feature_cols", feature_cols)
        setattr(ctx, "panel_artifact_id", artifact.get("artifact_id"))
        return True


class BuildFeatureMatrixTask(Task):
    """Build the per-ticker runtime matrix and fail closed on missing columns."""

    def run(self, ctx: Any) -> bool | None:
        feature_cols = list(getattr(ctx, "panel_feature_cols", []) or [])
        frame = _feature_frame(ctx)
        matrix: dict[str, dict[str, Any]] = {}
        for ticker in _watchlist(ctx):
            row = frame.get(ticker)
            if not isinstance(row, dict):
                _block(ctx, ticker, "missing_feature_row")
                continue
            result = validate_feature_contract(feature_cols, row.keys(), policy="error")
            if not result.ok:
                missing = ",".join(result.details.get("missing", [])[:5])
                reason = "feature_contract_missing"
                if missing:
                    reason = f"{reason}:{missing}"
                _block(ctx, ticker, reason)
                continue
            matrix[ticker] = {col: row[col] for col in feature_cols}

        setattr(ctx, "panel_feature_matrix", matrix)
        if not matrix:
            setattr(ctx, "buy_blocked", True)
            _trace(ctx)
            return False
        return True


class ApplyScoresTask(Task):
    """Apply explicit panel scores or a declared linear scorer."""

    def run(self, ctx: Any) -> bool | None:
        explicit_scores = _panel_scores_from_snapshot(ctx)
        linear_weights = _linear_weights(ctx)
        intercept = _linear_intercept(ctx)
        scores: dict[str, float] = {}
        matrix = getattr(ctx, "panel_feature_matrix", {}) or {}
        artifact_scorer = None
        scorer_load_error = None
        if not explicit_scores and not linear_weights:
            try:
                artifact_scorer = load_xgboost_panel_scorer(getattr(ctx, "artifact_manifest", {}) or {})
            except Exception as exc:  # noqa: BLE001
                scorer_load_error = str(exc)

        artifact_scores: dict[str, float] = {}
        if artifact_scorer is not None:
            try:
                artifact_scores = artifact_scorer.predict_rows(matrix)
            except Exception as exc:  # noqa: BLE001
                scorer_load_error = str(exc)

        for ticker, row in matrix.items():
            if ticker in explicit_scores:
                score = _finite_float(explicit_scores[ticker])
            elif linear_weights:
                score = _linear_score(row, linear_weights, intercept)
            elif ticker in artifact_scores:
                score = _finite_float(artifact_scores[ticker])
            else:
                score = None
            if score is None:
                reason = "missing_panel_score"
                if scorer_load_error:
                    reason = f"panel_scorer_load_failed:{scorer_load_error[:120]}"
                _block(ctx, ticker, reason)
                continue
            scores[ticker] = score

        if not scores:
            setattr(ctx, "buy_blocked", True)
            _trace(ctx)
            return False
        setattr(ctx, "raw_panel_scores", dict(scores))
        setattr(ctx, "panel_scores", dict(scores))
        ctx.scores.update(scores)
        return True


class ApplyGlobalCalibrationTask(Task):
    """Apply optional global calibration; required calibration fails closed."""

    def run(self, ctx: Any) -> bool | None:
        calibration = _calibration(ctx)
        if not calibration:
            return True
        if calibration.get("required") and not calibration.get("method"):
            _block_all(ctx, "missing_global_calibration")
            _trace(ctx)
            return False

        method = calibration.get("method")
        if not method or method == "identity":
            return True
        calibrated: dict[str, float] = {}
        for ticker, score in (getattr(ctx, "panel_scores", {}) or {}).items():
            value = _apply_calibration(float(score), calibration)
            if value is None:
                _block(ctx, ticker, "invalid_global_calibration")
                continue
            calibrated[ticker] = value
        if not calibrated:
            setattr(ctx, "buy_blocked", True)
            _trace(ctx)
            return False
        setattr(ctx, "panel_scores", dict(calibrated))
        ctx.scores.update(calibrated)
        return True


class RegimeModelAdmissionTask(Task):
    """Gate the whole model when configured evidence floors are not met."""

    def run(self, ctx: Any) -> bool | None:
        cfg = _panel_cfg(ctx).get("model_admission") or {}
        artifact = getattr(ctx, "artifact_manifest", {}) or {}
        metrics = artifact.get("metrics") or {}
        if metrics.get("accepted") is False:
            _block_all(ctx, "model_not_accepted")
            _trace(ctx)
            return False
        if not cfg.get("enabled", False):
            return True

        for field, reason in (
            ("min_oos_mean_ic", "model_oos_ic_below_floor"),
            ("min_wf_sharpe", "model_wf_sharpe_below_floor"),
            ("min_spy_relative_sharpe", "model_spy_relative_sharpe_below_floor"),
        ):
            floor = cfg.get(field)
            if floor is None:
                continue
            metric_name = field.removeprefix("min_")
            value = metrics.get(metric_name) or artifact.get(metric_name)
            numeric = _finite_float(value)
            if numeric is None or numeric < float(floor):
                _block_all(ctx, reason)
                _trace(ctx)
                return False

        if cfg.get("require_config_fingerprint", False):
            if not (artifact.get("config_fingerprint") or metrics.get("config_fingerprint")):
                _block_all(ctx, "missing_config_fingerprint")
                _trace(ctx)
                return False
        return True


class VetoWeakBuysTask(Task):
    """Convert panel scores into buy-eligible candidates without sizing them."""

    def run(self, ctx: Any) -> bool | None:
        floor = _buy_floor(ctx)
        accepted: list[dict[str, Any]] = []
        scores = getattr(ctx, "panel_scores", {}) or {}
        for ticker in _watchlist(ctx):
            score = scores.get(ticker)
            if score is None:
                _block(ctx, ticker, "missing_panel_score")
                continue
            if float(score) < floor:
                _block(ctx, ticker, "panel_score_below_buy_floor")
                continue
            accepted.append(
                {
                    "ticker": ticker,
                    "panel_score": float(score),
                    "rank_score": float(score),
                    "blocked_by": None,
                    "sector": _sector_map(ctx).get(ticker, "UNKNOWN"),
                    "model_type": _model_type(ctx),
                }
            )

        setattr(ctx, "accepted_candidates", accepted)
        _trace(ctx, selected=[row["ticker"] for row in accepted])
        return True


class EmitAttributedOrderIntentsTask(Task):
    """Emit attributed buy intents for already accepted/selected candidates."""

    def run(self, ctx: Any) -> bool | None:
        selected = _selected_candidates(ctx)
        quantities = _order_quantities(ctx)
        if selected and not quantities:
            _block_all(ctx, "missing_order_quantity")
            _trace(ctx)
            return False

        emitted: list[dict[str, Any]] = []
        for candidate in selected:
            ticker = str(candidate["ticker"])
            qty = quantities.get(ticker)
            if qty is None:
                _block(ctx, ticker, "missing_order_quantity")
                continue
            order = {
                "ticker": ticker,
                "action": "buy",
                "quantity": qty,
                "score": candidate.get("rank_score", candidate.get("panel_score")),
            }
            emitted.append(
                stamp_order_attribution(
                    order,
                    ctx,
                    source_job="PanelScoringJob",
                    source_task=type(self).__name__,
                    acceptance_reason="panel_score_admitted",
                    source_obj=candidate,
                    decision_inputs={
                        "buy_floor": _buy_floor(ctx),
                        "raw_panel_score": (getattr(ctx, "raw_panel_scores", {}) or {}).get(ticker),
                    },
                )
            )

        ctx.order_intents.extend(emitted)
        return True


class PanelScoringJob(Job):
    """Strict panel-scoring gate before ranking, QP, or execution."""

    def __init__(self, *, emit_orders: bool = False) -> None:
        tasks: list[Task] = [
            LoadScorerTask(),
            BuildFeatureMatrixTask(),
            ApplyScoresTask(),
            ApplyGlobalCalibrationTask(),
            RegimeModelAdmissionTask(),
            VetoWeakBuysTask(),
        ]
        if emit_orders:
            tasks.append(EmitAttributedOrderIntentsTask())
        self._tasks = tasks

    @property
    def tasks(self) -> list[Task]:
        return self._tasks

    def should_skip(self, ctx: Any) -> bool:
        return not bool(_panel_cfg(ctx).get("enabled", True))


def _trace(ctx: Any, selected: list[str] | None = None) -> None:
    append_ticker_daily_state_rows(
        getattr(ctx, "strategy_config", {}) or {},
        ctx,
        selected_tickers=selected or [],
        blocked_map=getattr(ctx, "blocked_by", {}) or {},
    )


def _block_all(ctx: Any, reason: str) -> None:
    for ticker in _watchlist(ctx):
        _block(ctx, ticker, reason)
    setattr(ctx, "buy_blocked", True)


def _block(ctx: Any, ticker: str, reason: str) -> None:
    if not hasattr(ctx, "blocked_by") or getattr(ctx, "blocked_by") is None:
        setattr(ctx, "blocked_by", {})
    ctx.blocked_by[str(ticker)] = reason


def _feature_cols(artifact: dict[str, Any]) -> list[str]:
    for source in (artifact, artifact.get("metadata") or {}, artifact.get("metrics") or {}):
        if not isinstance(source, dict):
            continue
        for key in ("feature_cols", "feature_columns", "input_feature_cols"):
            value = source.get(key)
            if isinstance(value, list) and value:
                return [str(col) for col in value]
    return []


def _feature_frame(ctx: Any) -> dict[str, dict[str, Any]]:
    market = getattr(ctx, "market_snapshot", {}) or {}
    raw = market.get("feature_frame") or market.get("features") or {}
    if isinstance(raw, dict):
        return {str(ticker): dict(row) for ticker, row in raw.items() if isinstance(row, dict)}
    if isinstance(raw, list):
        frame: dict[str, dict[str, Any]] = {}
        for row in raw:
            if not isinstance(row, dict):
                continue
            ticker = row.get("ticker") or row.get("symbol")
            if ticker:
                frame[str(ticker)] = dict(row)
        return frame
    return {}


def _panel_scores_from_snapshot(ctx: Any) -> dict[str, Any]:
    market = getattr(ctx, "market_snapshot", {}) or {}
    raw = market.get("panel_scores") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items()}


def _linear_weights(ctx: Any) -> dict[str, float]:
    artifact = getattr(ctx, "artifact_manifest", {}) or {}
    for source in (artifact, artifact.get("metadata") or {}):
        weights = source.get("linear_weights") if isinstance(source, dict) else None
        if isinstance(weights, dict) and weights:
            parsed: dict[str, float] = {}
            for key, value in weights.items():
                numeric = _finite_float(value)
                if numeric is not None:
                    parsed[str(key)] = numeric
            return parsed
    return {}


def _linear_intercept(ctx: Any) -> float:
    artifact = getattr(ctx, "artifact_manifest", {}) or {}
    for source in (artifact, artifact.get("metadata") or {}):
        if isinstance(source, dict) and "linear_intercept" in source:
            value = _finite_float(source.get("linear_intercept"))
            return float(value or 0.0)
    return 0.0


def _linear_score(row: dict[str, Any], weights: dict[str, float], intercept: float) -> float | None:
    total = intercept
    for col, weight in weights.items():
        if col not in row:
            return None
        value = _finite_float(row[col])
        if value is None:
            return None
        total += value * weight
    return total


def _calibration(ctx: Any) -> dict[str, Any]:
    artifact = getattr(ctx, "artifact_manifest", {}) or {}
    panel_cfg = _panel_cfg(ctx)
    for source in (
        panel_cfg.get("global_calibration"),
        artifact.get("global_calibration"),
        artifact.get("calibration"),
        (artifact.get("metrics") or {}).get("global_calibration"),
    ):
        if isinstance(source, dict) and source:
            return source
    return {}


def _apply_calibration(score: float, calibration: dict[str, Any]) -> float | None:
    method = calibration.get("method")
    if method == "linear":
        slope = _finite_float(calibration.get("slope", 1.0))
        intercept = _finite_float(calibration.get("intercept", 0.0))
        if slope is None or intercept is None:
            return None
        return intercept + slope * score
    if method in {"platt", "sigmoid", "logistic"}:
        slope = _finite_float(calibration.get("slope", calibration.get("coef", 1.0)))
        intercept = _finite_float(calibration.get("intercept", 0.0))
        if slope is None or intercept is None:
            return None
        z = max(-60.0, min(60.0, intercept + slope * score))
        return 1.0 / (1.0 + math.exp(-z))
    return None


def _selected_candidates(ctx: Any) -> list[dict[str, Any]]:
    raw = getattr(ctx, "selected_candidates", None)
    if raw is None:
        raw = getattr(ctx, "accepted_candidates", []) or []
    selected: list[dict[str, Any]] = []
    for candidate in raw:
        if isinstance(candidate, dict) and candidate.get("ticker"):
            selected.append(candidate)
        elif isinstance(candidate, str):
            selected.append({"ticker": candidate})
    return selected


def _order_quantities(ctx: Any) -> dict[str, Any]:
    market = getattr(ctx, "market_snapshot", {}) or {}
    raw = market.get("order_quantity_by_ticker") or {}
    if isinstance(raw, dict) and raw:
        return {str(k): v for k, v in raw.items()}
    default = (
        (getattr(ctx, "strategy_config", {}) or {})
        .get("execution", {})
        .get("default_quantity")
    )
    if default is None:
        return {}
    return {ticker: default for ticker in _watchlist(ctx)}


def _panel_cfg(ctx: Any) -> dict[str, Any]:
    cfg = getattr(ctx, "strategy_config", {}) or {}
    return (
        cfg.get("ranking", {})
        .get("panel_scoring", {})
    )


def _watchlist(ctx: Any) -> list[str]:
    cfg = getattr(ctx, "strategy_config", {}) or {}
    return [str(ticker) for ticker in (cfg.get("watchlist") or [])]


def _sector_map(ctx: Any) -> dict[str, str]:
    cfg = getattr(ctx, "strategy_config", {}) or {}
    return {str(k): str(v) for k, v in (cfg.get("sector_map") or {}).items()}


def _model_type(ctx: Any) -> str | None:
    artifact = getattr(ctx, "artifact_manifest", {}) or {}
    for key in ("model_type", "model_family", "kind"):
        if artifact.get(key):
            return str(artifact[key])
    return None


def _buy_floor(ctx: Any) -> float:
    cfg = _panel_cfg(ctx)
    value = cfg.get("buy_floor", cfg.get("floor", 0.0))
    parsed = _finite_float(value)
    return float(parsed or 0.0)


def _finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number
