"""Decision-trace rows shared by runtime and backtesting.

The trace contract is intentionally plain dictionaries. Storage layers can
persist them to SQLite, JSONL, or LEAN logs without importing broker or model
code.
"""
from __future__ import annotations

import json
from typing import Any, Iterable


def model_type_from_artifact(artifact: dict[str, Any] | None) -> str | None:
    """Infer a stable model-type label from an artifact manifest."""
    if not isinstance(artifact, dict):
        return None
    for key in ("kind", "model_type", "model_family", "backend"):
        value = artifact.get(key)
        if value:
            return str(value)
    metadata = artifact.get("metadata")
    if isinstance(metadata, dict):
        for key in ("kind", "model_type", "model_family", "backend"):
            value = metadata.get(key)
            if value:
                return str(value)
    return None


def active_panel_model_type(config: dict[str, Any] | None, ctx: Any | None = None) -> str | None:
    if ctx is not None:
        value = getattr(ctx, "_active_panel_model_type", None)
        if isinstance(value, str) and value:
            return value
    panel_cfg = (
        ((config or {}).get("ranking", {}) or {})
        .get("panel_scoring", {})
        or {}
    )
    if panel_cfg.get("enabled") is False:
        return None
    return str(panel_cfg.get("kind") or (config or {}).get("panel_ltr", {}).get("backend") or "xgb")


def active_scorer_identity(config: dict[str, Any] | None, ctx: Any | None = None) -> str | None:
    """Identity of the ACTIVE panel scorer, or None when panel scoring is off.

    Unlike :func:`active_panel_model_type` this never falls back to a
    default ``"xgb"`` label, so per-ticker model labels survive for
    strategies that do not run panel scoring (2026-06-07 audit follow-up).
    """
    if ctx is not None:
        value = getattr(ctx, "_active_panel_model_type", None)
        if isinstance(value, str) and value:
            return value
    panel_cfg = (
        ((config or {}).get("ranking", {}) or {})
        .get("panel_scoring", {})
        or {}
    )
    if panel_cfg.get("enabled") is False:
        return None
    kind = panel_cfg.get("kind")
    return str(kind) if kind else None


def build_ticker_daily_state_rows(
    config: dict[str, Any],
    ctx: Any,
    *,
    selected_tickers: Iterable[str] | None = None,
    blocked_map: dict[str, str] | None = None,
    model_types: dict[str, str] | None = None,
    pending_broker_tickers: Iterable[str] | None = None,
    sector_map: dict[str, str] | None = None,
    qp_delta_by_ticker: dict[str, float] | None = None,
    qp_target_by_ticker: dict[str, float] | None = None,
    qp_status: str | None = None,
    extra_tickers: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Build complete per-ticker decision rows for the current runtime state."""
    selected = set(selected_tickers or [])
    pending = set(pending_broker_tickers or [])
    blocked = blocked_map or getattr(ctx, "blocked_by", {}) or {}
    sectors = sector_map or config.get("sector_map") or {}
    scores = getattr(ctx, "scores", {}) or {}
    panel_scores = getattr(ctx, "panel_scores", None) or scores
    rank_scores = getattr(ctx, "rank_scores", None) or scores
    # 2026-06-24: persist the calibrated expected return (mu) that
    # ConvictionGateTask actually floors. Without it the decision history records
    # the raw panel_score but NOT the gated quantity, so a gate change (mu_floor /
    # demean / momentum guard) cannot be validated on real admitted-set outcomes.
    expected_returns = _expected_returns_by_ticker(ctx)
    watchlist = list(config.get("watchlist") or [])
    held = _position_tickers(getattr(ctx, "account_snapshot", {}) or {})
    ticker_order = _stable_ticker_order(
        watchlist, held, selected, blocked, pending, extra_tickers,
    )
    # 2026-06-07 audit follow-up: the active panel scorer is the model that
    # actually selected/ranked this bar — it must win over stale per-ticker
    # labels, which are preserved separately as legacy_model_type.
    active_scorer = active_scorer_identity(config, ctx)
    artifact_model_type = (
        active_scorer
        or model_type_from_artifact(getattr(ctx, "artifact_manifest", None))
        or active_panel_model_type(config, ctx)
    )
    admission = _model_admission_trace(
        getattr(ctx, "_regime_model_admission", None)
        or getattr(ctx, "model_admission", None)
    )
    regime_admission = _runtime_regime_admission_trace(
        getattr(ctx, "_regime_model_admission", None)
    )

    rows: list[dict[str, Any]] = []
    for ticker in ticker_order:
        legacy_model_type = (model_types or {}).get(ticker)
        model_type = active_scorer or legacy_model_type or artifact_model_type
        rows.append(
            {
                "ticker": ticker,
                "as_of": _get_market_value(ctx, "as_of"),
                "regime": _get_value(ctx, "regime"),
                "confidence": _finite_or_none(_get_value(ctx, "confidence")),
                "sector": sectors.get(ticker, "UNKNOWN"),
                "model_type": model_type,
                "active_scorer": active_scorer,
                "legacy_model_type": legacy_model_type,
                "score": _finite_or_none(scores.get(ticker)),
                "panel_score": _finite_or_none(panel_scores.get(ticker)),
                "rank_score": _finite_or_none(rank_scores.get(ticker)),
                "expected_return": _finite_or_none(expected_returns.get(ticker)),
                "blocked_by": blocked.get(ticker),
                "selected": ticker in selected,
                "in_watchlist": ticker in watchlist,
                "has_position": ticker in held,
                "pending_at_broker": ticker in pending,
                "qp_delta": _finite_or_none((qp_delta_by_ticker or {}).get(ticker)),
                "qp_target": _finite_or_none((qp_target_by_ticker or {}).get(ticker)),
                "qp_status": qp_status,
                "model_admission_ok": admission[0],
                "model_admission_reason": admission[1],
                "current_regime_admitted": regime_admission[0],
                "current_regime_admission_reason": regime_admission[1],
                "admitted_regimes": regime_admission[2],
                "blocked_regimes": regime_admission[3],
            }
        )
    return rows


def append_ticker_daily_state_rows(
    config: dict[str, Any],
    ctx: Any,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Append per-ticker state rows to ``ctx.decision_trace`` and return them."""
    rows = build_ticker_daily_state_rows(config, ctx, **kwargs)
    if not hasattr(ctx, "decision_trace") or getattr(ctx, "decision_trace") is None:
        setattr(ctx, "decision_trace", [])
    ctx.decision_trace.extend(rows)
    return rows


def _stable_ticker_order(
    watchlist: Iterable[str],
    held: Iterable[str],
    selected: Iterable[str],
    blocked: dict[str, str],
    pending: Iterable[str],
    extra_tickers: Iterable[str] | None,
) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for source in (watchlist, held, selected, blocked.keys(), pending, extra_tickers or []):
        for ticker in source:
            symbol = str(ticker)
            if symbol not in seen:
                ordered.append(symbol)
                seen.add(symbol)
    return ordered


def _position_tickers(account_snapshot: dict[str, Any]) -> set[str]:
    positions = account_snapshot.get("positions") or {}
    if isinstance(positions, dict):
        return {str(ticker) for ticker, position in positions.items() if position}
    if isinstance(positions, list):
        tickers: set[str] = set()
        for position in positions:
            if isinstance(position, dict):
                ticker = position.get("ticker") or position.get("symbol")
                if ticker:
                    tickers.add(str(ticker))
        return tickers
    return set()


def _get_market_value(ctx: Any, key: str) -> Any:
    market = getattr(ctx, "market_snapshot", {}) or {}
    return market.get(key)


def _get_value(ctx: Any, key: str) -> Any:
    if hasattr(ctx, key):
        return getattr(ctx, key)
    market = getattr(ctx, "market_snapshot", {}) or {}
    return market.get(key)


def _expected_returns_by_ticker(ctx: Any) -> dict[str, Any]:
    """ticker -> calibrated expected return (mu), from the candidates the
    conviction gate floors. Prefers ``ctx.candidates`` (each ``.ticker`` /
    ``.expected_return``); falls back to a pre-built ``ctx.expected_returns``
    mapping. Defensive: never raises, missing -> absent key."""
    explicit = getattr(ctx, "expected_returns", None)
    if isinstance(explicit, dict) and explicit:
        return explicit
    out: dict[str, Any] = {}
    for cand in getattr(ctx, "candidates", None) or []:
        ticker = getattr(cand, "ticker", None)
        if ticker is None and isinstance(cand, dict):
            ticker = cand.get("ticker")
        if ticker is None:
            continue
        er = getattr(cand, "expected_return", None)
        if er is None and isinstance(cand, dict):
            er = cand.get("expected_return")
        out[str(ticker)] = er
    return out


def _finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _model_admission_trace(value: Any) -> tuple[bool | None, str | None]:
    if not isinstance(value, dict):
        return None, None
    ok = value.get("ok")
    ok_bool = ok if isinstance(ok, bool) else None
    reason = value.get("reason")
    return ok_bool, str(reason) if reason else None


def _runtime_regime_admission_trace(
    value: Any,
) -> tuple[bool | None, str | None, str | None, str | None]:
    if not isinstance(value, dict) or "regime" not in value:
        return None, None, None, None
    regime = str(value.get("regime") or "")
    if not regime:
        return None, None, None, None
    ok = value.get("ok")
    admitted = [regime] if ok is True else []
    blocked = [regime] if ok is False else []
    reason = value.get("reason")
    return (
        ok if isinstance(ok, bool) else None,
        str(reason) if reason else None,
        json.dumps(admitted, sort_keys=True),
        json.dumps(blocked, sort_keys=True),
    )
