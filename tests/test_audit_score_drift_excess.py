"""audit_score_drift_excess CLI tests (reproduction for PR #279's evidence)."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from audit_score_drift_excess import audit  # noqa: E402


def _make_db(path, rows):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE score_drift_audits ("
        "psi REAL, severity TEXT, n_baseline INTEGER, n_current INTEGER)")
    conn.executemany(
        "INSERT INTO score_drift_audits VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def test_bands_and_counts_a_mixed_population(tmp_path):
    db = tmp_path / "runs.db"
    _make_db(db, [
        (0.02, "INFO", 1509, 83),       # near the floor: excess ~1x
        (0.50, "CRITICAL", 1509, 83),   # well above the floor
        (0.60, "CRITICAL", 1509, 83),
    ])
    result = audit(str(db))
    assert result["n_rows"] == 3 and result["n_scored"] == 3
    assert sum(result["counts"].values()) == 3
    assert result["critical_below_floor"] == 0


def test_does_not_write_to_the_db(tmp_path):
    db = tmp_path / "runs.db"
    _make_db(db, [(0.30, "CRITICAL", 1509, 83)])
    before = db.stat().st_mtime_ns
    audit(str(db))
    assert db.stat().st_mtime_ns == before


def test_rows_with_an_unusable_floor_are_excluded_not_counted_as_zero(tmp_path):
    db = tmp_path / "runs.db"
    _make_db(db, [(0.30, "CRITICAL", 5, 0)])  # degenerate shape -> NaN floor
    result = audit(str(db))
    assert result["n_rows"] == 1 and result["n_scored"] == 0
