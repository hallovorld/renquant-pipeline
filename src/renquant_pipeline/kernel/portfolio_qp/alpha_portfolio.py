"""Stage-A alpha-portfolio allocators + transfer-coefficient metric.

Implements the measurement instruments and the production-feasible
variant from the IC→Sharpe RFC
(renquant-orchestrator ``doc/research/2026-06-10-ic-to-pnl-architecture.md``,
merged PR #65):

==========  ============================================================
A0          rank-decile long-short, equal weight, horizon-held — the
            **measurement instrument**: its Sharpe is the empirical
            ceiling the signal's IC implies (Gu-Kelly-Xiu 2020 decile
            evaluation; Fama-French sorts).
A1          α-proportional long-short, w ∝ z-scored μ̂, dollar-neutral
            (Grinold 1994: α = IC·σ·z).
A2          long-only α-tilt — A1 projected onto w ≥ 0, Σw ≤ budget,
            per-name hard caps (the production-feasible point;
            CDST 2002 §long-only).
TC          per-date Spearman transfer coefficient between held and
            signal-implied active weights (CDST 2002) — the §3.2 RFC
            measurement definition.
==========  ============================================================

**Measurement instruments vs candidates.** A0/A1 intentionally hold
short legs and therefore violate ``w_lower``/long-only constraint
families of a production :class:`ConstraintSnapshot`. They are NOT
production candidates and must never be promoted; the replay harness
counts those violations as designed, and E1 reporting separates
``measurement::*`` allocators from production candidates by name
prefix. A2 is the production-feasible variant.

All allocators follow the harness contract
(``AllocatorFn(snap, mu=..., sigma=...) -> AllocatorResult``) so they
plug into the §8 step-4g replay (``allocator_replay.replay_all``)
unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

from renquant_pipeline.kernel.portfolio_qp.allocator_replay import (
    AllocatorReplayBar,
    ReplayResult,
    replay_one_allocator,
)
from renquant_pipeline.kernel.portfolio_qp.baseline_allocators import AllocatorResult
from renquant_pipeline.kernel.portfolio_qp.constraint_snapshot import ConstraintSnapshot

MEASUREMENT_PREFIX = "measurement::"


# ── cross-sectional helpers ──────────────────────────────────────────────────

def cross_sectional_zscore(mu: np.ndarray) -> np.ndarray:
    """Z-score of μ̂ over the finite entries; non-finite → 0 (no bet).

    The z-score is the canonical "score" in Grinold's α = IC·σ·z; using
    it (rather than raw μ̂) makes Stage A invariant to affine errors in
    the calibrator — exactly the property a *ranking* model's IC
    measures.
    """
    mu = np.asarray(mu, dtype=float)
    z = np.zeros_like(mu)
    finite = np.isfinite(mu)
    if finite.sum() < 2:
        return z
    vals = mu[finite]
    sd = float(np.std(vals))
    if sd < 1e-12:
        return z
    z[finite] = (vals - float(np.mean(vals))) / sd
    return z


def _rankdata(x: np.ndarray) -> np.ndarray:
    """Average-rank (ties → mean rank), numpy-only Spearman support."""
    x = np.asarray(x, dtype=float)
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(len(x), dtype=float)
    ranks[order] = np.arange(1, len(x) + 1, dtype=float)
    # average ties
    sorted_x = x[order]
    i = 0
    while i < len(x):
        j = i
        while j + 1 < len(x) and sorted_x[j + 1] == sorted_x[i]:
            j += 1
        if j > i:
            avg = float(np.mean(np.arange(i + 1, j + 2)))
            ranks[order[i:j + 1]] = avg
        i = j + 1
    return ranks


def transfer_coefficient(
    held_w: np.ndarray,
    signal_w: np.ndarray,
) -> Optional[float]:
    """Per-date Spearman TC between held and signal-implied active weights.

    RFC §3.2 definition: Spearman correlation over the union universe,
    defined only when both vectors have cross-sectional dispersion.
    Returns ``None`` when undefined (all-cash date, constant weights).
    """
    a = np.asarray(held_w, dtype=float)
    b = np.asarray(signal_w, dtype=float)
    if a.shape != b.shape or len(a) < 3:
        return None
    finite = np.isfinite(a) & np.isfinite(b)
    if finite.sum() < 3:
        return None
    a, b = a[finite], b[finite]
    if float(np.std(a)) < 1e-15 or float(np.std(b)) < 1e-15:
        return None
    ra, rb = _rankdata(a), _rankdata(b)
    ra -= ra.mean()
    rb -= rb.mean()
    denom = float(np.sqrt((ra ** 2).sum() * (rb ** 2).sum()))
    if denom < 1e-15:
        return None
    return float((ra * rb).sum() / denom)


# ── Stage-A allocators ───────────────────────────────────────────────────────

def decile_long_short(
    snap: ConstraintSnapshot,
    *,
    mu: np.ndarray,
    sigma: np.ndarray | None = None,  # noqa: ARG001 — harness contract
    fraction: float = 0.10,
    gross: float = 1.0,
) -> AllocatorResult:
    """A0 — rank-decile long-short ceiling (measurement instrument).

    Long the top ``fraction`` of finite-μ̂ names, short the bottom
    ``fraction``, equal weight within each leg, legs sized ±gross/2.
    Ignores production caps BY DESIGN (this measures the signal, not a
    deployable book) — under a long-only snapshot the harness will
    count ``w_lower`` violations; that is expected and documented.
    """
    mu = np.asarray(mu, dtype=float)
    n = snap.n
    target = np.zeros(n)
    finite_idx = [i for i in range(n) if np.isfinite(mu[i])]
    k = max(1, int(round(len(finite_idx) * float(fraction))))
    if len(finite_idx) < 2 * k:
        return AllocatorResult(
            delta_w=-snap.w_current,
            target_w=target,
            status="no_candidates",
            selected_indices=(),
        )
    by_mu = sorted(finite_idx, key=lambda i: float(mu[i]))
    shorts = by_mu[:k]
    longs = by_mu[-k:]
    leg = float(gross) / 2.0
    for i in longs:
        target[i] = leg / k
    for i in shorts:
        target[i] = -leg / k
    return AllocatorResult(
        delta_w=target - snap.w_current,
        target_w=target,
        status="optimal",
        selected_indices=tuple(sorted(longs + shorts)),
    )


def alpha_proportional_long_short(
    snap: ConstraintSnapshot,
    *,
    mu: np.ndarray,
    sigma: np.ndarray | None = None,  # noqa: ARG001 — harness contract
    gross: float = 1.0,
) -> AllocatorResult:
    """A1 — α-proportional dollar-neutral long-short (Grinold 1994).

    w ∝ demeaned z(μ̂), scaled to Σ|w| = gross. Measurement instrument:
    same constraint caveats as A0.
    """
    n = snap.n
    finite = np.isfinite(np.asarray(mu, dtype=float))
    z = cross_sectional_zscore(mu)
    active = np.zeros(n)
    if finite.any():
        # demean over the finite (bettable) names ONLY — non-finite μ̂
        # names must stay at exactly zero weight
        active[finite] = z[finite] - float(z[finite].mean())
    l1 = float(np.abs(active).sum())
    if l1 < 1e-12:
        return AllocatorResult(
            delta_w=-snap.w_current,
            target_w=np.zeros(n),
            status="no_candidates",
            selected_indices=(),
        )
    target = active * (float(gross) / l1)
    return AllocatorResult(
        delta_w=target - snap.w_current,
        target_w=target,
        status="optimal",
        selected_indices=tuple(i for i in range(n) if abs(target[i]) > 0),
    )


def alpha_tilt_long_only(
    snap: ConstraintSnapshot,
    *,
    mu: np.ndarray,
    sigma: np.ndarray | None = None,  # noqa: ARG001 — harness contract
) -> AllocatorResult:
    """A2 — long-only α-tilt: A1 projected onto the production box.

    Positive part of z(μ̂), normalised to the cash budget
    (1 − cash_reserve), clipped to the per-name hard caps. This is the
    minimal production-feasible Stage-A point (CDST long-only); the
    E1 ladder adds further production gates one at a time on top.
    Freed weight from cap-clipping is NOT redistributed — keeping the
    map monotone in the signal (redistribution would re-rank names).
    """
    z = cross_sectional_zscore(mu)
    pos = np.clip(np.where(np.isfinite(z), z, 0.0), 0.0, None)
    total = float(pos.sum())
    n = snap.n
    if total < 1e-12:
        return AllocatorResult(
            delta_w=-snap.w_current,
            target_w=np.zeros(n),
            status="no_candidates",
            selected_indices=(),
        )
    budget = max(0.0, 1.0 - float(snap.cash_reserve))
    target = pos * (budget / total)
    target = np.clip(target, 0.0, snap.w_upper_hard)
    return AllocatorResult(
        delta_w=target - snap.w_current,
        target_w=target,
        status="optimal",
        selected_indices=tuple(i for i in range(n) if target[i] > 0),
    )


# ── TC-instrumented replay ───────────────────────────────────────────────────

@dataclass
class TCReplayResult:
    """A :class:`ReplayResult` plus the per-bar transfer coefficient.

    ``tc_per_bar[i]`` is the Spearman TC (RFC §3.2) between the
    allocator's held target weights on bar i and the signal-implied
    active weights (A1 on the same bar — the signal's own opinion).
    ``None`` entries are undefined dates (all-cash / degenerate).
    """

    replay: ReplayResult
    tc_per_bar: list[Optional[float]] = field(default_factory=list)

    @property
    def tc_mean(self) -> Optional[float]:
        vals = [t for t in self.tc_per_bar if t is not None]
        return float(np.mean(vals)) if vals else None

    @property
    def tc_std(self) -> Optional[float]:
        vals = [t for t in self.tc_per_bar if t is not None]
        return float(np.std(vals, ddof=1)) if len(vals) > 1 else None


def replay_one_allocator_with_tc(
    name: str,
    allocator,
    bars: Sequence[AllocatorReplayBar],
) -> TCReplayResult:
    """``replay_one_allocator`` plus per-bar TC against the A1 signal book.

    Purely additive wrapper: metrics come from the unmodified harness
    (so DSR/PBO comparisons stay byte-compatible with step-4g); the TC
    series is computed in a second pass over the same bars.
    """
    replay = replay_one_allocator(name, allocator, bars)
    tc: list[Optional[float]] = []
    for bar in bars:
        try:
            alloc = allocator(bar.snap, mu=bar.mu, sigma=bar.sigma)
        except TypeError:
            alloc = allocator(bar.snap, mu=bar.mu)
        signal = alpha_proportional_long_short(bar.snap, mu=bar.mu)
        tc.append(transfer_coefficient(alloc.target_w, signal.target_w))
    return TCReplayResult(replay=replay, tc_per_bar=tc)


def stage_a_allocators() -> dict:
    """The RFC Stage-A set, keyed for ``allocator_replay.replay_all``.

    Measurement instruments carry the ``measurement::`` prefix so the
    step-4g verdict logic can exclude them from the zero-violations
    promotion gate while still reporting their ceilings.
    """
    return {
        f"{MEASUREMENT_PREFIX}A0_decile_ls": decile_long_short,
        f"{MEASUREMENT_PREFIX}A1_alpha_prop_ls": alpha_proportional_long_short,
        "A2_alpha_tilt_long_only": alpha_tilt_long_only,
    }
