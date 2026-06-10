"""Order-intent attribution contract.

Runtime pipelines produce intents; execution repos decide whether and how to
submit them. Every intent must therefore carry enough attribution to explain
why it exists before it leaves the pipeline boundary.
"""
from __future__ import annotations

from typing import Any

from .decision_trace import (
    active_panel_model_type,
    active_scorer_identity,
    model_type_from_artifact,
)


ATTRIBUTION_VERSION = "order_attribution_v1"


def score_snapshot(
    order: dict[str, Any],
    ctx: Any,
    *,
    source_obj: Any | None = None,
) -> dict[str, Any]:
    """Capture model, sector, score, and block context for an order intent."""
    ticker = str(order.get("ticker") or order.get("symbol") or "")
    config = getattr(ctx, "strategy_config", {}) or {}
    sectors = config.get("sector_map") or {}
    scores = getattr(ctx, "scores", {}) or {}
    panel_scores = getattr(ctx, "panel_scores", None) or scores
    rank_scores = getattr(ctx, "rank_scores", None) or scores
    blocked = getattr(ctx, "blocked_by", {}) or {}
    artifact = getattr(ctx, "artifact_manifest", {}) or {}

    # 2026-06-07 audit follow-up: the ACTIVE panel scorer (e.g. hf_patchtst)
    # is the model that selected this intent — it must win over the stale
    # per-ticker label, which is preserved as legacy_model_type.
    active_scorer = active_scorer_identity(config, ctx)
    artifact_model_type = model_type_from_artifact(artifact)
    legacy_model_type = (
        _attr(source_obj, "legacy_model_type")
        or _attr(source_obj, "model_type")
    )
    return {
        "ticker": ticker,
        "model_type": active_scorer
        or legacy_model_type
        or artifact_model_type
        or active_panel_model_type(config, ctx),
        "active_scorer": active_scorer,
        "legacy_model_type": legacy_model_type,
        "sector": sectors.get(ticker, "UNKNOWN"),
        "score": _finite_or_none(scores.get(ticker)),
        "panel_score": _finite_or_none(panel_scores.get(ticker)),
        "rank_score": _finite_or_none(rank_scores.get(ticker)),
        "blocked_by": blocked.get(ticker),
        "artifact_fingerprint": artifact.get("fingerprint"),
        "artifact_id": artifact.get("artifact_id"),
        "config_fingerprint": artifact.get("config_fingerprint")
        or (artifact.get("metrics") or {}).get("config_fingerprint"),
    }


def stamp_order_attribution(
    order: dict[str, Any],
    ctx: Any,
    *,
    source_job: str,
    source_task: str,
    acceptance_reason: str,
    source_obj: Any | None = None,
    decision_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach a validated attribution payload to an order intent."""
    stamped = dict(order)
    stamped["attribution"] = {
        "version": ATTRIBUTION_VERSION,
        "source_job": source_job,
        "source_task": source_task,
        "acceptance_reason": acceptance_reason,
        "score_snapshot": score_snapshot(stamped, ctx, source_obj=source_obj),
        "decision_inputs": dict(decision_inputs or {}),
    }
    validate_order_attribution(stamped)
    return stamped


def validate_order_attribution(order: dict[str, Any]) -> dict[str, Any]:
    """Raise when an order intent is missing attribution required for audit."""
    errors: list[str] = []
    if not order.get("ticker") and not order.get("symbol"):
        errors.append("order missing ticker")
    if not order.get("action"):
        errors.append("order missing action")
    if order.get("quantity") is None:
        errors.append("order missing quantity")

    attribution = order.get("attribution")
    if not isinstance(attribution, dict):
        errors.append("order missing attribution")
    else:
        for key in ("version", "source_job", "source_task", "acceptance_reason"):
            if not attribution.get(key):
                errors.append(f"attribution missing {key}")
        if attribution.get("version") != ATTRIBUTION_VERSION:
            errors.append("attribution version mismatch")
        snapshot = attribution.get("score_snapshot")
        if not isinstance(snapshot, dict):
            errors.append("attribution missing score_snapshot")
        elif not snapshot.get("ticker"):
            errors.append("score_snapshot missing ticker")

    if errors:
        raise ValueError("; ".join(errors))
    return order


def _attr(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


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
