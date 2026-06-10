"""Statistical hardening of the IC→Sharpe synthesis (RFC §5 + §7.3).

The E1–E4 verdicts rank allocators by raw Sharpe. The synthesis verdict's
robust claim is the *ordering* A2 long-only ≫ current QP — but a raw-Sharpe
ordering is not a promotion-grade statistic. This driver runs the same
deployable candidates over the **verified clean signal** and applies the
existing step-4g machinery:

- **HAC-corrected paired comparison** (current_qp vs each candidate) — the
  autocorrelation-robust significance of the per-bar return difference.
- **DSR** per candidate and **PBO** across the candidate matrix
  (Bailey-Borwein-López de Prado-Zhu 2015 CSCV) — multiple-comparison
  correction so a lucky ordering cannot masquerade as skill.
- **Per-regime stratification** (CLAUDE.md PRIME DIRECTIVE: by-regime
  first, pooled second).

Stage-A A2 is horizon-held (the E2 finding: ~3-bar cadence), so it needs
the observe-aware replay; the QP / equal-weight / inverse-vol baselines are
stateless. All run over one shared bar sequence so the paired + PBO matrix
is well-defined.

This is NOT a promotion: same caveats as the synthesis (minimal long-only
snapshot, single OOS holdout, gross of tax). It upgrades the synthesis's
"strong directional" ordering to a multiple-comparison-corrected one.
"""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from renquant_pipeline.kernel.portfolio_qp.allocator_replay import (
    AllocatorReplayBar,
    ReplayResult,
    replay_one_allocator,
)
from renquant_pipeline.kernel.portfolio_qp.alpha_portfolio import alpha_tilt_long_only
from renquant_pipeline.kernel.portfolio_qp.baseline_allocators import (
    current_qp_allocator,
    equal_weight_top_k,
    inverse_vol_top_k,
)
from renquant_pipeline.kernel.portfolio_qp.e1_tc_decomposition import (
    replay_with_observer,
)
from renquant_pipeline.kernel.portfolio_qp.e2_horizon_sweep import HorizonHeldWrapper
from renquant_pipeline.kernel.portfolio_qp.replay_significance import (
    compute_significance_verdicts,
    verdicts_to_dict,
)
from renquant_pipeline.kernel.portfolio_qp.run_ab_replay import (
    paired_comparison_metrics,
    regime_stratified_block,
)

INCUMBENT = "current_qp"


def build_candidate_results(
    bars: Sequence[AllocatorReplayBar],
    *,
    a2_hold_bars: int = 3,
) -> dict[str, ReplayResult]:
    """Replay the deployable candidate set over one shared bar sequence.

    A2 long-only is horizon-held (observe-aware); the rest are stateless.
    Returns ``{name: ReplayResult}`` ready for the significance machinery.
    """
    results: dict[str, ReplayResult] = {}

    # A2 long-only, horizon-held at the E2 cadence — needs observe.
    a2_wrapper = HorizonHeldWrapper(alpha_tilt_long_only, hold_bars=a2_hold_bars)
    a2_replay, _tc = replay_with_observer(
        f"A2_long_only_hold{a2_hold_bars}", a2_wrapper, bars,
    )
    results[f"A2_long_only_hold{a2_hold_bars}"] = a2_replay

    # Stateless baselines + the incumbent QP.
    for name, fn in (
        (INCUMBENT, current_qp_allocator),
        ("equal_weight_top_k", equal_weight_top_k),
        ("inverse_vol_top_k", inverse_vol_top_k),
    ):
        results[name] = replay_one_allocator(name, fn, bars)
    return results


def run_significance(
    bars: Sequence[AllocatorReplayBar],
    *,
    a2_hold_bars: int = 3,
    pbo_n_slices: int = 16,
) -> dict:
    """Full statistical block: DSR/PBO + HAC paired + per-regime.

    Mirrors ``run_ab_replay.run_replay`` but (a) injects the horizon-held
    A2 candidate and (b) keeps the clean-signal caveats explicit in the
    output so it cannot be mistaken for a production promotion.
    """
    results = build_candidate_results(bars, a2_hold_bars=a2_hold_bars)

    per_allocator = {
        name: {
            "sharpe_annual": r.sharpe_annual,
            "cumulative_return": r.cumulative_return,
            "max_drawdown": r.max_drawdown,
            "mean_turnover": r.mean_turnover,
            "cap_violations": r.cap_violations,
            "bars": r.bars,
        }
        for name, r in results.items()
    }

    # HAC-corrected paired comparisons vs the incumbent QP.
    aligned = {
        name: np.asarray(r.daily_returns_net, dtype=float)
        for name, r in results.items()
    }
    paired_block = {}
    if INCUMBENT in aligned:
        a = aligned[INCUMBENT]
        for name, b in aligned.items():
            if name == INCUMBENT:
                continue
            paired_block[f"{INCUMBENT}_vs_{name}"] = paired_comparison_metrics(
                a, b, name_a=INCUMBENT, name_b=name,
            )

    significance = verdicts_to_dict(
        compute_significance_verdicts(results, pbo_n_slices=pbo_n_slices)
    )
    regime_block = regime_stratified_block(results, bars)

    return {
        "experiment": "stage_a_significance",
        "incumbent": INCUMBENT,
        "a2_hold_bars": a2_hold_bars,
        "n_bars": len(bars),
        "per_allocator": per_allocator,
        "paired_vs_incumbent": paired_block,
        "significance_dsr_pbo": significance,
        "per_regime": regime_block,
        "basis": "replay_net_of_cost",
        "caveats": [
            "minimal long-only snapshot — not a production decision-trace reproduction",
            "single fixed OOS holdout — not walk-forward; promotion gates on WF + DSR/PBO",
            "1-day PnL, gross of tax — absolute Sharpes are benchmarks, orderings are the result",
        ],
        "rfc": "renquant-orchestrator doc/research/2026-06-10-ic-to-pnl-architecture.md",
    }


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
    from renquant_pipeline.kernel.portfolio_qp.patchtst_replay_loader import (
        load_patchtst_replay_bars,
        validate_clean_oos_manifest,
    )

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--predictions", required=True)
    p.add_argument("--clean-oos-manifest", required=True)
    p.add_argument("--sim-db", required=True)
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--a2-hold-bars", type=int, default=3)
    p.add_argument("--pbo-n-slices", type=int, default=16)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--repo-pin", action="append", default=[])
    args = p.parse_args(argv)

    manifest = validate_clean_oos_manifest(args.clean_oos_manifest, args.predictions)
    bars = load_patchtst_replay_bars(
        args.predictions, args.sim_db, start=args.start, end=args.end,
        fwd_horizon_days=1,
    )
    if not bars:
        print("no bars loaded — check coverage", file=sys.stderr)
        return 2

    out = run_significance(
        bars, a2_hold_bars=args.a2_hold_bars, pbo_n_slices=args.pbo_n_slices,
    )
    pins = {}
    for spec in args.repo_pin:
        name, _, path = spec.partition("=")
        sha = _git_sha(Path(path)) if path else None
        if sha:
            pins[name] = sha
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(args.out_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    result_path = run_dir / "significance.json"
    result_path.write_text(json.dumps(out, indent=2, default=str))
    manifest_out = {
        "experiment": "stage_a_significance",
        "run_id": run_id,
        "command": " ".join(sys.argv),
        "input": {
            "predictions": str(args.predictions),
            "clean_oos_manifest": manifest["_manifest_path"],
            "predictions_sha256": manifest["_predictions_sha256"],
            "n_bars": len(bars),
        },
        "repo_pins": pins,
        "outputs": {result_path.name: _sha256(result_path)},
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest_out, indent=2))

    for name, v in out["significance_dsr_pbo"].items():
        print(f"{name:28s} sharpe={v.get('sharpe_raw_annual')} "
              f"dsr={v.get('dsr')} pbo={v.get('pbo')}")
    for k, v in out["paired_vs_incumbent"].items():
        print(f"{k}: Δsharpe={v.get('delta_sharpe_annual')} "
              f"hac_t={v.get('hac_t_stat')} win_z={v.get('win_rate_a_beats_b_z_score')}")
    print(f"run dir: {run_dir}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
