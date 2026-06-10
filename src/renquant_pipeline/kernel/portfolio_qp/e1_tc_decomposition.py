"""E1 — transfer-coefficient decomposition ladder (IC→Sharpe RFC §5/E1).

Runs the RFC's constraint ladder over a shared bar sequence, measuring at
every step the annualised Sharpe AND the per-date transfer coefficient
(§3.2 definition), so the single step that destroys the most information
is identified empirically instead of asserted:

====  ====================================================  =============
step  book                                                  isolates
====  ====================================================  =============
0     A0 rank-decile L/S, zero trading costs                IC ceiling
1     A0 + realistic per-trade costs                        cost drag
2     A2 long-only α-tilt (+costs)                          long-only tax
3     step 2 + vol-target/drawdown scalar overlay           H-B test
4     step 3 + admission floor (top-quantile μ̂ only)        floor tax
5     step 4 + previous-day single-day-loss stop            stop tax
6     current QP allocator (production incumbent)           full stack
====  ====================================================  =============

Caveats encoded here on purpose (RFC §A.5 credibility discipline):

- The admission floor is a **cross-sectional-quantile proxy** for the
  production ``min_rank_score=0.55`` gate (replay bars carry μ̂, not the
  calibrated rank). The quantile is configurable and reported in the
  manifest.
- The single-day-loss stop uses the **previous bar's realised return**
  (no lookahead): a name whose prior-day return breached the threshold
  is force-flattened for ``reentry_bars`` bars.
- The harness Sharpe is net of per-trade cost, gross of tax — the
  replay layer has no tax ledger. Tax-basis comparisons live in the WF
  sim path; every output row carries ``basis='replay_net_of_cost'`` so
  numbers cannot be silently mixed with sim annual-net/event-level
  figures (the §A.4 lesson).

Storage follows RFC §A.6: one RUN_ID directory of raw per-step traces, a
committed-shape run manifest (command, pins, input/output sha256s) and a
tidy results CSV — every number one command away from its evidence.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import platform
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np

from renquant_pipeline.kernel.portfolio_qp.allocator_replay import (
    AllocatorReplayBar,
    ReplayResult,
    check_snapshot_feasibility,
)
from renquant_pipeline.kernel.portfolio_qp.alpha_portfolio import (
    MEASUREMENT_PREFIX,
    alpha_proportional_long_short,
    alpha_tilt_long_only,
    decile_long_short,
    transfer_coefficient,
)
from renquant_pipeline.kernel.portfolio_qp.baseline_allocators import (
    AllocatorResult,
    current_qp_allocator,
)


# ── stateful ladder wrappers ─────────────────────────────────────────────────

class VolTargetDrawdownOverlay:
    """Stage-B scalar overlay: uniform positive per-date multiplier.

    scale_t = clip(target_vol / realized_vol_{t-window..t-1}, floor, ceil)
              × max(dd_floor, 1 − drawdown_t / dd_max)

    State is built ONLY from the wrapper's own past net returns (no
    lookahead). H-B predicts the same-date cross-sectional TC of the
    wrapped book equals the base book's; E1 records both so the
    hypothesis is tested, not assumed.
    """

    def __init__(
        self,
        base: Callable,
        *,
        target_vol: float = 0.15,
        window: int = 20,
        scale_floor: float = 0.30,
        scale_ceiling: float = 1.0,
        dd_max: float = 0.20,
        dd_floor: float = 0.10,
    ):
        self.base = base
        self.target_vol = float(target_vol)
        self.window = int(window)
        self.scale_floor = float(scale_floor)
        self.scale_ceiling = float(scale_ceiling)
        self.dd_max = float(dd_max)
        self.dd_floor = float(dd_floor)
        self._returns: list[float] = []
        self._equity = 1.0
        self._peak = 1.0

    def current_scale(self) -> float:
        vol_scale = 1.0
        if len(self._returns) >= self.window:
            rv = float(np.std(np.asarray(self._returns[-self.window:]), ddof=1))
            ann = rv * float(np.sqrt(252.0))
            if ann > 1e-9:
                vol_scale = self.target_vol / ann
        vol_scale = float(np.clip(vol_scale, self.scale_floor, self.scale_ceiling))
        dd = 0.0 if self._peak <= 0 else max(0.0, 1.0 - self._equity / self._peak)
        dd_scale = max(self.dd_floor, 1.0 - dd / self.dd_max) if self.dd_max > 0 else 1.0
        return vol_scale * float(np.clip(dd_scale, 0.0, 1.0))

    def __call__(self, snap, *, mu, sigma=None) -> AllocatorResult:
        base = self.base(snap, mu=mu, sigma=sigma)
        s = self.current_scale()
        target = base.target_w * s
        return AllocatorResult(
            delta_w=target - snap.w_current,
            target_w=target,
            status=base.status,
            selected_indices=base.selected_indices,
        )

    def observe(self, bar: AllocatorReplayBar, daily_net_return: float) -> None:
        self._returns.append(float(daily_net_return))
        self._equity *= 1.0 + float(daily_net_return)
        self._peak = max(self._peak, self._equity)


class AdmissionFloorWrapper:
    """Zero μ̂ below the cross-sectional quantile before calling the base.

    Quantile proxy for the production ``min_rank_score`` admission gate
    (documented limitation — replay bars carry μ̂, not calibrated rank).
    """

    def __init__(self, base: Callable, *, floor_quantile: float = 0.55):
        self.base = base
        self.floor_quantile = float(floor_quantile)

    def __call__(self, snap, *, mu, sigma=None) -> AllocatorResult:
        mu = np.asarray(mu, dtype=float)
        finite = np.isfinite(mu)
        gated = np.full_like(mu, np.nan)
        if finite.sum() >= 2:
            cut = float(np.quantile(mu[finite], self.floor_quantile))
            keep = finite & (mu >= cut)
            gated[keep] = mu[keep]
        return self.base(snap, mu=gated, sigma=sigma)


class SingleDayLossStopWrapper:
    """Force-flatten names whose PREVIOUS bar return breached the stop.

    No lookahead: the block list is built in ``observe`` from the bar
    that has just been realised, and applies from the next bar for
    ``reentry_bars`` bars (the production ``min_reentry_days`` analogue).
    """

    def __init__(self, base: Callable, *, max_single_day_loss: float = 0.03,
                 reentry_bars: int = 5):
        self.base = base
        self.max_single_day_loss = float(max_single_day_loss)
        self.reentry_bars = int(reentry_bars)
        self._blocked_until: dict[int, int] = {}
        self._bar_index = 0

    def __call__(self, snap, *, mu, sigma=None) -> AllocatorResult:
        base = self.base(snap, mu=mu, sigma=sigma)
        target = base.target_w.copy()
        for i, until in self._blocked_until.items():
            if self._bar_index < until and i < len(target):
                target[i] = 0.0
        return AllocatorResult(
            delta_w=target - snap.w_current,
            target_w=target,
            status=base.status,
            selected_indices=base.selected_indices,
        )

    def observe(self, bar: AllocatorReplayBar, daily_net_return: float) -> None:  # noqa: ARG002
        fwd = np.asarray(bar.fwd_return, dtype=float)
        for i in range(len(fwd)):
            if np.isfinite(fwd[i]) and fwd[i] < -self.max_single_day_loss:
                self._blocked_until[i] = self._bar_index + 1 + self.reentry_bars
        self._bar_index += 1


# ── observer-aware replay (harness-compatible, additive) ─────────────────────

@dataclass
class E1StepResult:
    """ReplayResult + TC series for one ladder step."""

    step: int
    name: str
    replay: ReplayResult
    tc_per_bar: list[Optional[float]] = dataclasses.field(default_factory=list)

    @property
    def tc_mean(self) -> Optional[float]:
        vals = [t for t in self.tc_per_bar if t is not None]
        return float(np.mean(vals)) if vals else None

    @property
    def tc_std(self) -> Optional[float]:
        vals = [t for t in self.tc_per_bar if t is not None]
        return float(np.std(vals, ddof=1)) if len(vals) > 1 else None

    def to_row(self, *, windows: str) -> dict:
        r = self.replay
        return {
            "experiment": "E1",
            "step": self.step,
            "allocator": self.name,
            "windows": windows,
            "basis": "replay_net_of_cost",
            "bars": r.bars,
            "sharpe_annual": r.sharpe_annual,
            "cumulative_return": r.cumulative_return,
            "max_drawdown": r.max_drawdown,
            "mean_turnover": r.mean_turnover,
            "tc_mean": self.tc_mean,
            "tc_std": self.tc_std,
            "cap_violations": r.cap_violations,
            "is_measurement": self.name.startswith(MEASUREMENT_PREFIX),
        }


def replay_with_observer(
    name: str,
    allocator: Callable,
    bars: Sequence[AllocatorReplayBar],
) -> tuple[ReplayResult, list[Optional[float]]]:
    """Mirror of ``replay_one_allocator`` that (a) feeds realised returns
    back to stateful wrappers via ``observe`` and (b) records the per-bar
    TC against the A1 signal book. Metric arithmetic is kept identical to
    the harness so results remain comparable with step-4g outputs.
    """
    res = ReplayResult(name=name, bars=len(bars))
    tc: list[Optional[float]] = []
    for bar in bars:
        alloc = allocator(bar.snap, mu=bar.mu, sigma=bar.sigma)
        if alloc.status == "no_candidates":
            res.fallback_to_no_candidates += 1
        gross = float(np.sum(alloc.target_w * bar.fwd_return))
        turn = float(np.sum(np.abs(alloc.delta_w)))
        cost = turn * bar.cost_per_trade_bps * 1e-4
        daily = gross - cost
        fam = check_snapshot_feasibility(bar.snap, alloc.target_w, alloc.delta_w)
        for fam_name, count in fam.items():
            if count > 0:
                res.violations_per_family[fam_name] = (
                    res.violations_per_family.get(fam_name, 0) + count
                )
        if any(v > 0 for v in fam.values()):
            res.cap_violations += 1
        res.daily_returns_net.append(daily)
        res.turnover.append(turn)
        if bar.regime is not None:
            res.per_regime.setdefault(bar.regime, []).append(daily)
        signal = alpha_proportional_long_short(bar.snap, mu=bar.mu)
        tc.append(transfer_coefficient(alloc.target_w, signal.target_w))
        observe = getattr(allocator, "observe", None)
        if observe is not None:
            observe(bar, daily)
    return res, tc


# ── the ladder ───────────────────────────────────────────────────────────────

def _zero_cost(bars: Sequence[AllocatorReplayBar]) -> list[AllocatorReplayBar]:
    return [dataclasses.replace(b, cost_per_trade_bps=0.0) for b in bars]


def build_ladder(*, floor_quantile: float = 0.55,
                 max_single_day_loss: float = 0.03) -> list[tuple[int, str, Callable, bool]]:
    """(step, name, allocator_factory(), zero_cost_bars?) for each rung.

    Factories (not instances) so every run gets fresh wrapper state.
    """
    def a2_overlay():
        return VolTargetDrawdownOverlay(alpha_tilt_long_only)

    def a2_overlay_floor():
        return VolTargetDrawdownOverlay(
            AdmissionFloorWrapper(alpha_tilt_long_only, floor_quantile=floor_quantile),
        )

    def a2_overlay_floor_stop():
        return SingleDayLossStopWrapper(
            VolTargetDrawdownOverlay(
                AdmissionFloorWrapper(alpha_tilt_long_only, floor_quantile=floor_quantile),
            ),
            max_single_day_loss=max_single_day_loss,
        )

    return [
        (0, f"{MEASUREMENT_PREFIX}A0_decile_ls_zerocost", lambda: decile_long_short, True),
        (1, f"{MEASUREMENT_PREFIX}A0_decile_ls", lambda: decile_long_short, False),
        (2, "A2_alpha_tilt_long_only", lambda: alpha_tilt_long_only, False),
        (3, "A2_plus_overlay", a2_overlay, False),
        (4, "A2_plus_overlay_floor", a2_overlay_floor, False),
        (5, "A2_plus_overlay_floor_stop", a2_overlay_floor_stop, False),
        (6, "current_qp", lambda: current_qp_allocator, False),
    ]


def run_e1(
    bars: Sequence[AllocatorReplayBar],
    *,
    windows_label: str,
    floor_quantile: float = 0.55,
    max_single_day_loss: float = 0.03,
) -> list[E1StepResult]:
    zero = _zero_cost(bars)
    out: list[E1StepResult] = []
    for step, name, factory, use_zero in build_ladder(
        floor_quantile=floor_quantile, max_single_day_loss=max_single_day_loss,
    ):
        replay, tc = replay_with_observer(name, factory(), zero if use_zero else bars)
        out.append(E1StepResult(step=step, name=name, replay=replay, tc_per_bar=tc))
    _ = windows_label  # consumed by callers via to_row
    return out


# ── §A.6 storage ─────────────────────────────────────────────────────────────

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return f"sha256:{h.hexdigest()}"


def _git_sha(repo: Path) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def write_results(
    out_dir: Path,
    results: Sequence[E1StepResult],
    *,
    windows_label: str,
    params: dict,
    input_descriptor: dict,
    repo_pins: dict[str, str] | None = None,
) -> dict[str, Path]:
    """Persist one E1 run per RFC §A.6: raw traces + manifest + tidy CSV."""
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = out_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # raw per-step traces (append-only run dir)
    for r in results:
        trace = {
            "step": r.step,
            "name": r.name,
            "daily_returns_net": r.replay.daily_returns_net,
            "turnover": r.replay.turnover,
            "tc_per_bar": r.tc_per_bar,
            "violations_per_family": r.replay.violations_per_family,
            "per_regime": r.replay.per_regime,
        }
        (run_dir / f"step{r.step}_{r.name.replace('::', '_')}.trace.json").write_text(
            json.dumps(trace),
        )

    # tidy results CSV — the only file analyses should read
    rows = [r.to_row(windows=windows_label) for r in results]
    csv_path = run_dir / "e1_results.csv"
    cols = list(rows[0].keys())
    lines = [",".join(cols)]
    for row in rows:
        lines.append(",".join("" if row[c] is None else str(row[c]) for c in cols))
    csv_path.write_text("\n".join(lines) + "\n")

    manifest = {
        "experiment": "E1",
        "run_id": run_id,
        "command": " ".join(sys.argv),
        "python": platform.python_version(),
        "params": params,
        "input": input_descriptor,
        "repo_pins": repo_pins or {},
        "outputs": {
            p.name: _sha256(p) for p in sorted(run_dir.iterdir()) if p.is_file()
        },
        "basis": "replay_net_of_cost",
        "rfc": "renquant-orchestrator doc/research/2026-06-10-ic-to-pnl-architecture.md",
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return {"run_dir": run_dir, "csv": csv_path, "manifest": manifest_path}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    from renquant_pipeline.kernel.portfolio_qp.wf_replay_loader import (
        load_replay_bars_from_sim_db,
    )

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sim-db", required=True, help="Path to sim_runs.db")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--fwd-horizon-days", type=int, default=60)
    p.add_argument("--cost-bps", type=float, default=5.0)
    p.add_argument("--floor-quantile", type=float, default=0.55)
    p.add_argument("--max-single-day-loss", type=float, default=0.03)
    p.add_argument("--out-dir", required=True,
                   help="E1 evidence root (RFC §A.6), e.g. "
                        ".../artifacts/diagnostics/ic_to_pnl/E1")
    p.add_argument("--repo-pin", action="append", default=[],
                   help="name=path; git SHA recorded in the manifest")
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
    results = run_e1(
        bars,
        windows_label=label,
        floor_quantile=args.floor_quantile,
        max_single_day_loss=args.max_single_day_loss,
    )
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
            "fwd_horizon_days": args.fwd_horizon_days,
            "cost_bps": args.cost_bps,
            "floor_quantile": args.floor_quantile,
            "max_single_day_loss": args.max_single_day_loss,
        },
        input_descriptor={
            "sim_db": str(args.sim_db),
            "start": args.start,
            "end": args.end,
            "n_bars": len(bars),
        },
        repo_pins=pins,
    )
    for r in results:
        print(f"step {r.step} {r.name:42s} sharpe={r.replay.sharpe_annual} "
              f"tc={r.tc_mean}")
    print(f"run dir: {paths['run_dir']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
