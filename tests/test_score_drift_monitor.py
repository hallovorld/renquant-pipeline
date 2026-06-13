"""score_drift_monitor CLI tests (L6 sidecar companion)."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from score_drift_monitor import monitor  # noqa: E402


def _make_db(path, runs):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE candidate_scores (run_id TEXT, rank_score REAL)")
    for rid, scores in runs:
        conn.executemany("INSERT INTO candidate_scores VALUES (?, ?)",
                         [(rid, s) for s in scores])
    conn.commit()
    conn.close()


class TestMonitor:
    def test_stable_exit_0(self, tmp_path):
        rng = np.random.RandomState(0)
        runs = [(f"2026-06-{d:02d}-full", rng.normal(0.5, 0.1, 140).tolist())
                for d in range(1, 6)]
        db = tmp_path / "runs.db"
        _make_db(db, runs)
        code, reports = monitor([str(db)])
        assert code == 0 and reports[0]["status"] == "INFO"

    def test_collapse_exit_1(self, tmp_path):
        rng = np.random.RandomState(0)
        runs = [(f"2026-06-{d:02d}-full", rng.normal(0.5, 0.1, 140).tolist())
                for d in range(1, 5)]
        runs.append(("2026-06-06-full", [0.5] * 140))  # collapsed
        db = tmp_path / "runs.db"
        _make_db(db, runs)
        code, reports = monitor([str(db)])
        assert code == 1 and reports[0]["status"] == "CRITICAL"

    def test_insufficient_data_exit_2(self, tmp_path):
        db = tmp_path / "runs.db"
        _make_db(db, [("2026-06-01-full", [0.5] * 40)])
        code, reports = monitor([str(db)])
        assert code == 2 and reports[0]["status"] == "INSUFFICIENT_DATA"

    def test_unreadable_db_exit_3(self, tmp_path):
        code, reports = monitor([str(tmp_path / "nonexistent.db")])
        assert code == 3 and reports[0]["status"] == "UNREADABLE"

    def test_worst_across_dbs(self, tmp_path):
        rng = np.random.RandomState(0)
        good = tmp_path / "good.db"
        _make_db(good, [(f"2026-06-{d:02d}-full", rng.normal(0.5, 0.1, 140).tolist())
                        for d in range(1, 6)])
        code, reports = monitor([str(good), str(tmp_path / "missing.db")])
        assert code == 3  # worst wins
        assert len(reports) == 2
