"""E2 — holding-horizon sweep + horizon-held rebalancing (IC→Sharpe RFC §5/E2).

Two deliverables:

1. :class:`HorizonHeldWrapper` — fixes the E1-v1 spec deviation noted in
   run ``20260610T165049Z``: the replay harness calls the allocator every
   bar, which silently turned A0 ("rebalance at horizon") into a
   daily-rebalanced book. The wrapper re-solves the base allocator every
   ``hold_bars`` bars and HOLDS the book in between, so turnover and the
   harvested signal horizon match the RFC definition.

2. :func:`run_e2` — the IC-decay sweep: the same Stage-A book held for
   {20, 40, 60, 90} bars (configurable), measuring Sharpe / TC / turnover
   per horizon (Qian-Hua-Sorensen IC-decay). The horizon whose held book
   earns the most per unit turnover is the empirically right rebalance
   cadence for the signal — confirming (or refuting) the 60d label choice.

Basis discipline: identical to E1 — every row carries
``basis='replay_net_of_cost'`` (gross of tax); storage via the E1 §A.6
writer (one RUN_ID dir, traces + tidy CSV + fingerprinted manifest).
"""
from __future__ import annotations

import dataclasses
from typing import Callable, Optional, Sequence

import numpy as np

from renquant_pipeline.kernel.portfolio_qp.allocator_replay import AllocatorReplayBar
from renquant_pipeline.kernel.portfolio_qp.alpha_portfolio import (
    MEASUREMENT_PREFIX,
    decile_long_short,
)
from renquant_pipeline.kernel.portfolio_qp.baseline_allocators import AllocatorResult
from renquant_pipeline.kernel.portfolio_qp.e1_tc_decomposition import (
    E1StepResult,
    replay_with_observer,
)


class HorizonHeldWrapper:
    """Re-solve the base every ``hold_bars`` bars; hold the book between.

    **Ticker-keyed holding (not index-keyed).** A real horizon-held book
    holds *positions in names*, and the bar universe changes day to day
    (names enter/leave on coverage). An index-keyed hold silently
    misaligns when the universe membership changes (same n, different
    tickers) and degenerates to daily rebalancing when n changes — which
    made every E2 horizon collapse to the daily result on the PatchTST
    clean-signal bars (found 2026-06-10). This wrapper instead remembers
    the held book as a ``{ticker: weight}`` map and projects it onto each
    bar's tickers between rebalances:

      * a held name still present → keep its held weight (Δw = 0 vs the
        wrapper's own previous projection on that name);
      * a held name that left the universe → its position is gone (sold);
      * a new name → 0 weight until the next rebalance.

    **Path-consistent turnover.** Δw is reported against the wrapper's own
    previous per-ticker book, so a fully-held bar with a stable universe
    trades nothing, and only genuine rebalances or universe churn cost
    turnover. State advances via ``observe`` (one call per bar from
    ``replay_with_observer``) — no lookahead.
    """

    def __init__(self, base: Callable, *, hold_bars: int):
        if hold_bars < 1:
            raise ValueError(f"hold_bars must be >= 1, got {hold_bars}")
        self.base = base
        self.hold_bars = int(hold_bars)
        self._bar_index = 0
        self._held_by_ticker: Optional[dict[str, float]] = None
        self._prev_by_ticker: dict[str, float] = {}

    def _project(self, snap, held: dict[str, float]) -> np.ndarray:
        """Map a {ticker: weight} book onto this bar's ticker order."""
        return np.array(
            [float(held.get(t, 0.0)) for t in snap.tickers], dtype=float
        )

    def _result(self, snap, target: np.ndarray, status: str) -> AllocatorResult:
        # Δw against the wrapper's own previous per-ticker book (0 for
        # names it has never held / that have left).
        prev_vec = np.array(
            [float(self._prev_by_ticker.get(t, 0.0)) for t in snap.tickers],
            dtype=float,
        )
        res = AllocatorResult(
            delta_w=target - prev_vec,
            target_w=target.copy(),
            status=status,
            selected_indices=tuple(
                i for i in range(len(target)) if target[i] != 0.0
            ),
        )
        self._prev_by_ticker = {
            t: float(target[i]) for i, t in enumerate(snap.tickers)
            if target[i] != 0.0
        }
        return res

    def __call__(self, snap, *, mu, sigma=None) -> AllocatorResult:
        rebalance = (
            self._bar_index % self.hold_bars == 0 or self._held_by_ticker is None
        )
        if rebalance:
            base_res = self.base(snap, mu=mu, sigma=sigma)
            self._held_by_ticker = {
                t: float(base_res.target_w[i])
                for i, t in enumerate(snap.tickers)
                if base_res.target_w[i] != 0.0
            }
            return self._result(snap, base_res.target_w.copy(), base_res.status)
        # held: project the remembered book onto this bar's universe
        target = self._project(snap, self._held_by_ticker)
        return self._result(snap, target, "optimal")

    def observe(self, bar: AllocatorReplayBar, daily_net_return: float) -> None:  # noqa: ARG002
        self._bar_index += 1
        inner = getattr(self.base, "observe", None)
        if inner is not None:
            inner(bar, daily_net_return)


def run_e2(
    bars: Sequence[AllocatorReplayBar],
    *,
    horizons: Sequence[int] = (20, 40, 60, 90),
    base_factory: Callable[[], Callable] = lambda: decile_long_short,
    name_prefix: str = f"{MEASUREMENT_PREFIX}A0_decile_ls",
) -> list[E1StepResult]:
    """Sweep holding horizons for the same Stage-A base book.

    Returns one :class:`E1StepResult` per horizon (step = horizon in
    bars) so E1's §A.6 writer persists it unchanged with
    ``experiment='E1'`` replaced by the caller's label in the manifest.
    """
    out: list[E1StepResult] = []
    for h in horizons:
        wrapper = HorizonHeldWrapper(base_factory(), hold_bars=int(h))
        name = f"{name_prefix}_hold{h}"
        replay, tc = replay_with_observer(name, wrapper, bars)
        out.append(E1StepResult(step=int(h), name=name, replay=replay, tc_per_bar=tc))
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover — thin CLI
    import argparse
    import sys
    from pathlib import Path

    from renquant_pipeline.kernel.portfolio_qp.e1_tc_decomposition import (
        _git_sha,
        write_results,
    )
    from renquant_pipeline.kernel.portfolio_qp.wf_replay_loader import (
        load_replay_bars_from_sim_db,
    )

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sim-db", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--fwd-horizon-days", type=int, default=1,
                   help="Per-bar realised-return horizon for the harness "
                        "arithmetic. Default 1 — daily returns; the E1-v1 "
                        "run showed 60d-overlap inflation otherwise.")
    p.add_argument("--cost-bps", type=float, default=5.0)
    p.add_argument("--horizons", type=int, nargs="+", default=[20, 40, 60, 90])
    p.add_argument("--out-dir", required=True)
    p.add_argument("--repo-pin", action="append", default=[])
    args = p.parse_args(argv)

    bars = load_replay_bars_from_sim_db(
        args.sim_db, args.start, args.end,
        fwd_horizon_days=args.fwd_horizon_days,
        cost_per_trade_bps=args.cost_bps,
    )
    if not bars:
        print("no bars loaded — check DB coverage", file=sys.stderr)
        return 2
    label = f"{args.start}..{args.end}"
    results = run_e2(bars, horizons=args.horizons)
    pins = {}
    for spec in args.repo_pin:
        name, _, path = spec.partition("=")
        sha = _git_sha(Path(path)) if path else None
        if sha:
            pins[name] = sha
    paths = write_results(
        Path(args.out_dir), results,
        windows_label=label,
        params={
            "experiment": "E2",
            "horizons": list(args.horizons),
            "fwd_horizon_days": args.fwd_horizon_days,
            "cost_bps": args.cost_bps,
        },
        input_descriptor={
            "sim_db": str(args.sim_db), "start": args.start,
            "end": args.end, "n_bars": len(bars),
        },
        repo_pins=pins,
    )
    for r in results:
        print(f"hold={r.step:3d} {r.name:48s} sharpe={r.replay.sharpe_annual} "
              f"turnover={r.replay.mean_turnover:.4f} tc={r.tc_mean}")
    print(f"run dir: {paths['run_dir']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
