"""Deployment allocator (L2) — concentrated, DOWN-ONLY weight allocation.

Implements §2.2 of the Deployment Governor RFC (orchestrator
``doc/design/2026-07-09-deployment-governor-rfc.md``, D3) EXACTLY:

    1. w_i  = min(raw_i, cap_i)             per-name capped Kelly, top-k
                                            by conviction (raw desc)
    2. w   ← project(w)                     sector cap, correlation-pair
                                            cap, no-buy(wash-sale) mask —
                                            each enforced by REDUCING the
                                            offending weights only,
                                            lowest conviction trimmed
                                            first, never raising a weight
    3. if Σw > E*:  w ← w · E*/Σw           proportional scale-DOWN only
    4. E_final  = Σw                        the DECLARED exposure —
                                            always achievable
    5. residual = E* − E_final              routed to the parking sleeve,
                                            stamped with the binding
                                            constraint that produced it

INVARIANT (asserted): no output weight ever exceeds its per-name cap.

No-sell floors (RFC §1.3 / §2.2 masks): a held name under a min-hold or
wash-sale no-sell mask cannot be sold, so its CURRENT weight is a floor
the allocator must account for. Floored names are exempt from down-only
trims below their floor; every OTHER (reducible) weight is trimmed
instead — "reducing the offending weights only" with the floor treated
as non-reducible reality. A floored position that has DRIFTED above its
cap keeps its floor (the mask wins — the allocator never sells); the cap
invariant for floored names is therefore ``w_i ≤ max(cap_i, floor_i)``
and the allocation itself never ADDS above the cap. With no floors the
operator is byte-for-byte the RFC §2.2 pipeline.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence

__all__ = ["AllocationResult", "allocate_down_only"]

_EPS = 1e-9


@dataclass(frozen=True)
class AllocationResult:
    """Output of :func:`allocate_down_only`.

    ``weights`` contains only strictly positive final target weights.
    ``residual = e_star − e_final``; it is ≥ 0 whenever no-sell floors do
    not by themselves exceed E* (always, when no floors are passed).
    ``binding_constraints`` names every constraint family that reduced
    (or pinned) a weight, for decision-ledger stamping (RFC §2.2 step 5).
    """

    weights: dict[str, float]
    e_final: float
    residual: float
    binding_constraints: dict = field(default_factory=dict)


def allocate_down_only(
    *,
    raws: Mapping[str, float | None],
    caps: Mapping[str, float],
    e_star: float,
    top_k: int,
    current_weights: Optional[Mapping[str, float]] = None,
    no_buy: Iterable[str] = (),
    no_sell: Iterable[str] = (),
    sector_by_name: Optional[Mapping[str, str]] = None,
    sector_caps: Optional[Mapping[str, float]] = None,
    corr_pair_caps: Sequence[tuple[str, str, float]] = (),
) -> AllocationResult:
    """Allocate target weights over the admitted slate, down-only (RFC §2.2).

    Parameters
    ----------
    raws : name → shrunk-Kelly raw (conviction ordering source). Names
        with non-finite / non-positive raw are not allocated.
    caps : name → per-name weight cap. Missing cap = uncapped (the
        pipeline integration always supplies one per name).
    e_star : the Governor's E* — a CEILING input, never a promise.
    top_k : concentration limit — at most k names receive new capital.
    current_weights : name → current held weight (needed for masks).
    no_buy : names whose weight may not INCREASE past current (wash-sale
        buy block): ``w_i ← min(w_i, current_i)`` — a pure reduction.
    no_sell : held names whose weight may not DECREASE below current
        (min-hold / wash-sale no-sell mask): current weight becomes a
        non-reducible floor; see module docstring.
    sector_by_name / sector_caps : sector membership and per-sector
        weight caps. Sectors without a cap entry are unconstrained.
    corr_pair_caps : (name_a, name_b, cap) triples — enforce
        ``w_a + w_b ≤ cap`` (same convention as the QP's
        correlation-group constraint).
    """
    current = {k: max(_f(v, 0.0), 0.0) for k, v in (current_weights or {}).items()}
    no_buy_set = set(no_buy)
    no_sell_set = set(no_sell)
    e_star_f = max(_f(e_star, 0.0), 0.0)
    binding: dict = {
        "per_name_cap": [],
        "no_buy": [],
        "no_sell_floor": [],
        "sector_cap": {},
        "corr_pair_cap": [],
        "e_star_scaled": False,
        "top_k_dropped": [],
    }

    # ── Step 1: w_i = min(raw_i, cap_i), top-k by conviction ──────────
    admitted: dict[str, float] = {}
    for name, value in raws.items():
        r = _f(value, 0.0)
        if math.isfinite(r) and r > 0.0:
            admitted[name] = r
    order = sorted(admitted, key=lambda n: (-admitted[n], n))
    k = max(int(top_k), 0)
    chosen, dropped = order[:k], order[k:]
    binding["top_k_dropped"] = list(dropped)

    weights: dict[str, float] = {}
    for name in chosen:
        cap = max(_f(caps.get(name, math.inf), math.inf), 0.0)
        w = min(admitted[name], cap)
        if w < admitted[name] - _EPS:
            binding["per_name_cap"].append(name)
        weights[name] = w

    # Conviction rank for trim ordering (lowest conviction trimmed first).
    conviction = dict(admitted)

    # ── Step 2a: no-buy (wash-sale) mask — pure reduction ─────────────
    for name in sorted(no_buy_set):
        if name not in weights:
            continue
        cur = current.get(name, 0.0)
        if weights[name] > cur + _EPS:
            weights[name] = cur
            binding["no_buy"].append(name)
        if weights[name] <= _EPS and name not in no_sell_set:
            weights.pop(name)

    # ── Step 2b: no-sell floors — non-reducible held weight ───────────
    # A no-sell held name retains at least its current weight regardless
    # of its raw (even raw=0 / not top-k): the book cannot shed it.
    floors: dict[str, float] = {}
    for name in sorted(no_sell_set):
        cur = current.get(name, 0.0)
        if cur <= _EPS:
            continue
        floors[name] = cur
        if weights.get(name, 0.0) < cur:
            weights[name] = cur
            binding["no_sell_floor"].append(name)

    def _floor(name: str) -> float:
        return floors.get(name, 0.0)

    def _trim_group(members: list[str], excess: float, family: str, tag) -> float:
        """Reduce group members lowest-conviction-first, down to floors.

        Returns the excess that could NOT be removed (floors binding).
        Never raises any weight — pure reduction (RFC §2.2 step 2).
        """
        trimmed_any = False
        for name in sorted(members, key=lambda n: (conviction.get(n, 0.0), n)):
            if excess <= _EPS:
                break
            reducible = weights.get(name, 0.0) - _floor(name)
            if reducible <= _EPS:
                continue
            cut = min(reducible, excess)
            weights[name] -= cut
            excess -= cut
            trimmed_any = True
            if weights[name] <= _EPS and _floor(name) <= _EPS:
                weights.pop(name)
        if trimmed_any:
            if family == "sector_cap":
                binding["sector_cap"][tag] = True
            elif family == "corr_pair_cap":
                binding["corr_pair_cap"].append(tag)
        return excess

    # ── Step 2c: sector caps — reduce offenders only ──────────────────
    if sector_by_name and sector_caps:
        for sector in sorted(sector_caps):
            cap = max(_f(sector_caps[sector], math.inf), 0.0)
            members = [n for n in list(weights)
                       if sector_by_name.get(n) == sector]
            load = sum(weights[n] for n in members)
            if load > cap + _EPS:
                _trim_group(members, load - cap, "sector_cap", sector)

    # ── Step 2d: correlation-pair caps — reduce offenders only ────────
    for trip in corr_pair_caps:
        try:
            a, b, cap = str(trip[0]), str(trip[1]), float(trip[2])
        except (TypeError, IndexError, ValueError):
            continue
        pair_sum = weights.get(a, 0.0) + weights.get(b, 0.0)
        if pair_sum > cap + _EPS:
            _trim_group([n for n in (a, b) if n in weights],
                        pair_sum - cap, "corr_pair_cap", (a, b))

    # ── Step 3: Σw > E* → proportional scale-DOWN of reducible mass ───
    total = sum(weights.values())
    if total > e_star_f + _EPS:
        floor_total = sum(_floor(n) for n in weights)
        reducible_total = total - floor_total
        excess = total - e_star_f
        if reducible_total > _EPS:
            # With no floors this is exactly w ← w · E*/Σw (factor ≤ 1).
            factor = max(0.0, 1.0 - excess / reducible_total)
            for name in list(weights):
                fl = _floor(name)
                weights[name] = fl + (weights[name] - fl) * factor
                if weights[name] <= _EPS and fl <= _EPS:
                    weights.pop(name)
            binding["e_star_scaled"] = True

    # ── Step 4/5: declared exposure + exact residual accounting ───────
    weights = {n: w for n, w in weights.items() if w > _EPS}
    e_final = float(sum(weights.values()))
    residual = float(e_star_f - e_final)

    # INVARIANT: no output weight ever exceeds its cap (floored names:
    # never above max(cap, floor) — the allocation never ADDS past cap).
    for name, w in weights.items():
        cap = max(_f(caps.get(name, math.inf), math.inf), 0.0)
        limit = max(cap, _floor(name))
        assert w <= limit + 1e-6, (
            f"allocator invariant violated: {name} w={w} > "
            f"max(cap={cap}, floor={_floor(name)})"
        )
        assert w >= 0.0, f"allocator invariant violated: {name} w={w} < 0"

    return AllocationResult(
        weights=weights,
        e_final=e_final,
        residual=residual,
        binding_constraints=binding,
    )


def _f(value, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(out):
        return default
    return out
