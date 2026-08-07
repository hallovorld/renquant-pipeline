"""audit_score_drift_excess CLI tests (reproduction for PR #279's evidence)."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from audit_score_drift_excess import audit  # noqa: E402


def _make_db(path, audit_rows, candidate_rows=()):
    """``audit_rows`` entries are
    ``(run_id, psi, severity, n_baseline, n_current, baseline_run_ids)``
    where ``baseline_run_ids`` is a list of run_ids (JSON-encoded on insert)
    or None for a row with no persisted provenance (predates PR #280)."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE score_drift_audits "
        "(run_id TEXT, psi REAL, severity TEXT, n_baseline INTEGER, "
        "n_current INTEGER, baseline_run_ids_json TEXT)")
    conn.executemany(
        "INSERT INTO score_drift_audits VALUES (?, ?, ?, ?, ?, ?)",
        [(rid, psi, sev, n_b, n_c,
          json.dumps(bids) if bids is not None else None)
         for rid, psi, sev, n_b, n_c, bids in audit_rows])
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
    # The persisted baseline is the 20 runs immediately preceding the
    # current one, 40 scores each = 800; must match what audit() reconstructs
    # from that exact list, or the provenance check (PR #280 review, P1)
    # reports these unreconstructable.
    baseline_ids = run_ids[1:-1]
    n_baseline = 800
    _make_db(db, [
        (run_ids[-1], 0.02, "INFO", n_baseline, 83, baseline_ids),        # near the floor: excess ~1x
        (run_ids[-1], 0.50, "CRITICAL", n_baseline, 83, baseline_ids),    # well above the floor
        (run_ids[-1], 0.60, "CRITICAL", n_baseline, 83, baseline_ids),
    ], candidate_rows)
    result = audit(str(db))
    assert result["n_rows"] == 3 and result["n_scored"] == 3
    assert result["n_unreconstructable"] == 0
    assert sum(result["counts"].values()) == 3
    assert result["critical_below_floor"] == 0


def test_does_not_write_to_the_db(tmp_path):
    db = tmp_path / "runs.db"
    _make_db(db, [("gone", 0.30, "CRITICAL", 1509, 83, None)])
    before = db.stat().st_mtime_ns
    audit(str(db))
    assert db.stat().st_mtime_ns == before


def test_rows_whose_baseline_was_pruned_are_reported_not_silently_scored(tmp_path):
    """A row whose persisted `baseline_run_ids_json` names runs no longer in
    `candidate_scores` (rotated out) must be counted as unreconstructable,
    not silently dropped or scored off a partial baseline."""
    db = tmp_path / "runs.db"
    _make_db(db, [("gone", 0.30, "CRITICAL", 1509, 83, ["b0", "b1"])],
             candidate_rows=[])
    result = audit(str(db))
    assert result["n_rows"] == 1
    assert result["n_unreconstructable"] == 1
    assert result["n_scored"] == 0


def test_legacy_rows_without_baseline_provenance_stay_unreconstructable(tmp_path):
    """Rows written before `baseline_run_ids_json` existed have no record of
    which exact runs backed the baseline — even when `candidate_scores`
    still holds a run set whose SIZE happens to match the stored
    `n_baseline`. Inferring the window from count alone is exactly the P1
    bug (PR #280 review): a size match is not proof it's the historical
    window. Legacy rows must be unreconstructable, full stop, until
    re-audited under the new schema."""
    rng = np.random.default_rng(4)
    db = tmp_path / "runs.db"
    candidate_rows = _full_run("base0", 40, rng) + _full_run("run1", 40, rng)
    _make_db(db, [("run1", 0.30, "CRITICAL", 40, 40, None)], candidate_rows)
    result = audit(str(db))
    assert result["n_rows"] == 1
    assert result["n_unreconstructable"] == 1
    assert result["n_scored"] == 0


def test_rows_with_an_unusable_floor_are_excluded_not_counted_as_zero(tmp_path):
    rng = np.random.default_rng(2)
    db = tmp_path / "runs.db"
    # baseline reconstructs fine (base0's raw scores are still on disk and
    # its size matches the stored n_baseline=40 exactly); the stored
    # n_current=0 is the degenerate part that makes the floor unusable.
    candidate_rows = _full_run("base0", 40, rng) + _full_run("run1", 40, rng)
    _make_db(db, [("run1", 0.30, "CRITICAL", 40, 0, ["base0"])], candidate_rows)
    result = audit(str(db))
    assert result["n_rows"] == 1 and result["n_scored"] == 0
    assert result["n_unreconstructable"] == 0


def test_a_db_that_predates_the_provenance_column_does_not_crash(tmp_path):
    """A DB opened mode=ro that predates PR #280's baseline_run_ids_json
    migration has no such column, and this read-only script can never
    ALTER TABLE to add it. Every row on such a DB is unreconstructable, not
    a crash."""
    db = tmp_path / "runs.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE score_drift_audits "
        "(run_id TEXT, psi REAL, severity TEXT, n_baseline INTEGER, "
        "n_current INTEGER)")   # no baseline_run_ids_json column
    conn.execute(
        "INSERT INTO score_drift_audits VALUES (?, ?, ?, ?, ?)",
        ("r1", 0.30, "CRITICAL", 1509, 83))
    conn.execute("CREATE TABLE candidate_scores (run_id TEXT, rank_score REAL)")
    conn.commit()
    conn.close()
    result = audit(str(db))
    assert result["n_rows"] == 1
    assert result["n_unreconstructable"] == 1
    assert result["n_scored"] == 0


def test_full_run_substitution_is_not_silently_accepted(tmp_path):
    """Reviewer's P1 counterexample (PR #280 review): historical `run2` was
    audited against baseline `[run0, run1]` (40 scores each, n_baseline=80).
    By the time this script runs, `run1` has been pruned from
    `candidate_scores`, but an older, unrelated run `run_older` happens to
    still hold exactly 40 scores. A count-based reconstruction (the old
    approach this PR removes) would splice `[run_older, run0]` together,
    see size 80, and report it as verified evidence — even though half the
    real baseline (`run1`) is gone. The exact-run-ID check must instead
    fail outright, because `run1` itself is not in `candidate_scores`,
    regardless of what else is available to match the count."""
    rng = np.random.default_rng(3)
    db = tmp_path / "runs.db"
    candidate_rows = (_full_run("run_older", 40, rng)
                      + _full_run("run0", 40, rng)
                      + _full_run("run2", 83, rng))   # run1 deliberately absent
    _make_db(db, [("run2", 0.30, "CRITICAL", 80, 83, ["run0", "run1"])],
             candidate_rows)
    result = audit(str(db))
    assert result["n_rows"] == 1
    assert result["n_unreconstructable"] == 1
    assert result["n_scored"] == 0
