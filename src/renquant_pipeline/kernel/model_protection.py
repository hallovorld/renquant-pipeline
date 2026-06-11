"""Model-based position protection — thesis-aware meta-label exit (foundation).

Design RFC: renquant-orchestrator/doc/research/2026-06-11-construction-and-
model-based-protection.md.

A holding is protected not by a price-only stop (which sells winners on noise —
the NVTS/ORCL evidence) but by RE-EVALUATING the model's calibrated expected
return (μ) for it. While μ stays above an exit threshold the thesis holds and
the position is kept (a price drop the model still likes is opportunity, not
danger). Once μ breaches the threshold on N CONSECUTIVE evaluations the thesis
is judged broken and the position is exited. This is the meta-labeling idea
(López de Prado, *Advances in Financial Machine Learning*) with sequential
debouncing (CUSUM — Page 1954; SPRT — Wald 1945) to avoid acting on a single
noisy reading.

SCOPE OF THIS MODULE: the PURE, dependency-free core only — the breach state
machine and the exit decision. The integration that feeds it (provisional-bar
re-scoring of each holding on the latest price, the evaluation cadence,
end-of-day execution, and the PDT / wash-sale governors described in the RFC)
is layered on top and is NOT wired to live execution here. Everything is
DEFAULT OFF; nothing in this module changes behaviour until a caller both
enables it and routes exits through ``evaluate``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# Decisions returned by ``evaluate``.
ACTION_HOLD = "hold"            # thesis intact (or cannot be judged) — keep
ACTION_BREACH = "hold_breach"   # breached this eval, but below the strike count
ACTION_EXIT = "exit"            # N consecutive breaches — thesis broken, exit


@dataclass(frozen=True)
class ProtectionConfig:
    """Resolved model-protection parameters."""
    enabled: bool = False
    # τ: a breach occurs when the re-scored calibrated μ is <= this threshold.
    # 0.0 means "exit once the model is no longer net-positive on the name"
    # (μ>0 ⇔ raw>neutral_raw, consistent with the buy-side signal gate).
    exit_mu_threshold: float = 0.0
    # Consecutive breaches required before exiting (sequential debounce).
    n_strikes: int = 3


@dataclass(frozen=True)
class ProtectionState:
    """Per-holding protection state. Immutable; ``evaluate`` returns the next.

    ``consecutive_breaches`` resets to 0 the moment the thesis re-asserts
    (μ back above τ), so a single recovering reading clears the strike count —
    a CUSUM-style reset, not a leaky bucket.
    """
    consecutive_breaches: int = 0


def protection_config_from(config: dict[str, Any] | None) -> ProtectionConfig:
    """Read ``risk.model_protection`` (all keys optional; default OFF)."""
    mp = ((config or {}).get("risk", {}) or {}).get("model_protection", {}) or {}
    try:
        thr = float(mp.get("exit_mu_threshold", 0.0))
    except (TypeError, ValueError):
        thr = 0.0
    try:
        n = int(mp.get("n_strikes", 3))
    except (TypeError, ValueError):
        n = 3
    return ProtectionConfig(
        enabled=bool(mp.get("enabled", False)),
        exit_mu_threshold=thr if math.isfinite(thr) else 0.0,
        n_strikes=max(1, n),
    )


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def evaluate(
    mu: Any,
    cfg: ProtectionConfig,
    state: ProtectionState,
) -> tuple[str, ProtectionState, str]:
    """Decide protection action from the holding's freshly re-scored μ.

    ``mu`` is the calibrated expected return for the holding recomputed on the
    latest price (the caller owns that re-scoring). Returns
    ``(action, next_state, reason)``:

    - disabled, or μ unavailable → ``ACTION_HOLD`` (never exit on a missing
      read; an absent thesis-revaluation must not look like a broken thesis).
    - μ <= τ and the (now incremented) breach count reaches ``n_strikes`` →
      ``ACTION_EXIT`` (count reset on the returned state).
    - μ <= τ but below the strike count → ``ACTION_BREACH``.
    - μ > τ → ``ACTION_HOLD`` with the breach count reset to 0.
    """
    if not cfg.enabled:
        return ACTION_HOLD, state, ""
    m = _finite(mu)
    if m is None:
        return ACTION_HOLD, state, "mu_unavailable"

    if m <= cfg.exit_mu_threshold:
        n = state.consecutive_breaches + 1
        if n >= cfg.n_strikes:
            return (
                ACTION_EXIT,
                ProtectionState(0),
                f"thesis_breached mu={m:+.4f}<=tau={cfg.exit_mu_threshold:+.4f} "
                f"strikes={n}/{cfg.n_strikes}",
            )
        return (
            ACTION_BREACH,
            ProtectionState(n),
            f"thesis_breach {n}/{cfg.n_strikes} mu={m:+.4f}",
        )
    # Thesis re-asserts → clear the strike count.
    return ACTION_HOLD, ProtectionState(0), ""


__all__ = [
    "ACTION_HOLD",
    "ACTION_BREACH",
    "ACTION_EXIT",
    "ProtectionConfig",
    "ProtectionState",
    "protection_config_from",
    "evaluate",
]
