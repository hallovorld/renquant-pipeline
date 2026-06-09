"""Runtime inference-pipeline contract.

The current 104 implementation is ported behind these stages in reviewed
slices. This module pins the top-level contract first so execution and
backtesting can share the same runtime flow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from renquant_common import Job, Pipeline, Task
from renquant_artifacts import validate_artifact_manifest
from renquant_base_data import validate_data_manifest

from .decision_trace import build_ticker_daily_state_rows


@dataclass
class InferenceContext:
    strategy_config: dict[str, Any]
    data_manifest: dict[str, Any]
    artifact_manifest: dict[str, Any]
    market_snapshot: dict[str, Any]
    account_snapshot: dict[str, Any] = field(default_factory=dict)
    decision_trace: list[dict[str, Any]] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    order_intents: list[dict[str, Any]] = field(default_factory=list)
    blocked_by: dict[str, str] = field(default_factory=dict)
    buy_blocked: bool = False


class ValidateRuntimeInputsTask(Task):
    """Require auditable config/artifact/market inputs before scoring."""

    def run(self, ctx: InferenceContext) -> bool | None:
        if not ctx.strategy_config.get("watchlist"):
            raise ValueError("strategy_config missing watchlist")
        validate_data_manifest(ctx.data_manifest)
        validate_artifact_manifest(ctx.artifact_manifest)
        if not ctx.market_snapshot.get("as_of"):
            raise ValueError("market_snapshot missing as_of")
        return True


class RuntimeStageTask(Task):
    """Adapter task for dependency-injected runtime stages."""

    def __init__(self, name: str, fn) -> None:
        self._name = name
        self.fn = fn

    @property
    def name(self) -> str:
        return self._name

    def run(self, ctx: InferenceContext) -> bool | None:
        return self.fn(ctx)


class RuntimeInferenceJob(Job):
    def __init__(self, stages: list[Task]) -> None:
        self._tasks = [ValidateRuntimeInputsTask(), *stages]

    @property
    def tasks(self) -> list[Task]:
        return self._tasks


class RuntimeInferencePipeline(Pipeline):
    """Top-level runtime pipeline shared by live, shadow, sim, and LEAN."""

    def __init__(self, stages: list[Task]) -> None:
        super().__init__([RuntimeInferenceJob(stages)], name="runtime-inference")


def runtime_inference_payload(ctx: InferenceContext) -> dict[str, Any]:
    """Return the JSON payload consumed by native live-bundle tooling."""
    return {
        "schema_version": 1,
        "source": "renquant_pipeline.runtime_inference",
        "market_as_of": ctx.market_snapshot.get("as_of"),
        "decision_trace": list(ctx.decision_trace),
        "order_intents": list(ctx.order_intents),
        "scores": dict(ctx.scores),
        "blocked_by": dict(ctx.blocked_by),
        "buy_blocked": bool(ctx.buy_blocked),
    }


def _get_field(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _ctx_object(ctx: Any) -> Any:
    if isinstance(ctx, dict):
        return SimpleNamespace(**ctx)
    return ctx


def _dict_field(value: Any, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"live context field must be a dict: {field_name}")
    return dict(value)


def _list_of_dicts(value: Any, *, field_name: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"live context field must be a list: {field_name}")
    rows: list[dict[str, Any]] = []
    for idx, row in enumerate(value):
        if not isinstance(row, dict):
            raise ValueError(f"live context field {field_name}[{idx}] must be an object")
        rows.append(dict(row))
    return rows


def _ticker(row: Any) -> str | None:
    if isinstance(row, dict):
        value = row.get("ticker") or row.get("symbol")
    else:
        value = getattr(row, "ticker", None) or getattr(row, "symbol", None)
    return str(value) if value else None


def _score(row: Any) -> float | None:
    keys = ("rank_score", "panel_score", "score")
    for key in keys:
        value = row.get(key) if isinstance(row, dict) else getattr(row, key, None)
        if isinstance(value, int | float):
            return float(value)
    return None


def _scores_from_live_context(ctx: Any) -> dict[str, float]:
    scores = _get_field(ctx, "scores")
    if isinstance(scores, dict):
        return {
            str(ticker): float(score)
            for ticker, score in scores.items()
            if isinstance(score, int | float)
        }
    score_snapshot = _get_field(ctx, "_ticker_score_snapshot", "ticker_score_snapshot")
    if isinstance(score_snapshot, dict):
        parsed: dict[str, float] = {}
        for ticker, row in score_snapshot.items():
            if isinstance(row, int | float):
                parsed[str(ticker)] = float(row)
                continue
            score = _score(row)
            if score is not None:
                parsed[str(ticker)] = score
        if parsed:
            return parsed
    parsed: dict[str, float] = {}
    for field_name in ("candidates", "ranked", "ranked_candidates"):
        rows = _get_field(ctx, field_name)
        if not isinstance(rows, list):
            continue
        for row in rows:
            ticker = _ticker(row)
            score = _score(row)
            if ticker and score is not None:
                parsed[ticker] = score
    return parsed


def _market_as_of(ctx: Any, market_snapshot: dict[str, Any]) -> Any:
    if market_snapshot.get("as_of"):
        return market_snapshot["as_of"]
    today = _get_field(ctx, "today")
    if hasattr(today, "isoformat"):
        return today.isoformat()
    return today


def runtime_inference_payload_from_live_context(
    ctx: Any,
    *,
    strategy_config: dict[str, Any] | None = None,
    market_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract a parity-ready inference payload from a live-like runtime context.

    This adapter only reads the supplied context. It does not run scoring,
    submit orders, connect to a broker, or mutate persistent state.
    """
    ctx_obj = _ctx_object(ctx)
    config = strategy_config or _get_field(ctx, "strategy_config", "config", default={}) or {}
    if not isinstance(config, dict):
        raise ValueError("live context strategy_config/config must be a dict")
    market = market_snapshot or _get_field(ctx, "market_snapshot", default={}) or {}
    if not isinstance(market, dict):
        raise ValueError("live context market_snapshot must be a dict")

    explicit_trace = _get_field(ctx, "decision_trace")
    if explicit_trace is None:
        order_intents = _list_of_dicts(
            _get_field(ctx, "order_intents", "orders"),
            field_name="order_intents/orders",
        )
        blocked_by = _dict_field(
            _get_field(ctx, "blocked_by", "_blocked_by_ticker"),
            field_name="blocked_by",
        )
        scores = _scores_from_live_context(ctx)
        if not hasattr(ctx_obj, "scores"):
            setattr(ctx_obj, "scores", scores)
        if market_snapshot is not None or not hasattr(ctx_obj, "market_snapshot"):
            setattr(ctx_obj, "market_snapshot", market)
        decision_trace = build_ticker_daily_state_rows(
            config,
            ctx_obj,
            selected_tickers=[ticker for row in order_intents if (ticker := _ticker(row))],
            blocked_map=blocked_by,
            pending_broker_tickers=_get_field(
                ctx, "pending_broker_tickers", default=[],
            ) or [],
            extra_tickers=scores.keys(),
        )
    else:
        decision_trace = _list_of_dicts(explicit_trace, field_name="decision_trace")
        order_intents = _list_of_dicts(
            _get_field(ctx, "order_intents", "orders"),
            field_name="order_intents/orders",
        )
        blocked_by = _dict_field(
            _get_field(ctx, "blocked_by", "_blocked_by_ticker"),
            field_name="blocked_by",
        )
        scores = _scores_from_live_context(ctx)

    return {
        "schema_version": 1,
        "source": "renquant_pipeline.live_context_inference",
        "market_as_of": _market_as_of(ctx, market),
        "decision_trace": decision_trace,
        "order_intents": order_intents,
        "scores": scores,
        "blocked_by": blocked_by,
        "buy_blocked": bool(_get_field(ctx, "buy_blocked", default=False)),
    }


def write_runtime_inference_payload_from_live_context(
    ctx: Any,
    path: str | Path,
    *,
    strategy_config: dict[str, Any] | None = None,
    market_snapshot: dict[str, Any] | None = None,
) -> Path:
    """Write a live-context runtime inference payload as deterministic JSON."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = runtime_inference_payload_from_live_context(
        ctx,
        strategy_config=strategy_config,
        market_snapshot=market_snapshot,
    )
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def write_runtime_inference_payload(ctx: InferenceContext, path: str | Path) -> Path:
    """Write the runtime inference payload as deterministic JSON."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(runtime_inference_payload(ctx), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out
