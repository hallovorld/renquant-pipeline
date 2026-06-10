"""Order-attribution contract for buy emitters.

Every buy order emitted into ``ctx.orders`` must answer:

  * which Job/Task owned the decision?
  * which score state did it see at emit time?
  * why did it pass the final hurdle?

This is intentionally lightweight and dict-based because adapters already
consume order dicts. The invariant is enforced at emission time by
``validate_order_attribution`` and by source-level tests.
"""
from __future__ import annotations

import math
from typing import Any


ATTRIBUTION_VERSION = "order_attribution_v1"


def _finite_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _pick(order: dict, obj: Any, key: str) -> Any:
    if key in order:
        return order.get(key)
    return getattr(obj, key, None) if obj is not None else None


def _legacy_model_type_for(ctx: Any, ticker: str | None) -> str | None:
    """Stale per-ticker artifact label (XGB-era); kept for audit continuity."""
    if not ticker or ctx is None:
        return None
    try:
        from renquant_pipeline.kernel.decision_trace import (  # noqa: PLC0415
            model_type_from_artifact,
        )
    except Exception:
        return None
    models = getattr(ctx, "models", None) or {}
    return model_type_from_artifact(models.get(ticker))


def _model_attribution_for(
    ctx: Any,
    legacy_model_type: str | None,
) -> dict[str, str | None]:
    """Active-scorer-first model identity (2026-06-07 audit follow-up)."""
    try:
        from renquant_pipeline.kernel.decision_trace import (  # noqa: PLC0415
            active_panel_model_type,
            resolve_model_attribution,
        )
    except Exception:
        return {
            "model_type": legacy_model_type,
            "active_scorer": None,
            "legacy_model_type": legacy_model_type,
        }
    config = getattr(ctx, "config", None) if ctx is not None else None
    ident = resolve_model_attribution(
        config, ctx, legacy_model_type=legacy_model_type,
    )
    if ident["model_type"] is None:
        ident["model_type"] = active_panel_model_type(config, ctx)
    return ident


def _sector_for(ctx: Any, ticker: str | None) -> str | None:
    if not ticker or ctx is None:
        return None
    sector_map = (getattr(ctx, "config", None) or {}).get("sector_map", {}) or {}
    value = sector_map.get(ticker) or sector_map.get(str(ticker).upper())
    return value if isinstance(value, str) and value else None


def _blocked_by_for(ctx: Any, ticker: str | None) -> str | None:
    if not ticker or ctx is None:
        return None
    blocked = getattr(ctx, "_blocked_by_ticker", None) or {}
    value = blocked.get(ticker) or blocked.get(str(ticker).upper())
    return value if isinstance(value, str) and value else None


def score_snapshot(order: dict, *, source_obj: Any = None, ctx: Any = None) -> dict[str, Any]:
    """Capture the model/risk score state visible when an order is emitted."""
    ticker = order.get("ticker") or getattr(source_obj, "ticker", None)
    model_ident = _model_attribution_for(
        ctx,
        _pick(order, source_obj, "legacy_model_type")
        or _pick(order, source_obj, "model_type")
        or _legacy_model_type_for(ctx, ticker),
    )
    return {
        "rank_score": _finite_or_none(_pick(order, source_obj, "rank_score")),
        "panel_score": _finite_or_none(_pick(order, source_obj, "panel_score")),
        "rs_score": _finite_or_none(_pick(order, source_obj, "rs_score")),
        "mu": _finite_or_none(_pick(order, source_obj, "mu")),
        "sigma": _finite_or_none(_pick(order, source_obj, "sigma")),
        "kelly_target_pct": _finite_or_none(
            _pick(order, source_obj, "kelly_target_pct")
        ),
        "expected_return": _finite_or_none(
            _pick(order, source_obj, "expected_return")
        ),
        "expected_return_horizon_days": _pick(
            order, source_obj, "expected_return_horizon_days",
        ),
        "mu_horizon_days": _pick(order, source_obj, "mu_horizon_days"),
        "confidence": _finite_or_none(order.get("confidence", getattr(ctx, "confidence", None))),
        "regime": order.get("regime", getattr(ctx, "regime", None)),
        # Active scorer first (2026-06-07 audit): when panel scoring selects,
        # rows must say e.g. `hf_patchtst`, not a stale per-ticker label.
        "model_type": model_ident["model_type"],
        "active_scorer": model_ident["active_scorer"],
        "legacy_model_type": model_ident["legacy_model_type"],
        "sector": _pick(order, source_obj, "sector") or _sector_for(ctx, ticker),
        "blocked_by": _pick(order, source_obj, "blocked_by") or _blocked_by_for(ctx, ticker),
    }


def stamp_order_attribution(
    order: dict,
    *,
    ctx: Any,
    source_job: str,
    source_task: str,
    acceptance_reason: str,
    source_obj: Any = None,
    decision_inputs: dict[str, Any] | None = None,
) -> dict:
    """Stamp required attribution fields and validate the order contract."""
    if not acceptance_reason:
        raise ValueError("order attribution requires non-empty acceptance_reason")
    order_type = str(order.get("order_type") or "")
    if not order_type:
        raise ValueError("order attribution requires order_type")
    order_source = f"{source_job}.{source_task}"
    merged_inputs = dict(decision_inputs or {})
    merged_inputs.setdefault("acceptance_reason", acceptance_reason)
    merged_inputs.setdefault("order_type", order_type)
    merged_inputs.setdefault("source_job", source_job)
    merged_inputs.setdefault("source_task", source_task)
    snap = score_snapshot(order, source_obj=source_obj, ctx=ctx)
    for key in ("model_type", "sector", "blocked_by", "active_scorer", "legacy_model_type"):
        if snap.get(key) is not None:
            order.setdefault(key, snap.get(key))
    if snap.get("active_scorer"):
        # The selecting model wins even when the emitter pre-stamped a stale
        # per-ticker model_type (preserved above as legacy_model_type).
        order["model_type"] = snap["model_type"]
    order.update({
        "attribution_version": ATTRIBUTION_VERSION,
        "source_job": source_job,
        "source_task": source_task,
        "order_source": order_source,
        "source": order.get("source") or order_source,
        "score_snapshot": snap,
        "decision_inputs": merged_inputs,
    })
    validate_order_attribution(order)
    return order


def validate_order_attribution(order: dict) -> None:
    required = [
        "ticker",
        "order_type",
        "attribution_version",
        "source_job",
        "source_task",
        "order_source",
        "score_snapshot",
        "decision_inputs",
    ]
    missing = [key for key in required if key not in order]
    if missing:
        raise ValueError(f"order attribution missing fields: {missing}")
    if order["attribution_version"] != ATTRIBUTION_VERSION:
        raise ValueError("unknown order attribution version")
    if not isinstance(order["score_snapshot"], dict):
        raise ValueError("order score_snapshot must be a dict")
    if not isinstance(order["decision_inputs"], dict):
        raise ValueError("order decision_inputs must be a dict")
    if not order["decision_inputs"].get("acceptance_reason"):
        raise ValueError("order decision_inputs.acceptance_reason is required")


__all__ = [
    "ATTRIBUTION_VERSION",
    "score_snapshot",
    "stamp_order_attribution",
    "validate_order_attribution",
]
