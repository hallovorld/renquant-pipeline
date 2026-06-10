"""E3 breadth restoration + E4 short-sleeve value (IC→Sharpe RFC §5).

E3 — does the hard admission floor waste breadth?
    Run A2 long-only with the floor ON vs OFF; report Sharpe, TC, and
    **effective breadth** per bar. RFC §2.1/§5 insists on effective
    breadth, not a naive name-count: here it is the participation ratio
    PR = (Σ|w|)² / Σw² — the Grinold-Kahn "number of effective bets"
    (for K equal-weight names PR = K; concentration drives it below the
    name count). Honest limitation: this measures *weight* concentration,
    not the cross-sectional-correlation eigenvalue count the RFC's ideal
    asks for — the replay bar carries no full Σ. The proxy is monotone in
    the quantity of interest (how many independent positions the book
    actually expresses) and is labelled ``effective_breadth_participation``
    so it is never confused with the correlation-eigenvalue version.

E4 — is the short sleeve worth its cost at this account?
    Compare A1 (dollar-neutral L/S), A2 (long-only), and a 130/30
    long-biased book, charging a borrow fee on the short exposure. RFC
    expectation: likely NO at current NAV — but a *measured* decision, not
    an assumption. Reports Sharpe net of borrow + per-trade cost, gross
    and net exposure, and short-sleeve drag.

Both reuse the E1 §A.6 storage writer; every row carries
``basis='replay_net_of_cost'`` (gross of tax) plus the experiment-
specific cost notes.
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence

import numpy as np

from renquant_pipeline.kernel.portfolio_qp.allocator_replay import AllocatorReplayBar
from renquant_pipeline.kernel.portfolio_qp.alpha_portfolio import (
    MEASUREMENT_PREFIX,
    alpha_proportional_long_short,
    alpha_tilt_long_only,
    transfer_coefficient,
)
from renquant_pipeline.kernel.portfolio_qp.baseline_allocators import AllocatorResult
from renquant_pipeline.kernel.portfolio_qp.constraint_snapshot import ConstraintSnapshot
from renquant_pipeline.kernel.portfolio_qp.e1_tc_decomposition import (
    AdmissionFloorWrapper,
)


# ── shared metrics ───────────────────────────────────────────────────────────

def participation_ratio(weights: np.ndarray) -> Optional[float]:
    """Effective number of bets = (Σ|w|)² / Σw².  None if all-cash."""
    w = np.asarray(weights, dtype=float)
    w = w[np.isfinite(w)]
    l1 = float(np.abs(w).sum())
    l2sq = float((w ** 2).sum())
    if l2sq < 1e-18:
        return None
    return (l1 * l1) / l2sq


def _z_to_130_30(snap: ConstraintSnapshot, mu: np.ndarray) -> AllocatorResult:
    """130/30 long-biased book from z(μ̂): +130% long, −30% short, net 100%.

    Equal-weight within each leg over the sign of the demeaned z-score,
    so it is the simplest long-biased extension of A0/A1 (NOT a production
    candidate — it holds shorts; measurement only).
    """
    from renquant_pipeline.kernel.portfolio_qp.alpha_portfolio import (
        cross_sectional_zscore,
    )
    n = snap.n
    finite = np.isfinite(np.asarray(mu, dtype=float))
    z = cross_sectional_zscore(mu)
    active = np.zeros(n)
    if finite.any():
        active[finite] = z[finite] - float(z[finite].mean())
    longs = active > 0
    shorts = active < 0
    target = np.zeros(n)
    if longs.any():
        target[longs] = active[longs] / float(active[longs].sum()) * 1.30
    if shorts.any():
        neg = active[shorts]
        target[shorts] = neg / float(np.abs(neg).sum()) * 0.30
    return AllocatorResult(
        delta_w=target - snap.w_current,
        target_w=target,
        status="optimal" if (longs.any() or shorts.any()) else "no_candidates",
        selected_indices=tuple(i for i in range(n) if target[i] != 0.0),
    )


def _analyze(
    name: str,
    allocator: Callable,
    bars: Sequence[AllocatorReplayBar],
    *,
    borrow_bps_annual: float = 0.0,
) -> dict:
    """Replay one allocator collecting Sharpe/TC/breadth/exposure rows.

    Mirrors the harness arithmetic (net of per-trade cost) and additionally
    charges ``borrow_bps_annual/252`` on the absolute short exposure each
    bar — the E4 short-sleeve cost. ``borrow_bps_annual=0`` ⇒ long-only
    or cost-free comparison.
    """
    daily: list[float] = []
    turnover: list[float] = []
    tc: list[float] = []
    pr: list[float] = []
    gross_exp: list[float] = []
    net_exp: list[float] = []
    short_drag: list[float] = []
    for bar in bars:
        try:
            alloc = allocator(bar.snap, mu=bar.mu, sigma=bar.sigma)
        except TypeError:
            alloc = allocator(bar.snap, mu=bar.mu)
        w = alloc.target_w
        gross = float(np.sum(w * bar.fwd_return))
        turn = float(np.sum(np.abs(alloc.delta_w)))
        cost = turn * bar.cost_per_trade_bps * 1e-4
        short_exposure = float(np.sum(np.abs(w[w < 0])))
        borrow = short_exposure * (borrow_bps_annual * 1e-4) / 252.0
        daily.append(gross - cost - borrow)
        turnover.append(turn)
        signal = alpha_proportional_long_short(bar.snap, mu=bar.mu)
        t = transfer_coefficient(w, signal.target_w)
        if t is not None:
            tc.append(t)
        p = participation_ratio(w)
        if p is not None:
            pr.append(p)
        gross_exp.append(float(np.sum(np.abs(w))))
        net_exp.append(float(np.sum(w)))
        short_drag.append(borrow)

    arr = np.asarray(daily, dtype=float)
    sharpe = (
        float(np.mean(arr) / np.std(arr, ddof=1) * np.sqrt(252.0))
        if len(arr) > 1 and float(np.std(arr, ddof=1)) > 1e-12 else None
    )
    return {
        "allocator": name,
        "basis": "replay_net_of_cost",
        "bars": len(bars),
        "sharpe_annual": sharpe,
        "cumulative_return": float(np.prod(1.0 + arr) - 1.0) if len(arr) else 0.0,
        "tc_mean": float(np.mean(tc)) if tc else None,
        "effective_breadth_participation": float(np.mean(pr)) if pr else None,
        "mean_gross_exposure": float(np.mean(gross_exp)) if gross_exp else 0.0,
        "mean_net_exposure": float(np.mean(net_exp)) if net_exp else 0.0,
        "mean_turnover": float(np.mean(turnover)) if turnover else 0.0,
        "mean_borrow_drag_daily": float(np.mean(short_drag)) if short_drag else 0.0,
        "borrow_bps_annual": borrow_bps_annual,
    }


# ── E3 ───────────────────────────────────────────────────────────────────────

def run_e3(
    bars: Sequence[AllocatorReplayBar],
    *,
    floor_quantile: float = 0.55,
) -> list[dict]:
    """A2 long-only with the admission floor OFF vs ON.

    The breadth delta between the two rows is the quantified answer to
    "the rank floor collapses 142→29 names" (RFC §2.2 candidate 2).
    """
    floored = AdmissionFloorWrapper(alpha_tilt_long_only, floor_quantile=floor_quantile)
    return [
        {**_analyze("A2_no_floor", alpha_tilt_long_only, bars), "experiment": "E3",
         "floor_quantile": None},
        {**_analyze("A2_with_floor", floored, bars), "experiment": "E3",
         "floor_quantile": floor_quantile},
    ]


# ── E4 ───────────────────────────────────────────────────────────────────────

def run_e4(
    bars: Sequence[AllocatorReplayBar],
    *,
    borrow_bps_annual: float = 100.0,
) -> list[dict]:
    """A2 long-only vs A1 dollar-neutral vs 130/30, with borrow cost.

    The short books (A1, 130/30) pay ``borrow_bps_annual`` on their short
    exposure; A2 pays none. The Sharpe spread net of borrow is the
    measured short-sleeve value at this NAV (RFC §5/E4 — expected NO at
    $10k, but quantified).
    """
    rows = [
        {**_analyze("A2_long_only", alpha_tilt_long_only, bars, borrow_bps_annual=0.0),
         "experiment": "E4", "book": "long_only"},
        {**_analyze("A1_dollar_neutral", alpha_proportional_long_short, bars,
                    borrow_bps_annual=borrow_bps_annual),
         "experiment": "E4", "book": "dollar_neutral_ls"},
        {**_analyze("E4_130_30", _z_to_130_30, bars, borrow_bps_annual=borrow_bps_annual),
         "experiment": "E4", "book": "130_30"},
    ]
    return rows


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover — thin CLI
    import argparse
    import json
    import sys
    from datetime import datetime, timezone
    from pathlib import Path

    from renquant_pipeline.kernel.portfolio_qp.e1_tc_decomposition import (
        _git_sha,
        _sha256,
    )
    from renquant_pipeline.kernel.portfolio_qp.wf_replay_loader import (
        load_replay_bars_from_sim_db,
    )

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sim-db", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--fwd-horizon-days", type=int, default=1)
    p.add_argument("--cost-bps", type=float, default=5.0)
    p.add_argument("--floor-quantile", type=float, default=0.55)
    p.add_argument("--borrow-bps-annual", type=float, default=100.0)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--repo-pin", action="append", default=[])
    args = p.parse_args(argv)

    bars = load_replay_bars_from_sim_db(
        args.sim_db, args.start, args.end,
        fwd_horizon_days=args.fwd_horizon_days, cost_per_trade_bps=args.cost_bps,
    )
    if not bars:
        print("no bars loaded — check DB coverage", file=sys.stderr)
        return 2

    rows = run_e3(bars, floor_quantile=args.floor_quantile) + run_e4(
        bars, borrow_bps_annual=args.borrow_bps_annual,
    )
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(args.out_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    cols = sorted({k for r in rows for k in r})
    csv_path = run_dir / "e3_e4_results.csv"
    lines = [",".join(cols)]
    for r in rows:
        lines.append(",".join("" if r.get(c) is None else str(r.get(c)) for c in cols))
    csv_path.write_text("\n".join(lines) + "\n")

    pins = {}
    for spec in args.repo_pin:
        name, _, path = spec.partition("=")
        sha = _git_sha(Path(path)) if path else None
        if sha:
            pins[name] = sha
    manifest = {
        "experiment": "E3+E4",
        "run_id": run_id,
        "command": " ".join(sys.argv),
        "params": {
            "fwd_horizon_days": args.fwd_horizon_days, "cost_bps": args.cost_bps,
            "floor_quantile": args.floor_quantile,
            "borrow_bps_annual": args.borrow_bps_annual,
        },
        "input": {"sim_db": str(args.sim_db), "start": args.start,
                  "end": args.end, "n_bars": len(bars)},
        "repo_pins": pins,
        "outputs": {csv_path.name: _sha256(csv_path)},
        "basis": "replay_net_of_cost",
        "rfc": "renquant-orchestrator doc/research/2026-06-10-ic-to-pnl-architecture.md",
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    for r in rows:
        print(f"{r['experiment']:3s} {r['allocator']:20s} sharpe={r['sharpe_annual']} "
              f"breadth={r.get('effective_breadth_participation')} tc={r['tc_mean']}")
    print(f"run dir: {run_dir}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
