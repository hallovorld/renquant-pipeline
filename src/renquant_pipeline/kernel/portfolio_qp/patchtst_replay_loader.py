"""Build replay bars from a clean PatchTST OOS prediction export.

Bridges the P0 clean-IC artifact (renquant-model
``doc/evidence/2026-06-10-pt07-clean-oos-ic/``, placebo-clean OOS IC
+0.0724) into the E1–E4 experiment harness, so the IC→Sharpe RFC
experiments can finally run on the **verified** signal rather than the
provenance-unknown sim-DB μ̂ (RFC §7.1 prerequisite #1, now met).

Inputs:
- ``predictions.parquet`` — (date, ticker, pred, label) from the OOS
  export; ``pred`` is the model's cross-sectional score, used as μ̂.
- ``ticker_forward_returns`` (in ``sim_runs.db``) — the **raw** realised
  per-asset forward return at the requested horizon, used as the replay
  PnL driver. Raw returns (not the standardized ``label``, per-day
  std≈1.06) keep the harness Sharpe in real return units and — at
  ``fwd_horizon_days=1`` — avoid the 60d-overlap inflation the E1-v1 run
  flagged.

Each emitted :class:`AllocatorReplayBar` carries a **minimal long-only**
:class:`ConstraintSnapshot` (zero start book, flat per-name cap, no
sector/corr/wash constraints). This is deliberate: the E1 ladder ADDS
production gates one at a time as wrappers, so the base snapshot must be
simple enough to isolate each gate's transfer-coefficient cost while still
respecting the real-money long-only mandate (``w_lower=0``). A0/A1 remain
measurement instruments that intentionally hold short legs and therefore
report long-only ``w_lower`` violations under this snapshot — expected and
documented; they carry the ``measurement::`` prefix so the promotion gate
excludes them. The ``step 6 current_qp`` rung therefore measures the
long-only QP on this signal — NOT a reproduction of a PatchTST production
decision trace (none exists; PatchTST has only ever run sell-only). That
caveat is the loader's documented limitation and must be restated in any
verdict.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np

from renquant_pipeline.kernel.portfolio_qp.allocator_replay import AllocatorReplayBar
from renquant_pipeline.kernel.portfolio_qp.constraint_snapshot import ConstraintSnapshot

_FWD_COLS = {1: "fwd_1d", 5: "fwd_5d", 10: "fwd_10d", 20: "fwd_20d", 60: "fwd_60d"}


def _sha256_file(path: "str | Path") -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _resolve_existing(path: "str | Path") -> Path:
    return Path(path).expanduser().resolve()


def validate_clean_oos_manifest(
    manifest_path: "str | Path",
    predictions_parquet: "str | Path",
) -> dict:
    """Validate the P0 clean-OOS artifact before promotion-style E1."""
    mpath = _resolve_existing(manifest_path)
    manifest = json.loads(mpath.read_text())
    if manifest.get("kind") != "patchtst_oos_ic_export":
        raise ValueError(
            f"manifest kind must be patchtst_oos_ic_export, got {manifest.get('kind')!r}"
        )
    if not bool((manifest.get("oos_contract") or {}).get("passed")):
        raise ValueError("clean-OOS manifest oos_contract.passed is not true")
    if not bool((manifest.get("sanity_battery") or {}).get("passed")):
        raise ValueError("clean-OOS manifest sanity_battery.passed is not true")

    expected = (manifest.get("outputs") or {}).get("predictions_parquet")
    if not expected:
        raise ValueError("clean-OOS manifest missing outputs.predictions_parquet")
    pred_path = _resolve_existing(predictions_parquet)
    expected_path = _resolve_existing(expected)
    if pred_path != expected_path:
        raise ValueError(
            "predictions parquet does not match clean-OOS manifest: "
            f"{pred_path} != {expected_path}"
        )

    # Chain-of-custody: a path match is not enough — a same-path swap of
    # predictions.parquet would otherwise pass. Require the upstream manifest
    # to carry the predictions CONTENT hash (renquant-model export
    # output_hashes.predictions_parquet_sha256) and verify the bytes we load
    # are byte-identical to the placebo-clean export. Fail closed if the hash
    # is absent (an older manifest is not promotion-grade) or mismatches.
    actual_sha = _sha256_file(pred_path)
    expected_sha = (manifest.get("output_hashes") or {}).get(
        "predictions_parquet_sha256"
    )
    if not expected_sha:
        raise ValueError(
            "clean-OOS manifest missing output_hashes.predictions_parquet_sha256; "
            "regenerate the export with the content-hash stamp before a "
            "promotion-grade run (renquant-model oos_ic_export)."
        )
    if actual_sha != expected_sha:
        raise ValueError(
            "predictions.parquet content hash does not match the clean-OOS "
            f"manifest: loaded {actual_sha} != manifest {expected_sha}. The "
            "predictions file was modified after the placebo-clean export."
        )

    manifest["_manifest_path"] = str(mpath)
    manifest["_manifest_sha256"] = _sha256_file(mpath)
    manifest["_predictions_sha256"] = actual_sha
    return manifest


def minimal_snapshot(tickers: tuple[str, ...], *, cap: float = 0.20) -> ConstraintSnapshot:
    """Minimal production-feasible snapshot: long-only, flat cap, no group caps.

    ``w_lower=0`` matches the real-money long-only mandate. A0/A1
    measurement books still produce short legs (they do not read the
    snapshot bounds); the harness counts those as ``w_lower`` violations,
    which is the intended signal that they are measurement instruments,
    not deployable books.
    """
    n = len(tickers)
    return ConstraintSnapshot(
        n=n,
        tickers=tickers,
        w_current=np.zeros(n),
        w_upper_hard=np.full(n, float(cap)),
        w_upper=np.full(n, float(cap)),
        w_lower=0.0,
        dw_max=np.full(n, 1.0),
        cash_reserve=0.0,
        turnover_max=None,
        drawdown=0.0,
        drawdown_limit=0.20,
        gross_max=None,
        wash_sale_mask=np.zeros(n, dtype=bool),
    )


def _load_forward_returns(
    sim_db: "str | Path",
    start: str,
    end: str,
    fwd_col: str,
) -> dict[tuple[str, str], float]:
    con = sqlite3.connect(str(sim_db))
    try:
        cur = con.execute(
            f"SELECT as_of_date, ticker, {fwd_col} FROM ticker_forward_returns "
            "WHERE as_of_date BETWEEN ? AND ? AND "
            f"{fwd_col} IS NOT NULL",
            (start, end),
        )
        out: dict[tuple[str, str], float] = {}
        for d, t, v in cur.fetchall():
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if np.isfinite(fv):
                out[(str(d), str(t))] = fv
        return out
    finally:
        con.close()


def load_patchtst_replay_bars(
    predictions_parquet: "str | Path",
    sim_db: "str | Path",
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    fwd_horizon_days: int = 1,
    cap: float = 0.20,
    cost_per_trade_bps: float = 5.0,
    min_names: int = 20,
    regime: Optional[str] = None,
) -> list[AllocatorReplayBar]:
    """Join PatchTST predictions with raw forward returns into replay bars.

    One bar per date with ≥ ``min_names`` names carrying both a prediction
    and a finite forward return. ``min_names`` defaults to 20 so the A0
    decile book always has ≥ 2 names per leg.
    """
    import pandas as pd  # noqa: PLC0415 — heavy optional dep, lazy

    if fwd_horizon_days not in _FWD_COLS:
        raise ValueError(
            f"fwd_horizon_days must be one of {sorted(_FWD_COLS)}, "
            f"got {fwd_horizon_days}"
        )
    fwd_col = _FWD_COLS[fwd_horizon_days]

    preds = pd.read_parquet(predictions_parquet)
    preds = preds[["date", "ticker", "pred"]].copy()
    preds["date"] = pd.to_datetime(preds["date"]).dt.strftime("%Y-%m-%d")
    if start:
        preds = preds[preds["date"] >= start]
    if end:
        preds = preds[preds["date"] <= end]
    if preds.empty:
        return []

    win_start = start or str(preds["date"].min())
    win_end = end or str(preds["date"].max())
    fwd = _load_forward_returns(sim_db, win_start, win_end, fwd_col)

    bars: list[AllocatorReplayBar] = []
    for date, grp in preds.groupby("date", sort=True):
        rows = [
            (str(t), float(p), fwd[(date, str(t))])
            for t, p in zip(grp["ticker"], grp["pred"])
            if (date, str(t)) in fwd and np.isfinite(float(p))
        ]
        if len(rows) < int(min_names):
            continue
        rows.sort(key=lambda r: r[0])  # stable ticker order
        tickers = tuple(r[0] for r in rows)
        mu = np.array([r[1] for r in rows], dtype=float)
        fwd_ret = np.array([r[2] for r in rows], dtype=float)
        snap = minimal_snapshot(tickers, cap=cap)
        bars.append(AllocatorReplayBar(
            bar_date=date,
            snap=snap,
            mu=mu,
            sigma=np.full(len(rows), 0.20),
            fwd_return=fwd_ret,
            regime=regime,
            cost_per_trade_bps=cost_per_trade_bps,
        ))
    return bars


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover — thin CLI
    """Promotion-grade E1 on the clean PatchTST signal (RFC §7 path).

    Loads bars from the OOS prediction export, runs the E1 ladder, and
    persists via the §A.6 writer — one command from artifact to evidence.
    """
    import argparse
    import sys

    from renquant_pipeline.kernel.portfolio_qp.e1_tc_decomposition import (
        _git_sha,
        run_e1,
        write_results,
    )
    from renquant_pipeline.kernel.portfolio_qp.e2_horizon_sweep import run_e2

    p = argparse.ArgumentParser(description=main.__doc__)
    p.add_argument("--experiment", choices=("e1", "e2"), default="e1",
                   help="e1 = TC-decomposition ladder; e2 = holding-horizon "
                        "sweep (horizon-held A0, settles the A0-cost question)")
    p.add_argument("--predictions", required=True,
                   help="PatchTST OOS predictions.parquet (date,ticker,pred)")
    p.add_argument("--clean-oos-manifest", required=True,
                   help="P0 clean-OOS manifest.json; must pass OOS + sanity gates")
    p.add_argument("--sim-db", required=True, help="sim_runs.db for forward returns")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--fwd-horizon-days", type=int, default=1)
    p.add_argument("--allow-overlap-horizon", action="store_true",
                   help="research-only: allow fwd horizons >1; output is not promotion-grade")
    p.add_argument("--cap", type=float, default=0.20)
    p.add_argument("--cost-bps", type=float, default=5.0)
    p.add_argument("--min-names", type=int, default=20)
    p.add_argument("--floor-quantile", type=float, default=0.55)
    p.add_argument("--horizons", type=int, nargs="+", default=[1, 5, 20, 40, 60],
                   help="E2 holding horizons in bars (default 1 5 20 40 60)")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--repo-pin", action="append", default=[])
    args = p.parse_args(argv)

    clean_manifest = validate_clean_oos_manifest(args.clean_oos_manifest, args.predictions)
    if args.fwd_horizon_days != 1 and not args.allow_overlap_horizon:
        print(
            "fwd_horizon_days != 1 would feed overlapping multi-day returns "
            "into a daily-return replay; pass --allow-overlap-horizon for "
            "research-only evidence.",
            file=sys.stderr,
        )
        return 2

    bars = load_patchtst_replay_bars(
        args.predictions, args.sim_db,
        start=args.start, end=args.end,
        fwd_horizon_days=args.fwd_horizon_days,
        cap=args.cap, cost_per_trade_bps=args.cost_bps, min_names=args.min_names,
    )
    if not bars:
        print("no bars loaded — check coverage", file=sys.stderr)
        return 2
    label = f"patchtst_clean_{bars[0].bar_date}..{bars[-1].bar_date}"
    if args.experiment == "e2":
        results = run_e2(bars, horizons=args.horizons)
    else:
        results = run_e1(bars, windows_label=label, floor_quantile=args.floor_quantile)
    experiment_label = args.experiment.upper()
    pins = {}
    for spec in args.repo_pin:
        name, _, path = spec.partition("=")
        sha = _git_sha(Path(path)) if path else None
        if sha:
            pins[name] = sha
    paths = write_results(
        Path(args.out_dir), results, windows_label=label,
        experiment=experiment_label,
        params={
            "experiment": experiment_label,
            "signal": "pt07_clean_oos_ic",
            "fwd_horizon_days": args.fwd_horizon_days, "cap": args.cap,
            "cost_bps": args.cost_bps, "floor_quantile": args.floor_quantile,
            "horizons": args.horizons if args.experiment == "e2" else None,
            "snapshot": "minimal long-only (not a production decision-trace reproduction)",
            "promotion_grade": bool(args.fwd_horizon_days == 1),
            "allow_overlap_horizon": bool(args.allow_overlap_horizon),
        },
        input_descriptor={
            "predictions": str(args.predictions), "sim_db": str(args.sim_db),
            "n_bars": len(bars), "window": label,
            "clean_oos_manifest": clean_manifest["_manifest_path"],
            "clean_oos_manifest_sha256": clean_manifest["_manifest_sha256"],
            "predictions_sha256": clean_manifest["_predictions_sha256"],
            "clean_oos_run_id": clean_manifest.get("run_id"),
            "clean_oos_mean_ic": (clean_manifest.get("metrics") or {}).get("mean_oos_ic"),
            "clean_oos_sanity_passed": (clean_manifest.get("sanity_battery") or {}).get("passed"),
            "clean_oos_contract_passed": (clean_manifest.get("oos_contract") or {}).get("passed"),
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
