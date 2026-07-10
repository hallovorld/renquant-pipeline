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

Two of the three independent L1 candidates from RFC §2.1 are implemented
(r4 review — each defined fully and separately; only (B) is bounded by
E_raw by construction):

    (A) E*_ceil      = E_ceil(regime)                     PREREGISTERED
                                                            CANDIDATE
    (B) E*_kelly     = min(E_raw, E_ceil(regime))          COMPARISON ARM
                                                            (E* ≤ E_raw
                                                            guaranteed)

**(C) E*_voltarget is NOT implemented here** (post-#443-merge Codex review):
the RFC defines ``E_vol = σ_target / σ̂_pf`` where ``σ̂_pf`` MUST be the
realized/forecast volatility of the PORTFOLIO at the current top-k
E_raw-capped weights (i.e. ``sqrt(w^T Σ w)`` over the selected names' own
covariance) — not a market-index proxy. This module's first attempt at
(C) called ``kernel/vol_target.py::compute_vol_target_scale`` (an SPY-
proxied, β≈1 realized-vol scale, honestly documented as a proxy in its own
module) and mislabeled that proxy as the RFC's portfolio quantity; the two
diverge whenever the selected slate is concentrated or its names'
correlation structure differs from their correlation with SPY. A real
``σ̂_pf`` needs the selected names' n×n covariance matrix — this codebase
DOES compute one (``portfolio_qp/tasks.py::ComputeFullSigmaTask``, from a
loaded correlation artifact + per-name σ), but only inside the QP
pipeline's I/O/ctx-dependent task chain, which the Governor path is
designed to REPLACE, not depend on (this pure function takes no ctx/I-O
by contract, and the Governor's own pipeline integration never runs
``ComputeFullSigmaTask``). Wiring correlation-artifact loading into the
Governor path would be new cross-system integration, not a reuse of an
existing convention — out of scope for this fix. ``voltarget`` is
therefore removed from :data:`L1_CANDIDATES` until a real portfolio-vol
estimator exists; re-add it then, following the same pattern as (A)/(B).

Selected via ``l1_candidate`` ("ceil" | "kelly", default "kelly" — the
pre-existing behavior, preserved as the default so this addition is
additive/non-breaking). D6 §2 Phase-2's confirmatory run compares (A)
against (B); (C) rejoins the comparison once it has a real implementation.

Then, for whichever candidate produced E* above:

    hysteresis:  |E* − E_current| ≤ band  ⇒  E* = E_current
    step limit:  |E* − E_current| ≤ max_step_per_session ×
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

L1_CANDIDATES = ("ceil", "kelly")


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
    l1_candidate: str = "kelly"   # which of (A)/(B)/(C) produced e_target
    e_vol: float | None = None    # candidate (C)'s E_vol input, else None
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
    l1_candidate: str = "kelly",
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
    l1_candidate : one of :data:`L1_CANDIDATES` — which of the RFC §2.1
        candidates (A) ``"ceil"``, (B) ``"kelly"`` (default, pre-existing
        behavior) produces ``e_target``. (C) ``"voltarget"`` is not yet
        implemented (see module docstring) and is not a member of
        :data:`L1_CANDIDATES`. An unknown value is a contract fault →
        ``None`` (fail loud, no silent fallback to a different candidate
        than requested).

    Returns
    -------
    GovernorDecision, or ``None`` on model fault / contract fault.
    """
    if model_fault:
        return None

    if l1_candidate not in L1_CANDIDATES:
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

    # ── L1 candidate selection (RFC §2.1) ──────────────────────────────
    # (C) "voltarget" is not a member of L1_CANDIDATES (see module
    # docstring) — l1_candidate is validated against L1_CANDIDATES above,
    # so only "ceil"/"kelly" ever reach here.
    e_vol: float | None = None
    if l1_candidate == "ceil":
        # (A) preregistered candidate: independent of E_raw by
        # construction — the ceiling itself IS the pre-hysteresis target.
        e_pre = e_ceil
    else:  # "kelly" (B), the pre-existing default behavior
        e_pre = min(e_raw, e_ceil)

    e_star = e_pre
    # ceiling_bound: whether the regime ceiling was this session's binder.
    # "kelly" keeps its exact original strict-inequality semantics
    # (byte-identical to pre-candidate-selection behavior); the other two
    # candidates use >= since e_pre == e_ceil there is itself a ceiling bind.
    ceiling_bound = (e_raw > e_ceil) if l1_candidate == "kelly" else (e_pre >= e_ceil)

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
        l1_candidate=l1_candidate,
        e_vol=e_vol,
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
