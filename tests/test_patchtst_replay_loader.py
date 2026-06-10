"""PatchTST OOS predictions → replay bars (IC→Sharpe RFC, P0 bridge)."""
from __future__ import annotations

import sqlite3
import json

import numpy as np
import pytest

from renquant_pipeline.kernel.portfolio_qp.patchtst_replay_loader import (
    load_patchtst_replay_bars,
    minimal_snapshot,
    main,
    validate_clean_oos_manifest,
)


def _write_predictions(path, dates, tickers, rng):
    pd = pytest.importorskip("pandas")
    rows = []
    for d in dates:
        for t in tickers:
            rows.append({"date": d, "ticker": t,
                         "pred": float(rng.normal(0, 0.04)),
                         "label": float(rng.normal(0, 1))})
    pd.DataFrame(rows).to_parquet(path)


def _write_fwd_db(path, dates, tickers, rng, *, drop=()):
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE ticker_forward_returns "
        "(as_of_date TEXT, ticker TEXT, fwd_1d REAL, fwd_60d REAL)"
    )
    for d in dates:
        for t in tickers:
            if (d, t) in drop:
                continue
            con.execute(
                "INSERT INTO ticker_forward_returns VALUES (?,?,?,?)",
                (d, t, float(rng.normal(0, 0.01)), float(rng.normal(0, 0.05))),
            )
    con.commit()
    con.close()


def test_minimal_snapshot_is_long_only():
    snap = minimal_snapshot(("A", "B", "C"), cap=0.15)
    assert snap.n == 3
    assert (snap.w_upper_hard == 0.15).all()
    assert snap.w_lower == 0.0             # long-only mandate; A0/A1 shorts → violations
    assert snap.sector_indicator is None
    assert not snap.wash_sale_mask.any()


def test_loader_builds_one_bar_per_covered_date(tmp_path):
    rng = np.random.default_rng(1)
    dates = ["2025-03-13", "2025-03-14", "2025-03-17"]
    tickers = [f"T{i:02d}" for i in range(25)]
    _write_predictions(tmp_path / "p.parquet", dates, tickers, rng)
    _write_fwd_db(tmp_path / "sim.db", dates, tickers, rng)
    bars = load_patchtst_replay_bars(
        tmp_path / "p.parquet", tmp_path / "sim.db", min_names=20,
    )
    assert len(bars) == 3
    for b in bars:
        assert b.snap.n == 25
        assert b.mu.shape == (25,) and b.fwd_return.shape == (25,)
        assert list(b.snap.tickers) == sorted(b.snap.tickers)  # stable order


def test_loader_skips_low_coverage_dates(tmp_path):
    rng = np.random.default_rng(2)
    dates = ["2025-03-13", "2025-03-14"]
    tickers = [f"T{i:02d}" for i in range(25)]
    _write_predictions(tmp_path / "p.parquet", dates, tickers, rng)
    # drop all but 5 names on the 14th → below min_names
    drop = {("2025-03-14", f"T{i:02d}") for i in range(5, 25)}
    _write_fwd_db(tmp_path / "sim.db", dates, tickers, rng, drop=drop)
    bars = load_patchtst_replay_bars(
        tmp_path / "p.parquet", tmp_path / "sim.db", min_names=20,
    )
    assert [b.bar_date for b in bars] == ["2025-03-13"]


def test_loader_aligns_mu_and_fwd_by_ticker(tmp_path):
    pd = pytest.importorskip("pandas")
    # deterministic: pred increases with ticker index, fwd decreases —
    # confirms the join pairs the right pred with the right fwd, not by row order
    dates = ["2025-03-13"]
    tickers = [f"T{i:02d}" for i in range(20)]
    rows = [{"date": "2025-03-13", "ticker": t, "pred": float(i), "label": 0.0}
            for i, t in enumerate(tickers)]
    pd.DataFrame(rows).to_parquet(tmp_path / "p.parquet")
    con = sqlite3.connect(str(tmp_path / "sim.db"))
    con.execute("CREATE TABLE ticker_forward_returns "
                "(as_of_date TEXT, ticker TEXT, fwd_1d REAL, fwd_60d REAL)")
    for i, t in enumerate(tickers):
        con.execute("INSERT INTO ticker_forward_returns VALUES (?,?,?,?)",
                    ("2025-03-13", t, float(-i), 0.0))
    con.commit(); con.close()
    bars = load_patchtst_replay_bars(
        tmp_path / "p.parquet", tmp_path / "sim.db", min_names=20,
    )
    assert len(bars) == 1
    b = bars[0]
    # for every name, pred + fwd == 0 by construction (i + (-i))
    assert np.allclose(b.mu + b.fwd_return, 0.0)


def test_invalid_horizon_rejected(tmp_path):
    rng = np.random.default_rng(3)
    _write_predictions(tmp_path / "p.parquet", ["2025-03-13"],
                       [f"T{i:02d}" for i in range(25)], rng)
    _write_fwd_db(tmp_path / "sim.db", ["2025-03-13"],
                  [f"T{i:02d}" for i in range(25)], rng)
    with pytest.raises(ValueError, match="fwd_horizon_days"):
        load_patchtst_replay_bars(
            tmp_path / "p.parquet", tmp_path / "sim.db", fwd_horizon_days=7,
        )


def _write_clean_manifest(path, predictions_path, *, passed=True):
    path.write_text(json.dumps({
        "kind": "patchtst_oos_ic_export",
        "run_id": "test-run",
        "outputs": {"predictions_parquet": str(predictions_path.resolve())},
        "oos_contract": {"passed": passed},
        "sanity_battery": {"passed": passed},
        "metrics": {"mean_oos_ic": 0.07},
    }))


def test_manifest_validation_requires_clean_contract(tmp_path):
    rng = np.random.default_rng(4)
    pred = tmp_path / "p.parquet"
    _write_predictions(pred, ["2025-03-13"], [f"T{i:02d}" for i in range(25)], rng)
    manifest = tmp_path / "manifest.json"
    _write_clean_manifest(manifest, pred, passed=False)

    with pytest.raises(ValueError, match="passed"):
        validate_clean_oos_manifest(manifest, pred)


def test_manifest_validation_rejects_wrong_predictions_path(tmp_path):
    rng = np.random.default_rng(5)
    pred = tmp_path / "p.parquet"
    other = tmp_path / "other.parquet"
    _write_predictions(pred, ["2025-03-13"], [f"T{i:02d}" for i in range(25)], rng)
    _write_predictions(other, ["2025-03-13"], [f"T{i:02d}" for i in range(25)], rng)
    manifest = tmp_path / "manifest.json"
    _write_clean_manifest(manifest, pred)

    with pytest.raises(ValueError, match="does not match"):
        validate_clean_oos_manifest(manifest, other)


def test_cli_blocks_overlap_horizon_without_research_override(tmp_path, capsys):
    rng = np.random.default_rng(6)
    dates = ["2025-03-13"]
    tickers = [f"T{i:02d}" for i in range(25)]
    pred = tmp_path / "p.parquet"
    db = tmp_path / "sim.db"
    _write_predictions(pred, dates, tickers, rng)
    _write_fwd_db(db, dates, tickers, rng)
    manifest = tmp_path / "manifest.json"
    _write_clean_manifest(manifest, pred)

    rc = main([
        "--predictions", str(pred),
        "--clean-oos-manifest", str(manifest),
        "--sim-db", str(db),
        "--fwd-horizon-days", "60",
        "--out-dir", str(tmp_path / "out"),
    ])
    assert rc == 2
    assert "overlapping multi-day returns" in capsys.readouterr().err
