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


@dataclass(frozen=True)
class LiveContextSnapshot:
    """Normalized readonly view of a live-like context.

    This is the smallest contract needed to lift RunnerAdapter.make_context
    callers toward native code without copying the umbrella adapter wholesale.
    """

    strategy_config: dict[str, Any]
    market_snapshot: dict[str, Any]
    account_snapshot: dict[str, Any]
    market_as_of: Any
    decision_trace: list[dict[str, Any]]
    order_intents: list[dict[str, Any]]
    scores: dict[str, float]
    blocked_by: dict[str, Any]
    pending_broker_tickers: list[str]
    buy_blocked: bool

    def to_runtime_payload(self) -> dict[str, Any]:
        """Return the native inference payload consumed by live-run tooling."""
        return {
            "schema_version": 1,
            "source": "renquant_pipeline.live_context_inference",
            "market_as_of": self.market_as_of,
            "market_snapshot": dict(self.market_snapshot),
            "account_snapshot": dict(self.account_snapshot),
            "decision_trace": list(self.decision_trace),
            "order_intents": list(self.order_intents),
            "scores": dict(self.scores),
            "blocked_by": dict(self.blocked_by),
            "pending_broker_tickers": list(self.pending_broker_tickers),
            "buy_blocked": self.buy_blocked,
        }


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


def _trace_context(
    ctx: Any,
    *,
    scores: dict[str, float],
    market_snapshot: dict[str, Any],
    account_snapshot: dict[str, Any],
) -> Any:
    source = _ctx_object(ctx)
    values = dict(vars(source)) if hasattr(source, "__dict__") else {}
    for field_name in (
        "artifact_manifest",
        "panel_scores",
        "rank_scores",
        "regime",
        "confidence",
        "_active_panel_model_type",
        "_regime_model_admission",
        "model_admission",
    ):
        value = _get_field(source, field_name)
        if value is not None:
            values.setdefault(field_name, value)
    values.update({
        "scores": scores,
        "market_snapshot": market_snapshot,
        "account_snapshot": account_snapshot,
    })
    return SimpleNamespace(**values)


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


def _list_of_strings(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list | tuple | set):
        raise ValueError(f"live context field must be a list: {field_name}")
    return [str(item) for item in value if item]


def _holding_value(position: Any, *names: str) -> Any:
    for name in names:
        if isinstance(position, dict) and name in position:
            return position[name]
        if hasattr(position, name):
            return getattr(position, name)
    return None


def _holding_snapshot(
    ticker: str,
    position: Any,
    *,
    price: Any = None,
) -> dict[str, Any] | None:
    if isinstance(position, dict):
        row = dict(position)
    else:
        row = {}
        for source_key, output_key in (
            ("quantity", "quantity"),
            ("qty", "quantity"),
            ("shares", "quantity"),
            ("market_value", "market_value"),
            ("avg_entry_price", "avg_entry_price"),
            ("cost_basis", "cost_basis"),
        ):
            value = _holding_value(position, source_key)
            if value is not None and output_key not in row:
                row[output_key] = value
    quantity = row.get("quantity", row.get("qty", row.get("shares")))
    if quantity is not None:
        try:
            if float(quantity) == 0.0:
                return None
        except (TypeError, ValueError):
            pass
        row["quantity"] = quantity
        row.pop("qty", None)
        row.pop("shares", None)
    if price is not None and "price" not in row:
        row["price"] = price
    row.setdefault("ticker", ticker)
    return row


def _account_snapshot_from_live_context(ctx: Any) -> dict[str, Any]:
    explicit = _get_field(ctx, "account_snapshot")
    if explicit is not None:
        return _dict_field(explicit, field_name="account_snapshot")
    holdings = _get_field(ctx, "holdings")
    if not isinstance(holdings, dict):
        return {}
    prices = _get_field(ctx, "prices", default={}) or {}
    if not isinstance(prices, dict):
        prices = {}
    positions: dict[str, dict[str, Any]] = {}
    for raw_ticker, position in holdings.items():
        ticker = str(raw_ticker)
        row = _holding_snapshot(ticker, position, price=prices.get(ticker))
        if row:
            positions[ticker] = row
    snapshot: dict[str, Any] = {"positions": positions}
    for field_name in ("cash", "portfolio_value"):
        value = _get_field(ctx, field_name)
        if value is not None:
            snapshot[field_name] = value
    return snapshot


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
    for score_field in ("scores", "panel_scores", "rank_scores"):
        scores = _get_field(ctx, score_field)
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


def live_context_snapshot_from_live_context(
    ctx: Any,
    *,
    strategy_config: dict[str, Any] | None = None,
    market_snapshot: dict[str, Any] | None = None,
) -> LiveContextSnapshot:
    """Extract a normalized readonly snapshot from a live-like runtime context.

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
    account_snapshot = _account_snapshot_from_live_context(ctx)
    pending_broker_tickers = _list_of_strings(
        _get_field(ctx, "pending_broker_tickers", default=[]),
        field_name="pending_broker_tickers",
    )

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
        decision_trace = build_ticker_daily_state_rows(
            config,
            _trace_context(
                ctx_obj,
                scores=scores,
                market_snapshot=market,
                account_snapshot=account_snapshot,
            ),
            selected_tickers=[ticker for row in order_intents if (ticker := _ticker(row))],
            blocked_map=blocked_by,
            pending_broker_tickers=pending_broker_tickers,
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

    return LiveContextSnapshot(
        strategy_config=dict(config),
        market_snapshot=dict(market),
        account_snapshot=account_snapshot,
        market_as_of=_market_as_of(ctx, market),
        decision_trace=decision_trace,
        order_intents=order_intents,
        scores=scores,
        blocked_by=blocked_by,
        pending_broker_tickers=pending_broker_tickers,
        buy_blocked=bool(_get_field(ctx, "buy_blocked", default=False)),
    )


def runtime_inference_payload_from_live_context(
    ctx: Any,
    *,
    strategy_config: dict[str, Any] | None = None,
    market_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract a parity-ready inference payload from a live-like runtime context."""
    return live_context_snapshot_from_live_context(
        ctx,
        strategy_config=strategy_config,
        market_snapshot=market_snapshot,
    ).to_runtime_payload()


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
