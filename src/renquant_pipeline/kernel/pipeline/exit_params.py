"""Exit-parameter helpers shared by inference and adapter audit logs."""
from __future__ import annotations

import logging
import math
from typing import Any

log = logging.getLogger(__name__)


def _positive_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and out > 0 else None


def _regime_allowed(regime: str | None, allowed: Any) -> bool:
    if not allowed:
        return True
    if isinstance(allowed, str):
        allowed_set = {allowed}
    else:
        allowed_set = {str(x) for x in allowed}
    return regime in allowed_set


def apply_stop_loss_anchor_policy(
    exit_params: dict[str, Any],
    *,
    config: dict[str, Any],
    current_regime: str | None,
    entry_regime: str | None,
    entry_regime_params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Optionally keep cumulative stops no tighter than the entry-regime stop.

    Default ``current_regime`` preserves legacy behavior. ``max_entry_current``
    is an explicit A/B hook for the BULL_CALM thesis: if the entry thesis was a
    calmer momentum regime, do not silently tighten the cumulative stop after a
    later regime re-label unless the current regime's stop is already wider.
    """
    policy = ((config.get("risk") or {}).get("stop_loss_anchor_policy") or {})
    mode = str(policy.get("mode", "current_regime") or "current_regime")
    mode = mode.lower()
    if mode in {"current", "current_regime", "disabled", "off"}:
        return exit_params
    if mode != "max_entry_current":
        raise ValueError(f"unknown risk.stop_loss_anchor_policy.mode={mode!r}")

    if not entry_regime:
        return exit_params
    if not _regime_allowed(entry_regime, policy.get("entry_regimes")):
        return exit_params
    if not _regime_allowed(current_regime, policy.get("current_regimes")):
        return exit_params

    current_stop = _positive_float(exit_params.get("stop_loss_pct"))
    entry_stop = _positive_float((entry_regime_params or {}).get("stop_loss_pct"))
    # BL-3 (2026-06-10): fail SAFE, not closed. This anchor policy is an
    # opt-in A/B hook (mode=max_entry_current). A single holding whose
    # current- or entry-regime config lacks a positive stop_loss_pct must
    # NOT raise — the call sites apply this inside an un-guarded list
    # comprehension over every holding, so one bad config would abort sell
    # evaluation for the WHOLE book and take all risk stops dark. Degrade to
    # the stop we can compute and keep the holding's base stop intact.
    if current_stop is None:
        # No current stop to anchor; nothing to tighten. Leave exit_params
        # untouched so downstream stop logic uses whatever it already had.
        log.warning(
            "stop_loss_anchor_policy: missing current stop_loss_pct "
            "(current_regime=%s entry_regime=%s); skipping anchor, base stop preserved",
            current_regime,
            entry_regime,
        )
        return exit_params
    if entry_stop is None:
        # Entry-regime config has no positive stop; cannot widen toward it.
        # Keep the current stop rather than crashing the whole sell pass.
        log.warning(
            "stop_loss_anchor_policy: missing stop_loss_pct for entry regime %s "
            "(current_regime=%s); keeping current stop %.4f",
            entry_regime,
            current_regime,
            current_stop,
        )
        return exit_params

    anchored_stop = max(current_stop, entry_stop)
    anchor_regime = entry_regime if entry_stop >= current_stop else current_regime
    exit_params["stop_loss_pct"] = anchored_stop
    exit_params["stop_loss_anchor_policy"] = mode
    exit_params["stop_loss_anchor_regime"] = anchor_regime
    exit_params["stop_loss_current_regime"] = current_regime
    exit_params["stop_loss_current_pct"] = current_stop
    exit_params["stop_loss_entry_regime"] = entry_regime
    exit_params["stop_loss_entry_pct"] = entry_stop
    return exit_params


# Single-day-loss config keys anchored as a unit (the SDL gate reads both).
_SDL_ANCHOR_KEYS = ("max_single_day_loss_pct", "sdl_n_sigma")


def apply_single_day_loss_anchor_policy(
    exit_params: dict[str, Any],
    *,
    config: dict[str, Any],
    current_regime: str | None,
    entry_regime: str | None,
    entry_regime_params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Optionally anchor the single-day-loss gate to the ENTRY regime.

    H-1 (2026-06-10 deep audit): the SDL params are read from the CURRENT
    regime, so a BULL_CALM thesis (σ-adaptive ``sdl_n_sigma=3``, no absolute
    cap) re-labeled BULL_VOLATILE inherits that regime's tight absolute 6%
    single-day stop — a whipsaw exit on a position whose thesis never changed.
    The SDL discipline should follow the entry thesis, not a transient relabel.

    Default ``current_regime`` preserves legacy behaviour. ``entry_regime``
    sources the SDL config (``max_single_day_loss_pct``, ``sdl_n_sigma``) from
    the entry-thesis regime so a relabel cannot retighten it. Opt-in.

    Fails SAFE like :func:`apply_stop_loss_anchor_policy` — it runs inside the
    un-guarded per-holding sell comprehension, so a bad config must never raise
    and take the whole book's sell pass dark (the caller also wraps it).
    """
    policy = ((config.get("risk") or {}).get("sdl_anchor_policy") or {})
    mode = str(policy.get("mode", "current_regime") or "current_regime").lower()
    if mode in {"current", "current_regime", "disabled", "off"}:
        return exit_params
    if mode != "entry_regime":
        raise ValueError(f"unknown risk.sdl_anchor_policy.mode={mode!r}")

    if not entry_regime:
        return exit_params
    if not _regime_allowed(entry_regime, policy.get("entry_regimes")):
        return exit_params
    if not _regime_allowed(current_regime, policy.get("current_regimes")):
        return exit_params

    erp = entry_regime_params or {}
    if not isinstance(erp, dict):
        return exit_params
    anchored: dict[str, Any] = {}
    for key in _SDL_ANCHOR_KEYS:
        # Only override keys the entry regime actually defines; absent keys
        # keep the current value (this can only loosen, never invent a stop).
        if key in erp:
            exit_params[key] = erp[key]
            anchored[key] = erp[key]
    if anchored:
        exit_params["sdl_anchor_policy"] = mode
        exit_params["sdl_anchor_regime"] = entry_regime
        exit_params["sdl_current_regime"] = current_regime
    return exit_params
