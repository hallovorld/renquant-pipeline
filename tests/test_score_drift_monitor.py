"""score_drift_monitor CLI tests (L6 sidecar companion)."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from audit_score_drift_excess import audit  # noqa: E402
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


class TestPersist:
    def test_persist_records_drift(self, tmp_path):
        import sqlite3
        import numpy as np
        rng = np.random.RandomState(0)
        runs = [(f"2026-06-{d:02d}-full", rng.normal(0.5, 0.1, 140).tolist())
                for d in range(1, 5)]
        runs.append(("2026-06-06-full", [0.5] * 140))  # collapse
        db = tmp_path / "runs.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE candidate_scores (run_id TEXT, rank_score REAL)")
        for rid, scores in runs:
            conn.executemany("INSERT INTO candidate_scores VALUES (?, ?)",
                             [(rid, s) for s in scores])
        conn.commit()
        conn.close()
        code, reports = monitor([str(db)], persist=True)
        assert code == 1 and reports[0]["persisted"] is True
        # the audit row was written into the same DB
        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT severity, psi FROM score_drift_audits").fetchone()
        conn.close()
        assert row[0] == "CRITICAL" and row[1] is not None

    def test_persisted_row_is_scored_by_the_read_only_audit(self, tmp_path):
        """End-to-end regression (PR #280 review, P1 round 3): `_persist_audit`
        used to hardcode `run_id=None`, and audit_score_drift_excess.py's
        `audit()` unconditionally skips any row with `run_id is None` — so
        every --persist row was permanently unscorable and the "coverage
        grows forward-only" claim in the progress doc was false. Proves a
        freshly persisted, provenance-tagged row is reconstructable and
        scored by the read-only audit."""
        rng = np.random.RandomState(0)
        runs = [(f"2026-06-{d:02d}-full", rng.normal(0.5, 0.1, 140).tolist())
                for d in range(1, 5)]
        runs.append(("2026-06-06-full", [0.5] * 140))  # collapse -> CRITICAL
        db = tmp_path / "runs.db"
        _make_db(db, runs)
        code, reports = monitor([str(db)], persist=True)
        assert code == 1 and reports[0]["persisted"] is True

        result = audit(str(db))
        assert result["n_rows"] == 1
        assert result["n_unreconstructable"] == 0
        assert result["n_scored"] == 1

    def test_no_persist_does_not_write(self, tmp_path):
        import sqlite3
        import numpy as np
        rng = np.random.RandomState(0)
        db = tmp_path / "runs.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE candidate_scores (run_id TEXT, rank_score REAL)")
        for d in range(1, 6):
            conn.executemany("INSERT INTO candidate_scores VALUES (?, ?)",
                             [(f"2026-06-{d:02d}-full", s)
                              for s in rng.normal(0.5, 0.1, 140).tolist()])
        conn.commit()
        conn.close()
        monitor([str(db)], persist=False)
        conn = sqlite3.connect(str(db))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "score_drift_audits" not in tables  # read-only path untouched
