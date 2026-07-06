"""Panel-score admission tasks for runtime inference.

This module owns the strict runtime contract around panel scores. It does not
train models and it does not import model libraries at module import time.
Scorers are resolved through ``renquant_common.load_scorer`` against entry
points registered by ``renquant-model`` (per RFC §"Cross-Repo Contracts →
Scorer Protocol").
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from renquant_artifacts import validate_feature_contract
from renquant_common import (
    ArtifactManifest,
    Job,
    OOSEvidence,
    ScorerKindNotRegistered,
    Task,
    load_scorer,
)

from .decision_trace import append_ticker_daily_state_rows
from .model_admission import evaluate_model_admission
from .order_attribution import stamp_order_attribution
from .kernel.gate_registry import ctx_registry
from .runtime_features import build_runtime_feature_frame


def _legacy_dict_to_manifest(legacy: dict[str, Any]) -> ArtifactManifest | None:
    """Bridge legacy artifact dicts → ArtifactManifest for load_scorer.

    Producers in the umbrella still emit loose dicts (``uri`` /
    ``model_family`` / ``local_artifact_path`` / no ``oos_evidence``).
    Until those producers migrate to writing real :class:`ArtifactManifest`
    instances (during the umbrella code lift), this shim translates and
    fills missing fields with sentinel defaults. The strict contract is
    preserved on the producer side; only consumers wear the synthesis cost.
    """
    if not legacy:
        return None
    kind = legacy.get("kind")
    if not kind:
        return None
    artifact_uri = legacy.get("artifact_uri") or legacy.get("uri") or ""
    local = legacy.get("local_artifact_path")
    if local:
        artifact_uri = f"file://{local}"
    if not artifact_uri:
        return None
    return ArtifactManifest(
        kind=str(kind),
        family=str(
            legacy.get("family")
            or legacy.get("model_family")
            or "unknown"
        ),
        artifact_uri=str(artifact_uri),
        feature_fingerprint=str(
            legacy.get("feature_fingerprint")
            or legacy.get("fingerprint")
            or "legacy:unknown"
        ),
        config_fingerprint=str(
            legacy.get("config_fingerprint") or "legacy:unknown"
        ),
        training_data_fingerprint=str(
            legacy.get("training_data_fingerprint") or "legacy:unknown"
        ),
        trained_at=legacy.get("trained_at")
        or datetime(1970, 1, 1, tzinfo=timezone.utc),
        lookahead_days=int(legacy.get("lookahead_days") or 1),
        oos_evidence=OOSEvidence(
            mean_ic=float(legacy.get("oos_mean_ic") or 0.0),
            std_ic=float(legacy.get("oos_std_ic") or 0.0),
            per_fold_ic=tuple(legacy.get("oos_per_fold_ic") or ()),
            cv_method=str(legacy.get("cv_method") or "unknown"),
            embargo_days=int(legacy.get("cv_embargo_days") or 0),
        ),
        calibrator_uri=legacy.get("calibrator_uri"),
        owner_repo=str(legacy.get("owner_repo") or "umbrella-legacy"),
    )


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
        try:
            frame = _feature_frame(ctx, feature_cols)
        except Exception as exc:  # noqa: BLE001
            _block_all(ctx, f"feature_transform_failed:{str(exc)[:120]}")
            _trace(ctx)
            return False
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
            ctx_registry(ctx).submit(
                gate="panel_feature_matrix", scope="book", verdict="block",
                reason="no ticker produced a complete feature row",
                inputs={"watchlist_size": len(_watchlist(ctx))})
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
                manifest = _legacy_dict_to_manifest(
                    getattr(ctx, "artifact_manifest", {}) or {}
                )
                if manifest is not None:
                    artifact_scorer = load_scorer(manifest)
            except ScorerKindNotRegistered as exc:
                scorer_load_error = f"scorer_not_registered:{exc}"
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
            ctx_registry(ctx).submit(
                gate="panel_scores", scope="book", verdict="block",
                reason="no ticker received a panel score",
                inputs={"scorer_load_error": str(scorer_load_error or "")[:120]})
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
            ctx_registry(ctx).submit(
                gate="global_calibration", scope="book", verdict="block",
                reason="every score invalid after calibration",
                inputs={"method": str(method)})
            _trace(ctx)
            return False
        setattr(ctx, "panel_scores", dict(calibrated))
        ctx.scores.update(calibrated)
        return True


class RegimeModelAdmissionTask(Task):
    """Gate the whole model when configured evidence floors are not met."""

    def run(self, ctx: Any) -> bool | None:
        cfg = _panel_cfg(ctx).get("model_admission") or {}
        result = evaluate_model_admission(
            strategy_config=getattr(ctx, "strategy_config", {}) or {},
            artifact_manifest=getattr(ctx, "artifact_manifest", {}) or {},
            market_snapshot=getattr(ctx, "market_snapshot", {}) or {},
            admission_config=cfg,
        )
        setattr(ctx, "model_admission", {
            "ok": result.ok,
            "reason": result.reason,
            "details": result.details,
        })
        if not result.ok:
            _block_all(ctx, result.reason or "model_admission_failed")
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

    def run(self, ctx: Any) -> None:
        """Run the chain, then apply the registry aggregate ONCE.

        Errata-C choke point (eng plan S2-PR4), mirroring
        kernel/pipeline/job_gates.BuyGatesJob: tasks submit verdicts
        instead of writing ``buy_blocked``; the max-join aggregate is
        applied at the job boundary, before any downstream consumer
        (ranking / QP / order emit) reads the flag. Task False-returns
        still short-circuit the chain — independent mechanisms.
        """
        super().run(ctx)
        registry = getattr(ctx, "gate_registry", None)
        if registry is not None and registry.blocked("book"):
            setattr(ctx, "buy_blocked", True)

    def should_skip(self, ctx: Any) -> bool:
        return not bool(_panel_cfg(ctx).get("enabled", True))


class _StubFrozenFeatureMatrixTask(Task):
    """Injects an empty feature matrix keyed by the frozen class-A scores.

    Stands in for ``LoadScorerTask`` + ``BuildFeatureMatrixTask`` so
    :class:`FrozenScoreScoringJob` never has to build real runtime features —
    it exists purely to unblock the per-tick feature-availability gate for
    the diagnostic probe described there. See that class's docstring for the
    semantic-validity caveats this stub carries.
    """

    def run(self, ctx: Any) -> bool | None:
        market = getattr(ctx, "market_snapshot", {}) or {}
        scores = market.get("panel_scores") or {}
        matrix = {str(t): {} for t in scores}
        setattr(ctx, "panel_feature_cols", [])
        setattr(ctx, "panel_feature_matrix", matrix)
        setattr(ctx, "panel_artifact_id", "frozen-daily-signal")
        cfg = getattr(ctx, "strategy_config", {}) or {}
        exe = cfg.setdefault("execution", {})
        if exe.get("default_quantity") is None:
            exe["default_quantity"] = 1
        return True


class FrozenScoreScoringJob(PanelScoringJob):
    """PanelScoringJob replacement that skips feature build and uses
    pre-computed frozen scores from the class-A signal.

    Inherits ``run()``/``should_skip()`` from :class:`PanelScoringJob`
    unchanged — the ``buy_blocked`` choke point (errata-C(iii)) has exactly
    one designated writer in this module, pinned by
    ``tests/test_gate_writers_panel_scoring.py::TestCensusPin`` — only
    ``__init__`` is overridden, to swap ``LoadScorerTask`` +
    ``BuildFeatureMatrixTask`` for the frozen-score stub below.

    DIAGNOSTIC / DEBUG PROBE ONLY — not a validated intent-generation
    design. ``_StubFrozenFeatureMatrixTask`` injects an EMPTY feature
    matrix and sets ``default_quantity=1`` as a FALLBACK purely to unblock
    the pipeline from its usual per-tick feature-availability gate. There
    is no real sizing control: order sizing (``_order_quantities`` in this
    module) still prefers ``market_snapshot["order_quantity_by_ticker"]``
    (class B / session-start) when a caller supplies it — the fallback
    quantity of 1 fires only when that map is absent, which is true of the
    one real caller today (renquant-orchestrator's session scheduler builds
    no real-time quantity map at all) but is not an intrinsic property of
    this job; a future caller that DOES populate real quantities would get
    real sizing through, unreviewed. There is no proof that bypassing the
    feature contract this way preserves the pipeline's semantics, and no
    exit/sell path at all. Intended only to be driven through
    :func:`renquant_pipeline.intraday_decisioning
    .run_frozen_score_diagnostic_tick`, which confines it to a shadow-only
    caller (no submit path exists there) — but it must not be treated as,
    or built on top of, a defensible paper/live execution design until
    those gaps are closed.
    """

    def __init__(self, *, emit_orders: bool = False) -> None:  # noqa: super-not-called
        tasks: list[Task] = [
            _StubFrozenFeatureMatrixTask(),
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
    ctx_registry(ctx).submit(
        gate="panel_scoring", scope="book", verdict="block",
        reason=str(reason),
        inputs={"watchlist_size": len(_watchlist(ctx))})


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


def _feature_frame(ctx: Any, feature_cols: list[str]) -> dict[str, dict[str, Any]]:
    return build_runtime_feature_frame(
        getattr(ctx, "market_snapshot", {}) or {},
        getattr(ctx, "artifact_manifest", {}) or {},
        feature_cols,
        panel_config=_panel_cfg(ctx),
    )


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
    for key in ("kind", "model_type", "model_family"):
        if artifact.get(key):
            return str(artifact[key])
    panel_kind = _panel_cfg(ctx).get("kind")
    if panel_kind:
        return str(panel_kind)
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
