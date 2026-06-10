"""Shared decision-trace helpers for sim, live runner, and LEAN.

Adapters own execution side effects, but decision rows should not be
hand-built three different ways. This module centralizes the audit surface
used by `candidate_scores` and `ticker_daily_state`.
"""
from __future__ import annotations

import json
from typing import Any


def model_type_from_artifact(model: Any) -> str | None:
    """Extract a readable model type from dict/object artifacts."""
    if model is None:
        return None
    if isinstance(model, dict):
        meta = model.get("_metadata") or model.get("metadata") or {}
        for src in (meta, model):
            if not isinstance(src, dict):
                continue
            for key in ("best_approach", "model_type", "policy_type", "type", "kind"):
                val = src.get(key)
                if isinstance(val, str) and val:
                    return val
        return None
    meta = getattr(model, "metadata", None)
    if isinstance(meta, dict):
        for key in ("best_approach", "model_type", "policy_type", "type", "kind"):
            val = meta.get(key)
            if isinstance(val, str) and val:
                return val
    val = getattr(model, "model_type", None)
    return val if isinstance(val, str) and val else None


def model_types_from_models(models: dict[str, Any] | None) -> dict[str, str | None]:
    return {
        tk: model_type_from_artifact(model)
        for tk, model in (models or {}).items()
    }


def active_panel_model_type(config: dict | None, ctx: Any | None = None) -> str | None:
    """Return the active panel scorer family used for buy-side ranking."""
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
    kind = panel_cfg.get("kind") or (config or {}).get("panel_ltr", {}).get("backend")
    return str(kind or "xgb")


def active_scorer_identity(config: dict | None, ctx: Any | None = None) -> str | None:
    """Identity of the ACTIVE panel scorer, or None when panel scoring is off.

    Unlike :func:`active_panel_model_type` (which defaults to ``"xgb"`` as a
    last-resort fallback), this returns None when no panel scorer is
    configured, so per-ticker model labels are not overridden for
    non-panel strategies.
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


def resolve_model_attribution(
    config: dict | None,
    ctx: Any | None = None,
    *,
    legacy_model_type: str | None = None,
) -> dict[str, str | None]:
    """Resolve attribution identity for emitted candidate/trade/trace rows.

    Audit fix (2026-06-07 prod/shadow status audit): ``model_type`` must
    carry the ACTIVE panel scorer identity (e.g. ``hf_patchtst``) when panel
    scoring selects/ranks, instead of silently inheriting the stale
    per-ticker XGB-era label. The per-ticker label is preserved separately
    as ``legacy_model_type`` so existing per-ticker analytics keep working.
    """
    active = active_scorer_identity(config, ctx)
    legacy = (
        legacy_model_type
        if isinstance(legacy_model_type, str) and legacy_model_type
        else None
    )
    return {
        "model_type": active or legacy,
        "active_scorer": active,
        "legacy_model_type": legacy,
    }


def selected_buy_tickers(trade_events: list[dict[str, Any]] | None) -> set[str]:
    """Return tickers with buy trade/order events."""
    return {
        str(event.get("ticker"))
        for event in (trade_events or [])
        if str(event.get("action") or "").lower() == "buy" and event.get("ticker")
    }


def trade_event_tickers(trade_events: list[dict[str, Any]] | None) -> set[str]:
    """Return all tickers that appear in executed or attempted trade rows."""
    return {
        str(event.get("ticker"))
        for event in (trade_events or [])
        if event.get("ticker")
    }


def trade_event_blocked_map(
    trade_events: list[dict[str, Any]] | None,
) -> dict[str, str]:
    """Return per-ticker blocked reasons carried by attempted trade rows."""
    out: dict[str, str] = {}
    filled_actions = {"buy", "sell", "short_open", "short_cover"}
    for event in trade_events or []:
        ticker = event.get("ticker")
        action = str(event.get("action") or "").lower()
        blocked = event.get("blocked_by")
        if ticker and blocked and action not in filled_actions:
            out[str(ticker)] = str(blocked)
    return out


def candidate_trace_pool(ctx: Any) -> list[Any]:
    """Full candidate pool for trace persistence, including filtered candidates."""
    base = list(
        getattr(ctx, "_full_candidate_snapshot", None)
        or getattr(ctx, "candidates", None)
        or []
    )
    seen = {id(c) for c in base}
    for cand in list(getattr(ctx, "short_candidates", None) or []):
        if id(cand) in seen:
            continue
        base.append(cand)
        seen.add(id(cand))
    return base


def candidate_score_excluded_holding_tickers(config: dict) -> set[str]:
    """Holdings that should not be persisted as alpha candidate-score rows."""
    from renquant_pipeline.kernel.pipeline.task_benchmark_sleeve import (  # noqa: PLC0415
        benchmark_sleeve_ticker,
        exclude_benchmark_sleeve_from_alpha,
    )

    ticker = (
        benchmark_sleeve_ticker(config)
        if exclude_benchmark_sleeve_from_alpha(config) else None
    )
    return {ticker} if ticker else set()


def qp_trace_maps(ctx: Any) -> tuple[dict[str, float], dict[str, float], str | None]:
    """Extract per-ticker QP delta/target/status from the shared QP solution."""
    qp_delta_by_ticker: dict[str, float] = {}
    qp_target_by_ticker: dict[str, float] = {}
    qp_status = None
    qp_sol = getattr(ctx, "_qp_solution", None)
    qp_tickers = list(getattr(ctx, "_qp_tickers", None) or [])
    if qp_sol is not None and qp_tickers:
        qp_status = getattr(qp_sol, "status", None)
        for idx, tk in enumerate(qp_tickers):
            try:
                qp_delta_by_ticker[tk] = float(qp_sol.delta_w[idx])
            except Exception:
                pass
            try:
                qp_target_by_ticker[tk] = float(qp_sol.target_w[idx])
            except Exception:
                pass
    return qp_delta_by_ticker, qp_target_by_ticker, qp_status


def _score_value(src: Any, snap: dict[str, Any], name: str) -> Any:
    value = getattr(src, name, None) if src is not None else None
    return value if value is not None else snap.get(name)


def _sector_for(ticker: str, sector_map: dict[str, str]) -> str | None:
    value = sector_map.get(ticker)
    if isinstance(value, str) and value:
        return value
    upper = str(ticker).upper()
    value = sector_map.get(upper)
    return value if isinstance(value, str) and value else None


def build_ticker_daily_state_rows(
    *,
    config: dict,
    ctx: Any,
    selected_tickers: set[str],
    blocked_map: dict[str, str] | None,
    model_types: dict[str, str | None],
    universe_rejections: dict[str, str] | None = None,
    model_keys: set[str] | None = None,
    pending_broker_tickers: set[str] | None = None,
    portfolio_value: float | None = None,
    sector_map: dict[str, str] | None = None,
    qp_delta_by_ticker: dict[str, float] | None = None,
    qp_target_by_ticker: dict[str, float] | None = None,
    qp_status: str | None = None,
    extra_tickers: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Build one `ticker_daily_state` row per decision-trace ticker.

    This is shared by sim/live/LEAN so blocked reasons, score snapshots, QP
    fields, and pending-broker semantics cannot drift adapter-by-adapter.
    """
    from renquant_pipeline.kernel.pipeline.task_benchmark_sleeve import decision_trace_tickers  # noqa: PLC0415

    blocked_map = blocked_map or {}
    sector_map = sector_map if sector_map is not None else (config.get("sector_map", {}) or {})
    universe_rejections = universe_rejections or {}
    pending_broker_tickers = pending_broker_tickers or set()
    qp_delta_by_ticker = qp_delta_by_ticker or {}
    qp_target_by_ticker = qp_target_by_ticker or {}
    model_keys = model_keys if model_keys is not None else set(model_types)

    cand_pool = candidate_trace_pool(ctx)
    cand_by_t = {c.ticker: c for c in cand_pool}
    score_snapshots = getattr(ctx, "_ticker_score_snapshot", {}) or {}
    prices = getattr(ctx, "prices", {}) or {}
    holdings = getattr(ctx, "holdings", {}) or {}
    admission = _model_admission_trace(
        getattr(ctx, "_regime_model_admission", None)
        or getattr(ctx, "model_admission", None)
    )
    regime_admission = _runtime_regime_admission_trace(
        getattr(ctx, "_regime_model_admission", None)
    )
    watchlist_set = set(config.get("watchlist", []) or [])
    trace_tickers = list(decision_trace_tickers(config))
    seen_trace = set(trace_tickers)
    for tk in sorted(
        set(extra_tickers or set())
        | set(selected_tickers or set())
        | set(pending_broker_tickers or set())
        | set(blocked_map or {})
    ):
        if tk not in seen_trace:
            trace_tickers.append(tk)
            seen_trace.add(tk)
    pf_value = (
        float(portfolio_value)
        if portfolio_value is not None
        else float(getattr(ctx, "portfolio_value", 0.0) or 0.0)
    )

    rows: list[dict[str, Any]] = []
    for tk in trace_tickers:
        hs = holdings.get(tk)
        cand = cand_by_t.get(tk)
        src = cand if cand is not None else hs
        snap = score_snapshots.get(tk, {}) or {}
        has_pos = 1 if hs is not None else 0
        pos_qty = float(getattr(hs, "shares", 0.0)) if hs else None
        px = prices.get(tk, 0.0)
        pos_pct = None
        if hs and pf_value > 0 and px:
            pos_pct = (pos_qty * px) / pf_value

        blocked_str = blocked_map.get(tk)
        if blocked_str is None and tk not in model_keys:
            reason = universe_rejections.get(tk, "not_loaded")
            blocked_str = f"universe:{reason}"
        if blocked_str is None and tk in pending_broker_tickers:
            blocked_str = "broker_pending"
        if blocked_str is None and cand is None and hs is not None:
            blocked_str = "held_no_new_buy"
        if blocked_str is None and cand is None:
            blocked_str = "no_model_signal"
        if blocked_str is None and tk not in selected_tickers:
            blocked_str = "not_selected"

        if cand is not None:
            model_action = "buy"
        elif hs is not None and getattr(hs, "sell_streak", 0) > 0:
            model_action = "sell"
        else:
            model_action = snap.get("model_action", "hold")

        model_ident = resolve_model_attribution(
            config,
            ctx,
            legacy_model_type=(
                model_types.get(tk)
                or getattr(src, "legacy_model_type", None)
                or getattr(src, "model_type", None)
                or snap.get("model_type")
            ),
        )
        rows.append({
            "ticker": tk,
            "regime": getattr(ctx, "regime", None),
            "confidence": getattr(ctx, "confidence", None),
            "in_watchlist": 1 if tk in watchlist_set else 0,
            "in_universe": 1 if tk in model_keys else 0,
            "pending_at_broker": 1 if tk in pending_broker_tickers else 0,
            "has_position": has_pos,
            "position_qty": pos_qty,
            "position_pct": pos_pct,
            "model_type": (
                model_ident["model_type"]
                or active_panel_model_type(config, ctx)
            ),
            "active_scorer": model_ident["active_scorer"],
            "legacy_model_type": model_ident["legacy_model_type"],
            "model_action": model_action,
            "sell_streak": int(getattr(hs, "sell_streak", 0)) if hs else None,
            "panel_score": _score_value(src, snap, "panel_score"),
            "rank_score": _score_value(src, snap, "rank_score"),
            "expected_return": _score_value(src, snap, "expected_return"),
            "expected_return_horizon_days": _score_value(
                src, snap, "expected_return_horizon_days",
            ),
            "kelly_target_pct": _score_value(src, snap, "kelly_target_pct"),
            "mu": _score_value(src, snap, "mu"),
            "mu_horizon_days": _score_value(src, snap, "mu_horizon_days"),
            "sigma": _score_value(src, snap, "sigma"),
            "in_candidates": 1 if cand is not None else 0,
            "selected": 1 if tk in selected_tickers else 0,
            "blocked_by": blocked_str,
            "sector": _sector_for(tk, sector_map),
            "qp_delta_w": qp_delta_by_ticker.get(tk),
            "qp_target_w": qp_target_by_ticker.get(tk),
            "qp_status": qp_status,
            "model_admission_ok": admission[0],
            "model_admission_reason": admission[1],
            "current_regime_admitted": regime_admission[0],
            "current_regime_admission_reason": regime_admission[1],
            "admitted_regimes": regime_admission[2],
            "blocked_regimes": regime_admission[3],
        })
    return rows


def _model_admission_trace(value: Any) -> tuple[int | None, str | None]:
    if not isinstance(value, dict):
        return None, None
    ok = value.get("ok")
    ok_int = int(ok) if isinstance(ok, bool) else None
    reason = value.get("reason")
    return ok_int, str(reason) if reason else None


def _runtime_regime_admission_trace(
    value: Any,
) -> tuple[int | None, str | None, str | None, str | None]:
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
        int(ok) if isinstance(ok, bool) else None,
        str(reason) if reason else None,
        json.dumps(admitted, sort_keys=True),
        json.dumps(blocked, sort_keys=True),
    )


__all__ = [
    "build_ticker_daily_state_rows",
    "active_panel_model_type",
    "active_scorer_identity",
    "resolve_model_attribution",
    "candidate_score_excluded_holding_tickers",
    "candidate_trace_pool",
    "model_type_from_artifact",
    "model_types_from_models",
    "qp_trace_maps",
    "selected_buy_tickers",
    "trade_event_blocked_map",
    "trade_event_tickers",
]
