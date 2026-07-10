"""Allocator A/B replay harness — §8 Step 4b (PR #125).

Runs N allocators on a shared sequence of per-bar inputs
(:class:`AllocatorReplayBar`) and produces per-allocator paired-daily
returns + Sharpe / MDD / turnover + per-regime stratified attribution.

This module is **the math**, not the data loader. A separate
follow-up PR wires the WF cut loader (training artifact + holdout
dates + per-cut Σ̂) to this module's input shape. Tests in this PR
use synthetic bars so the harness math can be pinned independently
of the production artifact storage.

Output is :class:`ReplayResult` per allocator — JSON-serialisable so
the decision-grade A/B replay artifact can be committed under
``doc/research/evidence/``.

The harness deliberately keeps DSR / PBO out of this scaffolding
module; those are added in Step 4c via ``kernel.metrics`` (lifted to
renquant-common) so the same multiple-comparison correction applies
to both the QP and Hybrid candidate evaluations.
"""
from __future__ import annotations

import dataclasses
import datetime
import math
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

import numpy as np

from renquant_pipeline.kernel.deployment_governor import shrunk_kelly_raw
from renquant_pipeline.kernel.portfolio_qp.baseline_allocators import AllocatorResult
from renquant_pipeline.kernel.portfolio_qp.constraint_snapshot import ConstraintSnapshot
from renquant_pipeline.kernel.rotation import tax_drag


# An allocator callable: takes (snapshot, mu, sigma) and returns AllocatorResult.
# (The richer QP / Hybrid signatures absorb extra kwargs via partial.)
AllocatorFn = Callable[..., AllocatorResult]


@dataclass(frozen=True)
class AllocatorReplayBar:
    """One bar of input to the A/B replay.

    The same bar is fed to every allocator under test — they see the
    same snapshot, μ̂, σ̂, and realised forward return. Paired-daily-
    returns + DSR/PBO comparisons all key off this shared input.
    """

    bar_date: str                          # ISO date (informational)
    snap: ConstraintSnapshot
    mu: np.ndarray                          # shape (n,)
    sigma: np.ndarray                       # shape (n,)
    fwd_return: np.ndarray                  # shape (n,) — realised per-asset return
    regime: Optional[str] = None            # for per-regime stratification
    cost_per_trade_bps: float = 5.0         # 5 bp round-trip transaction cost
    # Session close prices per asset (shape (n,), NaN when unknown).
    # Optional; only required by the D6 whole-share quantization
    # convention (``ReplayConventions.integer_shares``).
    prices: Optional[np.ndarray] = None


# ════════════════════════════════════════════════════════════════════
#  D6 protocol replay conventions (all OPT-IN — defaults preserve the
#  pre-D6 stateless behavior byte-for-byte)
# ════════════════════════════════════════════════════════════════════

#: D6 §1.1 frozen tax convention — realized-gain tax rates.
D6_TAX_SHORT_RATE = 0.50
D6_TAX_LONG_RATE = 0.32
D6_LONG_TERM_THRESHOLD_DAYS = 365
#: D6 §4 frozen non-degradation gates.
D6_PER_NAME_CAP = 0.12
D6_SECTOR_CAP = 0.35


@dataclass(frozen=True)
class ReplayConventions:
    """Opt-in D6 protocol conventions for the replay harness.

    Every field defaults to OFF / the pre-D6 value; a ``None`` (or
    all-defaults) conventions object reproduces the stateless harness
    behavior exactly, so existing committed evidence stays
    reproducible (pinned by ``tests/test_replay_d6_conventions.py``).

    * ``stateful`` — carry portfolio state (positions, tax lots with
      entry date + entry price, cash) across sessions within an arm.
      Deployed fraction becomes a real state variable distinct from
      turnover; allocators receive the carried ``w_current`` so
      hysteresis / no-trade bands are evaluable.
    * ``tax`` — charge realized-gain tax on every exit leg (D6 §1.1
      frozen convention: short 50% / long 32%, lot holding period
      decides), mirroring :func:`renquant_pipeline.kernel.rotation.tax_drag`
      (losses give zero drag). Requires ``stateful``.
    * ``integer_shares`` — RFC #443 §2.3 L3 integer-aware execution
      (executed-state invariant), mirroring the merged production L3
      (kernel/pipeline/governor_sizing.py): round DOWN by default
      (``floor(Δw · PV / p)`` in conviction order), a deferred
      one-share rescue on leftover investable headroom (cap- and
      reserve-headroom-bounded, evaluated AFTER all round-down orders
      fund), and post-round rechecks of cash (incl. reserve) /
      single-name cap / sector cap / correlation pairs on the EXECUTED
      quantities — violating buys are capped down, never carried in
      breach. The post-round executed weights (not the continuous
      targets) carry into state. Requires ``stateful`` and per-bar
      ``prices``. See :func:`_execute_integer_session`.
    * ``enforce_caps`` — apply the D6 §4 per-name / sector caps INSIDE
      the arm as a down-only projection before returns are computed,
      recording per-session breach counters instead of silently
      allowing breaches. Sector caps need ``sector_map``; decision-grade
      runs FAIL CLOSED when the map does not cover every active ticker
      in every replay bar (r2 #180 review). The permissive behavior
      survives only behind the explicit exploratory flag
      ``allow_unmapped_sectors``, which marks the evidence
      non-decision-grade.
    * ``allow_unmapped_sectors`` — EXPLORATORY ONLY: lets
      ``enforce_caps`` run with a missing/partial sector map (unmapped
      tickers carry no sector constraint). Forces
      ``execution_fidelity`` to ``"L1_L2_ONLY"`` and
      ``promotion_eligible`` to False — the evidence cannot pass the
      promotion gate.
    """

    stateful: bool = False
    tax: bool = False
    integer_shares: bool = False
    enforce_caps: bool = False
    tax_short_rate: float = D6_TAX_SHORT_RATE
    tax_long_rate: float = D6_TAX_LONG_RATE
    long_term_threshold_days: int = D6_LONG_TERM_THRESHOLD_DAYS
    per_name_cap: float = D6_PER_NAME_CAP
    sector_cap: float = D6_SECTOR_CAP
    sector_map: Optional[dict] = None      # ticker → sector name
    initial_capital: float = 10_000.0      # dollars; scale for share floors
    allow_unmapped_sectors: bool = False   # exploratory only (r2 fail-closed)

    def __post_init__(self) -> None:
        if self.tax and not self.stateful:
            raise ValueError(
                "ReplayConventions: tax=True requires stateful=True — the D6 "
                "tax convention charges realized-gain tax on exit legs, which "
                "only exist when lots are carried across sessions."
            )
        if self.integer_shares and not self.stateful:
            raise ValueError(
                "ReplayConventions: integer_shares=True requires stateful=True "
                "— the executed-state invariant carries post-round weights "
                "into the next session's state."
            )
        if self.initial_capital <= 0:
            raise ValueError(
                f"ReplayConventions: initial_capital must be > 0, got "
                f"{self.initial_capital}"
            )
        # NOTE (r2 #180 merged design): enforce_caps WITHOUT a sector map
        # is not rejected at construction — callers need the object to run
        # the sector_map_coverage_gap prescan (the CLI writes a structured
        # invalid_experiment artifact). The fail-closed guarantee is
        # enforced at REPLAY time instead: apply_d6_cap_projection raises
        # on any unmapped active ticker unless allow_unmapped_sectors.

    @property
    def any_enabled(self) -> bool:
        return bool(self.stateful or self.enforce_caps)

    @property
    def execution_fidelity(self) -> str:
        """Machine-readable evidence-contract stamp (r2 #180 review).

        ``"L3_FULL"`` iff the FULL D6 execution-layer convention set is
        engaged: stateful + tax + integer-shares (round-down, deferred
        rescue, post-round rechecks) + fail-closed cap enforcement with
        a supplied sector map. Anything less — including any exploratory
        sector coverage — is ``"L1_L2_ONLY"``: usable for allocator-
        ranking diagnostics but NOT deployed-fraction / end-to-end /
        promotion evidence. The promotion gate rejects non-L3_FULL
        payloads mechanically (see ``run_ab_replay``).
        """
        full = (
            self.stateful
            and self.tax
            and self.integer_shares
            and self.enforce_caps
            and self.sector_map is not None
            and not self.allow_unmapped_sectors
        )
        return "L3_FULL" if full else "L1_L2_ONLY"

    @property
    def promotion_eligible(self) -> bool:
        return self.execution_fidelity == "L3_FULL"

    def to_dict(self) -> dict:
        out: dict = {
            "stateful": self.stateful,
            "tax": self.tax,
            "integer_shares": self.integer_shares,
            "enforce_caps": self.enforce_caps,
            "execution_fidelity": self.execution_fidelity,
            "promotion_eligible": self.promotion_eligible,
        }
        if self.tax:
            out["tax_short_rate"] = self.tax_short_rate
            out["tax_long_rate"] = self.tax_long_rate
            out["long_term_threshold_days"] = self.long_term_threshold_days
        if self.enforce_caps:
            out["per_name_cap"] = self.per_name_cap
            out["sector_cap"] = self.sector_cap
            out["sector_map_supplied"] = self.sector_map is not None
            out["n_sector_mapped_tickers"] = (
                len(self.sector_map) if self.sector_map else 0
            )
            out["sector_coverage"] = (
                "exploratory_unmapped_allowed" if self.allow_unmapped_sectors
                else "fail_closed"
            )
        if self.stateful:
            out["initial_capital"] = self.initial_capital
        return out


@dataclass
class TaxLot:
    """One tax lot: entry date + entry price + basis, marked to market.

    ``market_value`` evolves with the replay's per-bar ``fwd_return``
    (returns-consistent pricing); ``cost_basis`` is fixed at entry.
    ``shares`` is only populated under the integer-shares convention.
    """

    entry_date: str
    entry_bar_index: int
    cost_basis: float                # dollars at entry
    market_value: float              # dollars, marked via fwd_return
    entry_price: Optional[float] = None   # per-share, integer mode only
    shares: float = 0.0                    # integer mode only


@dataclass
class PortfolioState:
    """Carried portfolio state for the stateful replay mode."""

    cash: float
    lots: dict[str, list[TaxLot]] = field(default_factory=dict)
    # Returns-evolved internal price per held ticker (integer mode).
    # Anchored to the session close price at (re-)entry, then evolved
    # multiplicatively by fwd_return so shares × price ≡ market value
    # exactly (cash-conservation invariant).
    internal_price: dict[str, float] = field(default_factory=dict)

    def position_value(self, ticker: str) -> float:
        return float(sum(lot.market_value for lot in self.lots.get(ticker, ())))

    def position_shares(self, ticker: str) -> float:
        return float(sum(lot.shares for lot in self.lots.get(ticker, ())))

    def total_positions_value(self) -> float:
        return float(
            sum(lot.market_value for lots in self.lots.values() for lot in lots)
        )

    @property
    def portfolio_value(self) -> float:
        return float(self.cash) + self.total_positions_value()


@dataclass
class ReplayResult:
    """Per-allocator output of the A/B replay.

    Attributes are JSON-serialisable (numpy → list inside
    :meth:`to_dict`). Sharpe is the annualised mean / std of daily
    net-of-cost returns; MDD is the maximum drawdown of the cumulative
    return series; turnover is the mean per-bar L1 |Δw|.

    **Constraint violation tracking** (codex #131 review HIGH): the
    Step 4 A/B gate requires *zero hard-constraint regressions vs the
    ConstraintSnapshot*. The harness validates every allocator output
    against the full hard-constraint set advertised by the snapshot
    and tallies per-family violations.
    """

    name: str
    bars: int
    daily_returns_net: list[float] = field(default_factory=list)
    turnover: list[float] = field(default_factory=list)
    cap_violations: int = 0  # any-family violation count (legacy alias)
    fallback_to_no_candidates: int = 0
    per_regime: dict[str, list[float]] = field(default_factory=dict)
    # Per-family violation counters (codex #131 review HIGH)
    violations_per_family: dict[str, int] = field(default_factory=dict)

    # ── D6 convention outputs (opt-in; None ⇒ convention not engaged,
    #    and the corresponding evidence keys are omitted so the default
    #    evidence schema stays byte-identical) ──────────────────────────
    deployed_fraction: Optional[list[float]] = None      # stateful
    cost_paid: Optional[list[float]] = None              # stateful ($/session)
    tax_paid: Optional[list[float]] = None               # stateful + tax
    executed_exposure: Optional[list[float]] = None      # integer: ΣW executed
    integer_residual: Optional[list[float]] = None       # integer: ΣW target−exec
    rescue_buys: Optional[list[int]] = None              # integer: RFC §2.3 rescue
    recheck_capdowns: Optional[list[int]] = None         # integer: RFC §2.3 recheck
    name_cap_breaches: Optional[list[int]] = None        # enforce_caps
    sector_cap_breaches: Optional[list[int]] = None      # enforce_caps
    off_universe_liquidations: Optional[int] = None      # stateful
    # Stateful accounting series (tests + audits; not serialized)
    cash_series: Optional[list[float]] = None
    positions_value_series: Optional[list[float]] = None
    final_state: Optional["PortfolioState"] = None

    @property
    def sharpe_annual(self) -> Optional[float]:
        r = np.asarray(self.daily_returns_net, dtype=float)
        if len(r) < 2:
            return None
        sd = float(np.std(r, ddof=1))
        if sd < 1e-12:
            return None
        return float(np.mean(r) / sd * np.sqrt(252.0))

    @property
    def mean_daily_return(self) -> float:
        r = self.daily_returns_net
        return float(np.mean(r)) if r else 0.0

    @property
    def cumulative_return(self) -> float:
        if not self.daily_returns_net:
            return 0.0
        return float(np.prod(1.0 + np.asarray(self.daily_returns_net)) - 1.0)

    @property
    def max_drawdown(self) -> float:
        if not self.daily_returns_net:
            return 0.0
        equity = np.cumprod(1.0 + np.asarray(self.daily_returns_net))
        peak = np.maximum.accumulate(equity)
        dd = (equity - peak) / peak
        return float(dd.min())

    @property
    def mean_turnover(self) -> float:
        return float(np.mean(self.turnover)) if self.turnover else 0.0

    def per_regime_sharpe(self) -> dict[str, Optional[float]]:
        out: dict[str, Optional[float]] = {}
        for regime, returns in self.per_regime.items():
            arr = np.asarray(returns, dtype=float)
            if len(arr) < 2:
                out[regime] = None
                continue
            sd = float(np.std(arr, ddof=1))
            out[regime] = (
                None if sd < 1e-12
                else float(np.mean(arr) / sd * np.sqrt(252.0))
            )
        return out

    def total_violations(self) -> int:
        return int(sum(self.violations_per_family.values()))

    def to_dict(self) -> dict:
        out = {
            "name": self.name,
            "bars": self.bars,
            "sharpe_annual": self.sharpe_annual,
            "mean_daily_return": self.mean_daily_return,
            "cumulative_return": self.cumulative_return,
            "max_drawdown": self.max_drawdown,
            "mean_turnover": self.mean_turnover,
            "cap_violations": self.cap_violations,
            "violations_per_family": dict(self.violations_per_family),
            "total_violations": self.total_violations(),
            "fallback_to_no_candidates": self.fallback_to_no_candidates,
            "per_regime_sharpe": self.per_regime_sharpe(),
            "per_regime_n_bars": {
                r: len(v) for r, v in self.per_regime.items()
            },
        }
        # D6 convention keys — strictly ADDITIVE and only emitted when
        # the corresponding convention was engaged, so the default
        # evidence schema stays byte-identical (schema contract: new
        # keys only, never change existing keys).
        if self.deployed_fraction is not None:
            out["deployed_fraction"] = [float(x) for x in self.deployed_fraction]
            out["mean_deployed_fraction"] = (
                float(np.mean(self.deployed_fraction))
                if self.deployed_fraction else 0.0
            )
        if self.cost_paid is not None:
            out["cost_paid"] = [float(x) for x in self.cost_paid]
            out["total_cost_paid"] = float(np.sum(self.cost_paid))
        if self.tax_paid is not None:
            out["tax_paid"] = [float(x) for x in self.tax_paid]
            out["total_tax_paid"] = float(np.sum(self.tax_paid))
        if self.executed_exposure is not None:
            out["E_executed"] = [float(x) for x in self.executed_exposure]
        if self.integer_residual is not None:
            out["integer_residual"] = [float(x) for x in self.integer_residual]
        if self.rescue_buys is not None:
            out["rescue_buys"] = [int(x) for x in self.rescue_buys]
            out["total_rescue_buys"] = int(np.sum(self.rescue_buys))
        if self.recheck_capdowns is not None:
            out["recheck_capdowns"] = [int(x) for x in self.recheck_capdowns]
            out["total_recheck_capdowns"] = int(np.sum(self.recheck_capdowns))
        if self.name_cap_breaches is not None:
            out["name_cap_breaches"] = [int(x) for x in self.name_cap_breaches]
            out["total_name_cap_breaches"] = int(np.sum(self.name_cap_breaches))
        if self.sector_cap_breaches is not None:
            out["sector_cap_breaches"] = [
                int(x) for x in self.sector_cap_breaches
            ]
            out["total_sector_cap_breaches"] = int(
                np.sum(self.sector_cap_breaches)
            )
        if self.off_universe_liquidations is not None:
            out["off_universe_liquidations"] = int(self.off_universe_liquidations)
        return out


# Hard-constraint families surfaced by ConstraintSnapshot.
_VIOLATION_FAMILIES = (
    "w_upper_hard",
    "w_lower",
    "wash_sale",
    "dw_max",
    "cash_budget",
    "turnover_max",
    "sector_cap",
    "corr_group_cap",
    "gross_max",
)


def check_snapshot_feasibility(
    snap: "ConstraintSnapshot",
    target_w: np.ndarray,
    delta_w: np.ndarray,
    *,
    tol: float = 1e-9,
) -> dict[str, int]:
    """Validate ``target_w`` / ``delta_w`` against the full snapshot
    hard-constraint set and return per-family violation counts (0 or 1
    each — one bar can contribute at most one violation per family).

    Codex #131 review HIGH fix: replay was previously only counting
    ``target_w > w_upper_hard`` and missing every other family. Step
    4's gate of *zero hard-constraint regressions vs ConstraintSnapshot*
    requires the full check.
    """
    fam: dict[str, int] = {name: 0 for name in _VIOLATION_FAMILIES}
    n = snap.n

    if (target_w > snap.w_upper_hard + tol).any():
        fam["w_upper_hard"] = 1
    if (target_w < snap.w_lower - tol).any():
        fam["w_lower"] = 1
    if snap.wash_sale_mask.any():
        # Δw_i must be ≤ 0 for masked names
        if (delta_w[snap.wash_sale_mask.astype(bool)] > tol).any():
            fam["wash_sale"] = 1
    if snap.dw_max is not None:
        if (np.abs(delta_w) > snap.dw_max + tol).any():
            fam["dw_max"] = 1
    budget = max(0.0, 1.0 - float(snap.cash_reserve))
    if float(target_w.sum()) > budget + tol:
        fam["cash_budget"] = 1
    if snap.turnover_max is not None:
        if float(np.sum(np.abs(delta_w))) > float(snap.turnover_max) + tol:
            fam["turnover_max"] = 1
    if snap.sector_indicator is not None and snap.sector_cap_vec is not None:
        loads = snap.sector_indicator @ target_w
        if (loads > snap.sector_cap_vec + tol).any():
            fam["sector_cap"] = 1
    for trip in snap.corr_group_pairs or ():
        try:
            i, j, cap = int(trip[0]), int(trip[1]), float(trip[2])
        except (TypeError, IndexError, ValueError):
            continue
        if 0 <= i < n and 0 <= j < n:
            if float(target_w[i] + target_w[j]) > cap + tol:
                fam["corr_group_cap"] = 1
    if snap.gross_max is not None:
        if float(np.sum(np.abs(target_w))) > float(snap.gross_max) + tol:
            fam["gross_max"] = 1
    return fam


def _call_allocator(
    allocator: AllocatorFn,
    snap: ConstraintSnapshot,
    bar: AllocatorReplayBar,
) -> AllocatorResult:
    try:
        return allocator(snap, mu=bar.mu, sigma=bar.sigma)
    except TypeError:
        # Allocator may not accept sigma (e.g. equal-weight).
        return allocator(snap, mu=bar.mu)


def apply_d6_cap_projection(
    target_w: np.ndarray,
    tickers: Sequence[str],
    conv: "ReplayConventions",
    *,
    tol: float = 1e-9,
) -> tuple[np.ndarray, int, int]:
    """D6 §4 in-arm constraint enforcement — DOWN-ONLY projection.

    Applies the per-name cap then the sector cap (via ``conv.sector_map``)
    to the allocator's proposed ``target_w``. Both steps only ever REDUCE
    weights (clip / proportional scale-down), so the projection cannot
    introduce a new breach in the other family. Returns
    ``(projected_w, n_name_breaches, n_sector_breaches)`` where the breach
    counts are the PRE-projection violations — recorded per session
    instead of silently allowing the breach (#445 gap 4).

    **Sector coverage is FAIL-CLOSED** (r2 #180 review): any active
    ticker not covered by ``conv.sector_map`` raises — a decision-grade
    run must never silently leave a name outside the D6 §4 35% sector
    gate. The permissive behavior (unmapped tickers carry no sector
    constraint — no silent guessing of membership) survives only behind
    the explicit exploratory flag ``conv.allow_unmapped_sectors``, whose
    evidence is marked non-decision-grade.
    """
    if not conv.allow_unmapped_sectors:
        sector_map = conv.sector_map or {}
        missing = [t for t in tickers if not sector_map.get(t)]
        if missing:
            raise ValueError(
                f"enforce_caps FAIL-CLOSED: sector map does not cover active "
                f"ticker(s) {missing} in this replay bar — the D6 §4 sector "
                f"gate cannot be enforced blind (r2 #180 review). Supply a "
                f"complete sector map, or set allow_unmapped_sectors=True / "
                f"--allow-unmapped-sectors for an EXPLORATORY run whose "
                f"evidence is marked non-decision-grade."
            )
    w = np.asarray(target_w, dtype=float).copy()

    # Per-name cap — down-only clip.
    over_name = w > conv.per_name_cap + tol
    n_name_breaches = int(np.count_nonzero(over_name))
    if n_name_breaches:
        w = np.minimum(w, conv.per_name_cap)

    # Sector cap — proportional scale-down of each over-cap sector.
    n_sector_breaches = 0
    if conv.sector_map:
        sector_to_idx: dict[str, list[int]] = {}
        for j, t in enumerate(tickers):
            sec = conv.sector_map.get(t)
            if sec and isinstance(sec, str):
                sector_to_idx.setdefault(sec, []).append(j)
        for _sec, idx_list in sorted(sector_to_idx.items()):
            idx = np.asarray(idx_list, dtype=int)
            load = float(w[idx].sum())
            if load > conv.sector_cap + tol:
                n_sector_breaches += 1
                w[idx] *= conv.sector_cap / load
    return w, n_name_breaches, n_sector_breaches


def sector_map_coverage_gap(
    bars: Sequence["AllocatorReplayBar"],
    conv: "ReplayConventions",
) -> tuple[str, ...]:
    """Tickers appearing in any bar's snapshot but absent from
    ``conv.sector_map`` (codex #180 review, 2026-07-10).

    ``apply_d6_cap_projection`` deliberately leaves an unmapped ticker
    unconstrained (no silent guessing of sector membership) — but a
    *caller* that claims to be running a D6 sector-cap replay must not
    let that permissive behavior silently convert a missing hard
    constraint into no constraint. This function surfaces the gap so the
    CLI/caller can decide to fail closed (default, D6-strict mode) or
    proceed only under an explicit exploratory/non-decision-grade mode.

    Returns an empty tuple when every ticker appearing in ``bars`` is a
    key in ``conv.sector_map`` (including the vacuous case of zero bars
    or a conventions object with ``enforce_caps=False``).
    """
    sector_map = conv.sector_map or {}
    missing: set[str] = set()
    for bar in bars:
        for t in bar.snap.tickers:
            if t not in sector_map:
                missing.add(t)
    return tuple(sorted(missing))


def _record_family_violations(
    res: ReplayResult,
    snap: ConstraintSnapshot,
    target_w: np.ndarray,
    delta_w: np.ndarray,
) -> None:
    """Per-family feasibility check (codex #131 review HIGH-1)."""
    family_viol = check_snapshot_feasibility(snap, target_w, delta_w)
    for fam_name, count in family_viol.items():
        if count > 0:
            res.violations_per_family[fam_name] = (
                res.violations_per_family.get(fam_name, 0) + count
            )
    if any(v > 0 for v in family_viol.values()):
        res.cap_violations += 1  # legacy any-family counter


def replay_one_allocator(
    name: str,
    allocator: AllocatorFn,
    bars: Sequence[AllocatorReplayBar],
    conventions: Optional[ReplayConventions] = None,
) -> ReplayResult:
    """Run a single allocator over the bar sequence and collect metrics.

    ``conventions`` is the opt-in D6 protocol convention set
    (:class:`ReplayConventions`). ``None`` — or a conventions object
    with nothing enabled — reproduces the original stateless harness
    behavior EXACTLY (pinned byte-identical by
    ``tests/test_replay_d6_conventions.py``).

    **no_candidates accounting** (codex #131 review HIGH-2): the
    allocator's returned ``target_w`` (typically zeros = liquidate to
    cash) and ``delta_w`` (= ``-w_current``) ARE the action the
    allocator chose; the harness must honour them. Previously
    ``no_candidates`` bars were short-circuited to zero return / zero
    turnover, which silently discarded the liquidation cost and
    over-stated baselines vs QP.
    """
    if conventions is not None and conventions.stateful:
        return _replay_one_allocator_stateful(name, allocator, bars, conventions)

    enforce = conventions is not None and conventions.enforce_caps
    res = ReplayResult(name=name, bars=len(bars))
    if enforce:
        res.name_cap_breaches = []
        res.sector_cap_breaches = []
    for bar in bars:
        alloc = _call_allocator(allocator, bar.snap, bar)
        if alloc.status == "no_candidates":
            res.fallback_to_no_candidates += 1
        # ALWAYS compute gross + cost from the allocator's own
        # target_w / delta_w — no_candidates means "go to cash" which
        # has a real liquidation cost.
        target_w = alloc.target_w
        delta_w = alloc.delta_w
        if enforce:
            # D6 §4 in-arm enforcement: the projected (executed) weights
            # are what earns returns / pays costs; the pre-projection
            # breach is recorded, not silently allowed.
            target_w, n_name, n_sector = apply_d6_cap_projection(
                target_w, bar.snap.tickers, conventions,
            )
            delta_w = target_w - bar.snap.w_current
            res.name_cap_breaches.append(n_name)
            res.sector_cap_breaches.append(n_sector)
        gross = float(np.sum(target_w * bar.fwd_return))
        turn = float(np.sum(np.abs(delta_w)))
        cost = turn * bar.cost_per_trade_bps * 1e-4
        daily = gross - cost
        _record_family_violations(res, bar.snap, target_w, delta_w)
        res.daily_returns_net.append(daily)
        res.turnover.append(turn)
        if bar.regime is not None:
            res.per_regime.setdefault(bar.regime, []).append(daily)
    return res


# ── D6 stateful replay engine ───────────────────────────────────────


def _parse_iso_date(s: str) -> Optional[datetime.date]:
    try:
        return datetime.date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _lot_hold_days(
    lot: TaxLot, bar_date: str, bar_index: int,
) -> int:
    """Lot holding period in days — decides the D6 short/long tax rate.

    Uses calendar days between ISO dates when both parse; falls back to
    the bar-index difference (synthetic test sequences whose bar_date is
    not ISO). The fallback treats one bar as one calendar day, which is
    conservative for the long-term boundary.
    """
    entry = _parse_iso_date(lot.entry_date)
    exit_ = _parse_iso_date(bar_date)
    if entry is not None and exit_ is not None:
        return (exit_ - entry).days
    return bar_index - lot.entry_bar_index


def _sell_from_lots(
    state: PortfolioState,
    ticker: str,
    sell_value: float,
    bar_date: str,
    bar_index: int,
    conv: ReplayConventions,
) -> float:
    """Consume lots FIFO for a sell leg; return the tax charged.

    Tax per the D6 §1.1 frozen convention, reusing the rotation
    ``tax_drag()`` convention: realized-gain fraction × rate (short
    50% / long 32% by lot holding period); losses give zero drag.
    Proceeds are credited to cash; tax is debited from cash.
    Shares (integer mode) are consumed proportionally to the value
    fraction taken from each lot, which is exact because every lot of
    a ticker is marked at the same returns-evolved internal price.
    """
    lots = state.lots.get(ticker, [])
    remaining = float(sell_value)
    tax_total = 0.0
    kept: list[TaxLot] = []
    for lot in lots:
        if remaining <= 1e-12:
            kept.append(lot)
            continue
        take = min(lot.market_value, remaining)
        fraction = take / lot.market_value if lot.market_value > 0 else 1.0
        basis_consumed = lot.cost_basis * fraction
        gain = take - basis_consumed
        if conv.tax and take > 0:
            hold_days = _lot_hold_days(lot, bar_date, bar_index)
            # rotation.tax_drag(): drag is a FRACTION of position value
            # (gain_pct × rate, 0 on losses/NaN); multiply back by the
            # exit-leg value to get dollars — exactly gain × rate.
            drag_pct = tax_drag(
                gain / take,
                hold_days,
                conv.tax_short_rate,
                conv.tax_long_rate,
                conv.long_term_threshold_days,
            )
            tax_total += drag_pct * take
        lot.market_value -= take
        lot.cost_basis -= basis_consumed
        lot.shares -= lot.shares * fraction
        remaining -= take
        if lot.market_value > 1e-12:
            kept.append(lot)
    if kept:
        state.lots[ticker] = kept
    else:
        state.lots.pop(ticker, None)
        state.internal_price.pop(ticker, None)
    state.cash += float(sell_value) - tax_total
    return tax_total


# Conviction ordering key (RFC #443 §2.3, "conviction, defined"): the
# shrunk fractional-Kelly raw, used ONLY as an ordering key. The
# fraction is a positive scalar (ordering-invariant); shrinkage matches
# the merged GOVERNOR_DEFAULTS (kernel/pipeline/governor_sizing.py).
_CONVICTION_KELLY_FRACTION = 0.3
_CONVICTION_MU_SHRINKAGE = 0.0


def _execute_integer_session(
    name: str,
    state: PortfolioState,
    bar: AllocatorReplayBar,
    target_w: np.ndarray,
    pv_base: float,
    conv: ReplayConventions,
    bar_index: int,
) -> tuple[np.ndarray, float, float, int, int]:
    """RFC #443 §2.3 L3 — integer-aware execution (executed-state invariant).

    Mirrors the merged production L3 (kernel/pipeline/governor_sizing.py
    ``_fill_buys`` + the S6 A-3 deferred one-share rescue in
    kernel/pipeline/task_selection.py):

    * **Round DOWN by default** — buy legs take ``floor(Δw·PV/p)`` shares
      in conviction order (shrunk-Kelly raw desc, ticker tiebreak),
      cash-aware; sell legs take ``floor(Δw·PV/p)`` shares (full
      liquidation when the target is ~0), so a lower-priority name only
      sees the cash genuinely left.
    * **Deferred one-share rescue** — AFTER all round-down orders fund,
      leftover investable headroom is re-offered one share at a time in
      conviction order to names still short of target (a name that
      floored to 0 rounds UP to exactly one share); each rescue share is
      permitted only iff it fits the per-name cap AND the remaining
      investable headroom (cash minus the snapshot cash reserve — the
      task_selection rescue's reserve-aware headroom). A name may
      overshoot its target by at most one share.
    * **Post-round recheck on EXECUTED quantities** — cash (incl.
      reserve; guaranteed by construction since every fill is
      headroom-bounded), single-name cap, sector caps (snapshot
      families + the D6 caps when ``enforce_caps``), and
      correlation-pair constraints are re-verified on the integer
      quantities; a violating BUY is capped down one share at a time
      (lowest conviction first, deterministic tiebreak) — never carried
      in breach. Breaches attributable to carried drift or sell legs
      (down-only by construction) are not "orders" and remain visible
      through the violation accounting instead.

    Returns ``(executed_w, traded_dollars, tax_dollars, n_rescue_buys,
    n_recheck_capdowns)``. ``executed_w`` is the ACTUAL post-fill book
    over the session's tickers — what carries into state (executed-state
    invariant) and what ``E_executed`` / ``integer_residual =
    Σtarget − E_executed`` are stamped from.
    """
    snap = bar.snap
    tickers = snap.tickers
    n = len(tickers)
    traded = 0.0
    tax_total = 0.0

    def _price_of(i: int, tk: str) -> float:
        p = state.internal_price.get(tk)
        if p is None:
            p = float(bar.prices[i]) if bar.prices is not None else float("nan")
        if not math.isfinite(p) or p <= 0.0:
            raise ValueError(
                f"stateful replay [{name}] bar {bar.bar_date}: "
                f"integer_shares needs a positive close price for {tk}; "
                f"got {p!r}. Supply per-bar prices (the WF loader stamps "
                f"ticker_forward_returns.close_price) — no silent "
                f"fractional fallback."
            )
        return float(p)

    # ── Sell legs: floor the delta; full liquidation at target ≈ 0 ──
    current_w = np.array(
        [state.position_value(tk) / pv_base for tk in tickers], dtype=float,
    )
    for i, tk in enumerate(tickers):
        cur_val = state.position_value(tk)
        if cur_val <= 1e-12 or target_w[i] >= current_w[i] - 1e-12:
            continue
        p = _price_of(i, tk)
        if target_w[i] <= 1e-12:
            sell_value = cur_val                      # full liquidation
        else:
            cur_shares = state.position_shares(tk)
            sell_shares = min(
                int((current_w[i] - target_w[i]) * pv_base / p + 1e-9),
                int(cur_shares + 0.5),
            )
            if sell_shares < 1:
                continue
            sell_value = sell_shares * p
        tax_total += _sell_from_lots(
            state, tk, sell_value, bar.bar_date, bar_index, conv,
        )
        traded += sell_value

    # ── Buy plan: round-down main pass + deferred rescue + recheck ──
    realized = np.array(
        [state.position_value(tk) / pv_base for tk in tickers], dtype=float,
    )
    # Investable headroom = cash minus the snapshot cash reserve (RFC
    # §2.3 "remaining investable headroom"; task_selection convention).
    headroom = max(float(state.cash) - float(snap.cash_reserve) * pv_base, 0.0)
    raws = [
        shrunk_kelly_raw(
            float(bar.mu[i]), float(bar.sigma[i]),
            kelly_fraction=_CONVICTION_KELLY_FRACTION,
            mu_shrinkage=_CONVICTION_MU_SHRINKAGE,
        )
        for i in range(n)
    ]
    buy_order = sorted(
        (i for i in range(n) if target_w[i] - realized[i] > 1e-12),
        key=lambda i: (-raws[i], tickers[i]),
    )
    price_by_i = {i: _price_of(i, tickers[i]) for i in buy_order}
    cap = np.asarray(snap.w_upper_hard, dtype=float).copy()
    if conv.enforce_caps:
        cap = np.minimum(cap, conv.per_name_cap)
    bought: dict[int, int] = {i: 0 for i in buy_order}

    # Main pass — floor of the remaining delta, conviction order,
    # headroom-aware (mirrors governor_sizing._fill_buys main pass).
    for i in buy_order:
        p = price_by_i[i]
        need_w = target_w[i] - realized[i]
        shares = min(int(need_w * pv_base / p + 1e-9),
                     int((headroom + 1e-6) / p))
        if shares >= 1:
            bought[i] += shares
            realized[i] += shares * p / pv_base
            headroom -= shares * p

    # Deferred one-share rescue sweeps (governor_sizing residual pass /
    # S6 A-3): leftover headroom only, conviction order, cap-bounded.
    n_rescue = 0
    progressed = True
    while progressed:
        progressed = False
        for i in buy_order:
            if realized[i] >= target_w[i] - 1e-12:
                continue
            p = price_by_i[i]
            if p > headroom + 1e-6:
                continue
            if realized[i] + p / pv_base > cap[i] + 1e-6:
                continue
            bought[i] += 1
            realized[i] += p / pv_base
            headroom -= p
            n_rescue += 1
            progressed = True

    # Post-round recheck — cap down violating BUYS, one share at a
    # time, lowest conviction first (deterministic tiebreak). Cash
    # (incl. reserve) holds by construction: every fill above was
    # bounded by the reserve-adjusted headroom.
    n_capdown = 0

    def _remove_one(i: int) -> None:
        nonlocal headroom, n_capdown
        p = price_by_i[i]
        bought[i] -= 1
        realized[i] -= p / pv_base
        headroom += p
        n_capdown += 1

    # 1. Single-name cap.
    for i in buy_order:
        while realized[i] > cap[i] + 1e-9 and bought[i] >= 1:
            _remove_one(i)
    # 2. Sector caps — snapshot families + D6 map when enforce_caps.
    groups: list[tuple[list[int], float]] = []
    if snap.sector_indicator is not None and snap.sector_cap_vec is not None:
        for row in range(snap.sector_indicator.shape[0]):
            members = [int(j) for j in np.where(snap.sector_indicator[row] > 0.5)[0]]
            if members:
                groups.append((members, float(snap.sector_cap_vec[row])))
    if conv.enforce_caps and conv.sector_map:
        by_sec: dict[str, list[int]] = {}
        for j, tk in enumerate(tickers):
            sec = conv.sector_map.get(tk)
            if sec and isinstance(sec, str):
                by_sec.setdefault(sec, []).append(j)
        for sec in sorted(by_sec):
            groups.append((by_sec[sec], float(conv.sector_cap)))
    for members, gcap in groups:
        while float(realized[members].sum()) > gcap + 1e-9:
            removable = [j for j in members if bought.get(j, 0) >= 1]
            if not removable:
                break   # carried-drift breach, not an order — stays
                        # visible via the violation accounting.
            _remove_one(min(removable, key=lambda k: (raws[k], tickers[k])))
    # 3. Correlation-pair caps (snapshot convention: (i, j, cap)).
    for trip in snap.corr_group_pairs or ():
        try:
            a, b, pcap = int(trip[0]), int(trip[1]), float(trip[2])
        except (TypeError, IndexError, ValueError):
            continue
        if not (0 <= a < n and 0 <= b < n):
            continue
        while float(realized[a] + realized[b]) > pcap + 1e-9:
            removable = [j for j in (a, b) if bought.get(j, 0) >= 1]
            if not removable:
                break
            _remove_one(min(removable, key=lambda k: (raws[k], tickers[k])))

    # ── Execute the surviving buys ───────────────────────────────────
    for i in buy_order:
        if bought[i] < 1:
            continue
        tk = tickers[i]
        p = price_by_i[i]
        invest = bought[i] * p
        if tk not in state.internal_price:
            # (Re-)entry anchors the internal price to the session
            # close (D6 fill convention).
            state.internal_price[tk] = p
        state.lots.setdefault(tk, []).append(
            TaxLot(
                entry_date=bar.bar_date,
                entry_bar_index=bar_index,
                cost_basis=invest,
                market_value=invest,
                entry_price=p,
                shares=float(bought[i]),
            )
        )
        state.cash -= invest
        traded += invest

    executed_w = np.array(
        [state.position_value(tk) / pv_base for tk in tickers], dtype=float,
    )
    return executed_w, traded, tax_total, n_rescue, n_capdown


def _replay_one_allocator_stateful(
    name: str,
    allocator: AllocatorFn,
    bars: Sequence[AllocatorReplayBar],
    conv: ReplayConventions,
) -> ReplayResult:
    """D6 stateful replay: carry positions / tax lots / cash across
    sessions within one arm.

    Accounting conventions (documented, exact):

    * **PV accounting** — ``PV = cash + Σ lot market values`` at all
      times. The per-session net return is ``PV_close / PV_open − 1``;
      costs and taxes flow through cash, so they are embedded in the
      return series by construction (cash-conservation invariant, D6
      test requirement).
    * **Returns-consistent pricing** — held positions are marked by the
      same per-bar ``fwd_return`` the stateless harness uses. Under the
      integer-shares convention, the per-ticker internal price is
      anchored to the session close price at (re-)entry and then evolves
      by ``fwd_return``, so ``shares × price ≡ market value`` exactly.
      (Deviation from a pure close-to-close mark when sessions are
      non-contiguous is a documented limitation; it keeps the stateful
      and stateless arms driven by the identical return series.)
    * **Off-universe forced liquidation** — a carried position whose
      ticker is absent from the current session's universe is sold at
      its carried value (zero-return exit) with cost + tax charged, and
      counted in ``off_universe_liquidations``. This keeps the budget
      the allocator sees exact (it can only reason about the session's
      tickers).
    * **Deployed fraction** — ``Σ position values / PV`` measured
      post-trade each session: a REAL state variable, no longer ≡
      turnover (#445 gap 3).
    * **Hysteresis** — the session snapshot's ``w_current`` is replaced
      with the carried weights before the allocator is called, so
      no-trade bands / hysteresis are evaluable.
    """
    res = ReplayResult(name=name, bars=len(bars))
    res.deployed_fraction = []
    res.cost_paid = []
    res.cash_series = []
    res.positions_value_series = []
    res.off_universe_liquidations = 0
    if conv.tax:
        res.tax_paid = []
    if conv.integer_shares:
        res.executed_exposure = []
        res.integer_residual = []
        res.rescue_buys = []
        res.recheck_capdowns = []
    if conv.enforce_caps:
        res.name_cap_breaches = []
        res.sector_cap_breaches = []

    state = PortfolioState(cash=float(conv.initial_capital))

    for t, bar in enumerate(bars):
        pv_open = state.portfolio_value
        bar_tickers = bar.snap.tickers
        bar_ticker_set = set(bar_tickers)
        session_tax = 0.0
        session_traded = 0.0

        # ── 1. Off-universe forced liquidation (zero-return exit) ──
        for held in sorted(set(state.lots) - bar_ticker_set):
            value = state.position_value(held)
            session_tax += _sell_from_lots(
                state, held, value, bar.bar_date, t, conv,
            )
            session_traded += value
            res.off_universe_liquidations += 1

        # ── 2. Carried weights → session snapshot ──────────────────
        pv_base = state.portfolio_value
        if pv_base <= 0:
            raise ValueError(
                f"stateful replay [{name}] bar {bar.bar_date}: portfolio "
                f"value {pv_base} <= 0 — accounting is no longer meaningful."
            )
        w_current = np.array(
            [state.position_value(tk) / pv_base for tk in bar_tickers],
            dtype=float,
        )
        snap = dataclasses.replace(bar.snap, w_current=w_current)

        # ── 3. Allocator sees the carried state ────────────────────
        alloc = _call_allocator(allocator, snap, bar)
        if alloc.status == "no_candidates":
            res.fallback_to_no_candidates += 1
        target_w = np.asarray(alloc.target_w, dtype=float)

        # ── 4. D6 §4 in-arm caps (down-only projection) ────────────
        if conv.enforce_caps:
            target_w, n_name, n_sector = apply_d6_cap_projection(
                target_w, bar_tickers, conv,
            )
            res.name_cap_breaches.append(n_name)
            res.sector_cap_breaches.append(n_sector)

        # ── 5+6. Execution ──────────────────────────────────────────
        if conv.integer_shares:
            # RFC #443 §2.3 L3: round-down + deferred one-share rescue
            # + post-round rechecks on executed quantities (mirrors the
            # merged kernel/pipeline/governor_sizing.py semantics).
            executed_w, int_traded, int_tax, n_rescue, n_capdown = (
                _execute_integer_session(
                    name, state, bar, target_w, pv_base, conv, t,
                )
            )
            session_traded += int_traded
            session_tax += int_tax
            res.executed_exposure.append(float(executed_w.sum()))
            res.integer_residual.append(
                float(target_w.sum() - executed_w.sum())
            )
            res.rescue_buys.append(n_rescue)
            res.recheck_capdowns.append(n_capdown)
        else:
            executed_dollars = target_w * pv_base
            executed_w = executed_dollars / pv_base
            for i, tk in enumerate(bar_tickers):
                cur_val = state.position_value(tk)
                delta = executed_dollars[i] - cur_val
                if delta < -1e-12:
                    session_tax += _sell_from_lots(
                        state, tk, -delta, bar.bar_date, t, conv,
                    )
                    session_traded += -delta
                elif delta > 1e-12:
                    state.lots.setdefault(tk, []).append(
                        TaxLot(
                            entry_date=bar.bar_date,
                            entry_bar_index=t,
                            cost_basis=float(delta),
                            market_value=float(delta),
                            entry_price=None,
                            shares=0.0,
                        )
                    )
                    state.cash -= float(delta)
                    session_traded += delta

        # ── 7. Linear cost: bps per side on every traded dollar ────
        session_cost = session_traded * bar.cost_per_trade_bps * 1e-4
        state.cash -= session_cost

        # ── 8. Post-trade deployed fraction (real state variable) ──
        positions_post = state.total_positions_value()
        pv_post = state.cash + positions_post
        res.deployed_fraction.append(
            positions_post / pv_post if pv_post > 0 else 0.0
        )

        # ── 9. Feasibility accounting vs the session snapshot ──────
        _record_family_violations(
            res, snap, executed_w, executed_w - w_current,
        )

        # ── 10. Mark positions with the session's realised return ──
        for i, tk in enumerate(bar_tickers):
            lots = state.lots.get(tk)
            if not lots:
                continue
            growth = 1.0 + float(bar.fwd_return[i])
            for lot in lots:
                lot.market_value *= growth
            if tk in state.internal_price:
                state.internal_price[tk] *= growth

        # ── 11. Close the session ───────────────────────────────────
        pv_close = state.portfolio_value
        daily = pv_close / pv_open - 1.0
        res.daily_returns_net.append(daily)
        res.turnover.append(session_traded / pv_open if pv_open > 0 else 0.0)
        res.cost_paid.append(session_cost)
        if conv.tax:
            res.tax_paid.append(session_tax)
        res.cash_series.append(state.cash)
        res.positions_value_series.append(state.total_positions_value())
        if bar.regime is not None:
            res.per_regime.setdefault(bar.regime, []).append(daily)

    res.final_state = state
    return res


def replay_all(
    allocators: dict[str, AllocatorFn],
    bars: Sequence[AllocatorReplayBar],
    conventions: Optional[ReplayConventions] = None,
) -> dict[str, ReplayResult]:
    """Run every allocator over the same bar sequence.

    Returns ``{name: ReplayResult}``. The bar sequence is shared so
    downstream paired-daily-returns + DSR / PBO comparisons key off
    a consistent input. ``conventions`` (opt-in) engages the D6
    stateful / tax / integer-share / cap-enforcement conventions; the
    default ``None`` preserves the original behavior exactly.
    """
    out: dict[str, ReplayResult] = {}
    for name, fn in allocators.items():
        out[name] = replay_one_allocator(name, fn, bars, conventions)
    return out


def paired_daily_returns(
    results: dict[str, ReplayResult],
) -> dict[str, np.ndarray]:
    """Return ``{name: np.ndarray}`` aligned by bar index.

    All result objects must have the same ``bars`` count (i.e. they
    were produced by ``replay_all`` over the same bar sequence) —
    otherwise paired daily returns are not well-defined.
    """
    if not results:
        return {}
    bar_counts = {name: r.bars for name, r in results.items()}
    if len(set(bar_counts.values())) != 1:
        raise ValueError(
            f"Allocators produced different bar counts: {bar_counts}. "
            "Paired daily returns are only valid for results from the "
            "same bar sequence."
        )
    return {
        name: np.asarray(r.daily_returns_net, dtype=float)
        for name, r in results.items()
    }
