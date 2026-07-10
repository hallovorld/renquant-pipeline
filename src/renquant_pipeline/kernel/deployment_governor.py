"""Deployment Governor (L1) — session target gross exposure E*.

Implements §2.1 of the Deployment Governor RFC (orchestrator
``doc/design/2026-07-09-deployment-governor-rfc.md``, D2): a PURE function
that computes the session's target gross exposure from the per-name
shrunk-Kelly raws of the admitted slate. No I/O, no ctx, no config reads —
the pipeline integration (``kernel/pipeline/governor_sizing.py``) wires
inputs and consumes the decision.

Algorithm (RFC §2.1, ceiling only — NO exposure floor):

    raw_i  = λ · max(μ̂_i − s·σ_i, 0) / σ_i²      (shrunk fractional Kelly,
                                                   same convention as
                                                   ``fractional_kelly_top_k``)
    E_raw  = Σ_{i ∈ top-k} min(raw_i, cap_i)
    E*     = min(E_raw, E_ceil(regime))
    then hysteresis:  |E* − E_current| ≤ band  ⇒  E* = E_current
    then step limit:  |E* − E_current| ≤ max_step_per_session ×
                      confidence_to_size_multiplier(confidence)

Fail-closed semantics (RFC §2.1):

* ``model_fault=True``  → return ``None`` — the caller MUST fall back to
  the legacy sizing path. Model fault means staleness / fingerprint
  mismatch / missing moments — a broken signal, not a weak one.
* Unmapped regime (no ``e_ceil_by_regime`` entry) or a non-finite
  ``current_gross_exposure`` are contract faults → ``None`` as well.
* A WEAK SLATE IS NOT A FAULT: a healthy model with few/weak admitted
  names produces a LOW E* — the correct output — together with
  ``slate_stats`` (admitted count, Σraw, μ̂ dispersion) for decision-ledger
  stamping so weak-slate sessions are auditable, not silent.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Optional

from renquant_pipeline.kernel.regime import confidence_to_size_multiplier

__all__ = [
    "GovernorDecision",
    "shrunk_kelly_raw",
    "compute_session_target_exposure",
]


def shrunk_kelly_raw(
    mu: float | None,
    sigma: float | None,
    *,
    kelly_fraction: float,
    mu_shrinkage: float,
) -> float:
    """Per-name shrunk fractional Kelly raw: ``λ · max(μ̂ − s·σ, 0) / σ²``.

    Same formula and guard conventions as
    :func:`renquant_pipeline.kernel.portfolio_qp.baseline_allocators.fractional_kelly_top_k`
    (Garlappi-Uppal-Wang 2007 shrinkage, Thorp fractional-Kelly safety) and
    the NaN/None guards of :func:`renquant_pipeline.kernel.kelly.kelly_target_pct`.
    Missing / non-finite inputs or σ ≤ 0 return 0.0 ("don't bet"), never
    raise — a zeroed name simply doesn't contribute to the slate.
    """
    if mu is None or sigma is None:
        return 0.0
    try:
        mu_f = float(mu)
        sigma_f = float(sigma)
    except (TypeError, ValueError):
        return 0.0
    if not (math.isfinite(mu_f) and math.isfinite(sigma_f)):
        return 0.0
    if sigma_f <= 0.0:
        return 0.0
    shrunk = mu_f - float(mu_shrinkage) * sigma_f
    if shrunk <= 0.0:
        return 0.0
    raw = float(kelly_fraction) * shrunk / (sigma_f * sigma_f)
    if not math.isfinite(raw) or raw <= 0.0:
        return 0.0
    return raw


@dataclass(frozen=True)
class GovernorDecision:
    """Output of :func:`compute_session_target_exposure`.

    ``e_target`` is the session target gross exposure after ceiling,
    hysteresis, and step limit. ``slate_stats`` carries the weak-slate
    audit payload for the decision ledger (RFC §2.1).
    """

    e_target: float          # final E* the allocator should size to
    e_raw: float             # Σ_{top-k} min(raw_i, cap_i), pre-ceiling
    e_ceil: float            # regime ceiling applied
    e_current: float         # current gross exposure input
    hysteresis_held: bool    # |E*−E_current| ≤ band → held at E_current
    step_limited: bool       # step clamp bound this session's move
    ceiling_bound: bool      # E_raw > E_ceil (ceiling was the binder)
    slate_stats: dict = field(default_factory=dict)


def compute_session_target_exposure(
    *,
    raws: Mapping[str, float | None],
    caps: Mapping[str, float],
    regime: str,
    e_ceil_by_regime: Mapping[str, float],
    current_gross_exposure: float,
    hysteresis_band: float,
    confidence: float | None,
    top_k: int,
    max_step_per_session: float,
    model_fault: bool = False,
    mu: Mapping[str, float | None] | None = None,
) -> Optional[GovernorDecision]:
    """Compute the session target gross exposure E* (RFC §2.1).

    Parameters
    ----------
    raws : name → shrunk-Kelly raw (see :func:`shrunk_kelly_raw`).
        Non-finite / non-positive values are treated as "not admitted".
    caps : name → per-name weight cap (e.g. regime ``max_position_pct``).
        A missing cap means "uncapped" for that name; the pipeline
        integration always supplies one per name.
    regime / e_ceil_by_regime : the regime label and its ceiling map.
        An unmapped regime is a config-contract fault → ``None``.
    current_gross_exposure : Σ of current held weights (post-exit book).
    hysteresis_band : no-trade band around E_current (Davis-Norman idea
        applied at the aggregate level).
    confidence : regime classifier confidence; scales the per-session
        step via the existing ``confidence_to_size_multiplier`` (floored
        at 0.5 — same convention as position sizing).
    top_k : number of names whose capped raws sum into E_raw.
    max_step_per_session : maximum |ΔE| per session at confidence 1.0.
    model_fault : True ⇒ return ``None`` (fail-closed; caller falls back
        to the legacy path). A weak slate must NOT set this.
    mu : optional name → μ̂ map, used only for the μ-dispersion slate stat.

    Returns
    -------
    GovernorDecision, or ``None`` on model fault / contract fault.
    """
    if model_fault:
        return None

    # Contract faults → fail-closed (None), never a silent default.
    try:
        e_ceil = float(e_ceil_by_regime[regime])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(e_ceil) or e_ceil < 0.0:
        return None
    try:
        e_current = float(current_gross_exposure)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(e_current):
        return None
    e_current = max(e_current, 0.0)

    band = _nonneg_finite(hysteresis_band, default=0.0)
    max_step = _nonneg_finite(max_step_per_session, default=0.0)
    k = max(int(top_k), 0)

    # ── Admitted slate (weak slate is NOT a fault) ────────────────────
    admitted: dict[str, float] = {}
    for name, value in raws.items():
        if value is None:
            continue
        try:
            r = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(r) and r > 0.0:
            admitted[name] = r

    chosen = sorted(admitted, key=lambda n: (-admitted[n], n))[:k]
    e_raw = 0.0
    for name in chosen:
        cap = _nonneg_finite(caps.get(name, math.inf), default=math.inf)
        e_raw += min(admitted[name], cap)

    e_star = min(e_raw, e_ceil)
    ceiling_bound = e_raw > e_ceil

    # ── Hysteresis: aggregate no-trade band around E_current ──────────
    hysteresis_held = False
    step_limited = False
    if abs(e_star - e_current) <= band:
        e_star = e_current
        hysteresis_held = True
    else:
        # ── Step limit: confidence-scaled max move per session ────────
        step = max_step * confidence_to_size_multiplier(confidence)
        if e_star > e_current + step:
            e_star = e_current + step
            step_limited = True
        elif e_star < e_current - step:
            e_star = e_current - step
            step_limited = True
    e_star = max(e_star, 0.0)

    # ── Weak-slate audit payload for the decision ledger ──────────────
    slate_stats = {
        "admitted_count": len(admitted),
        "selected_count": len(chosen),
        "sum_raw": float(sum(admitted.values())),
        "mu_dispersion": _mu_dispersion(mu, admitted) if mu is not None else None,
        "weak_slate": len(admitted) == 0,
    }

    return GovernorDecision(
        e_target=float(e_star),
        e_raw=float(e_raw),
        e_ceil=e_ceil,
        e_current=e_current,
        hysteresis_held=hysteresis_held,
        step_limited=step_limited,
        ceiling_bound=ceiling_bound,
        slate_stats=slate_stats,
    )


def _nonneg_finite(value, *, default: float) -> float:
    """Coerce to a non-negative float; NaN / negative / unparseable → default.

    +inf passes through — it is only ever meaningful for a missing per-name
    cap ("uncapped"); band/step callers pass finite config values.
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(out) or out < 0.0:
        return default
    return out


def _mu_dispersion(
    mu: Mapping[str, float | None],
    admitted: Mapping[str, float],
) -> float | None:
    """Population stdev of finite μ̂ over the admitted slate (≥ 2 names)."""
    values: list[float] = []
    for name in admitted:
        v = mu.get(name)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            values.append(f)
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    var = sum((x - mean) ** 2 for x in values) / len(values)
    return math.sqrt(var)
