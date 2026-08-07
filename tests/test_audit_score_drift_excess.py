"""audit_score_drift_excess CLI tests (reproduction for PR #279's evidence)."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from audit_score_drift_excess import audit  # noqa: E402


def _make_db(path, audit_rows, candidate_rows=()):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE score_drift_audits "
        "(run_id TEXT, psi REAL, severity TEXT, n_baseline INTEGER, n_current INTEGER)")
    conn.executemany(
        "INSERT INTO score_drift_audits VALUES (?, ?, ?, ?, ?)", audit_rows)
    conn.execute("CREATE TABLE candidate_scores (run_id TEXT, rank_score REAL)")
    conn.executemany(
        "INSERT INTO candidate_scores VALUES (?, ?)", candidate_rows)
    conn.commit()
    conn.close()


def _full_run(run_id, n, rng):
    """`n` candidate_scores rows for one run (a "full" run needs n >= 30)."""
    return [(run_id, float(v)) for v in rng.normal(size=n)]


def test_bands_and_counts_a_mixed_population(tmp_path):
    rng = np.random.default_rng(1)
    db = tmp_path / "runs.db"
    run_ids = [f"run{i:02d}" for i in range(22)]
    candidate_rows = []
    for rid in run_ids[:-1]:
        candidate_rows += _full_run(rid, 40, rng)      # 21 trailing-eligible runs
    candidate_rows += _full_run(run_ids[-1], 83, rng)  # the "current" run
    _make_db(db, [
        (run_ids[-1], 0.02, "INFO", 1509, 83),       # near the floor: excess ~1x
        (run_ids[-1], 0.50, "CRITICAL", 1509, 83),   # well above the floor
        (run_ids[-1], 0.60, "CRITICAL", 1509, 83),
    ], candidate_rows)
    result = audit(str(db))
    assert result["n_rows"] == 3 and result["n_scored"] == 3
    assert result["n_unreconstructable"] == 0
    assert sum(result["counts"].values()) == 3
    assert result["critical_below_floor"] == 0


def test_does_not_write_to_the_db(tmp_path):
    db = tmp_path / "runs.db"
    _make_db(db, [("gone", 0.30, "CRITICAL", 1509, 83)])
    before = db.stat().st_mtime_ns
    audit(str(db))
    assert db.stat().st_mtime_ns == before


def test_rows_whose_baseline_was_pruned_are_reported_not_silently_scored(tmp_path):
    """`score_drift_audits` never stored raw scores, only n_baseline/n_current
    counts (PR #279 review, P1) — once `candidate_scores` rotates a run out,
    its floor can no longer be conditioned on the real baseline. Those rows
    must be counted as unreconstructable, not silently dropped or scored with
    the old (known-wrong) shape-only approximation."""
    db = tmp_path / "runs.db"
    _make_db(db, [("gone", 0.30, "CRITICAL", 1509, 83)], candidate_rows=[])
    result = audit(str(db))
    assert result["n_rows"] == 1
    assert result["n_unreconstructable"] == 1
    assert result["n_scored"] == 0


def test_rows_with_an_unusable_floor_are_excluded_not_counted_as_zero(tmp_path):
    rng = np.random.default_rng(2)
    db = tmp_path / "runs.db"
    # baseline reconstructs fine (base0 precedes run1); the stored
    # n_current=0 is the degenerate part that makes the floor unusable.
    candidate_rows = _full_run("base0", 40, rng) + _full_run("run1", 40, rng)
    _make_db(db, [("run1", 0.30, "CRITICAL", 1509, 0)], candidate_rows)
    result = audit(str(db))
    assert result["n_rows"] == 1 and result["n_scored"] == 0
    assert result["n_unreconstructable"] == 0
