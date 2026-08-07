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


# --- role filter (orch#899) --------------------------------------------------

def _roled_db(path, runs):
    """A runs DB WITH the role column, as every live DB has had since it landed."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE candidate_scores "
                 "(run_id TEXT, rank_score REAL, role TEXT)")
    for rid, scores, role in runs:
        conn.executemany("INSERT INTO candidate_scores VALUES (?, ?, ?)",
                         [(rid, s, role) for s in scores])
    conn.commit()
    return conn


def _two_role_runs(n_runs=5, n_cand=60, n_hold=60, cand=0.0, hold=100.0):
    import numpy as np
    rng = np.random.default_rng(7)
    out = []
    for i in range(n_runs):
        rid = f"2026-08-{i + 1:02d}-live-x"
        out.append((rid, list(rng.normal(cand, 1.0, n_cand)), "candidate"))
        out.append((rid, list(rng.normal(hold, 1.0, n_hold)), "holding"))
    return out


class TestOnlyCandidateRowsAreScored:
    """`candidate_scores` holds two populations whose `rank_score` is not the
    same quantity: candidates carry the scorer's z-composite, holdings carry
    `calibrate_probability(panel_score)`. Measured live 2026-08-07, one run spans
    [-2.667, 3.050] on the candidate side and [0.104, 0.340] on the hold side —
    pooling them is a PSI over a mixture of two units."""

    def test_holdings_are_excluded_from_both_windows(self, tmp_path):
        from renquant_pipeline.kernel.score_drift import load_score_drift_from_db
        conn = _roled_db(tmp_path / "a.db", _two_role_runs())
        rep = load_score_drift_from_db(conn)
        assert rep is not None
        assert rep.n_current == 60, (
            f"120 rows per run, 60 of them holdings: {rep.n_current}")
        assert rep.n_baseline == 4 * 60, rep.n_baseline

    def test_the_exclusion_is_by_ROLE_not_by_value(self, tmp_path):
        """Anti-vacuity. Holdings on the SAME scale as candidates must be dropped
        too — otherwise this suite would pass on a filter that merely trimmed
        outliers."""
        from renquant_pipeline.kernel.score_drift import load_score_drift_from_db
        far = load_score_drift_from_db(
            _roled_db(tmp_path / "far.db", _two_role_runs(hold=100.0)))
        near = load_score_drift_from_db(
            _roled_db(tmp_path / "near.db", _two_role_runs(hold=0.0)))
        assert far is not None and near is not None
        assert far.n_current == near.n_current == 60
        assert far.n_baseline == near.n_baseline

    def test_a_bar_that_is_full_only_because_of_holdings_is_not_the_latest_run(
            self, tmp_path):
        """Live this dropped exactly one run of 95: it cleared MIN_SCORES_PER_RUN
        only because holding rows padded it past 30. Counting a sell-only bar as
        a full scoring run because positions were persisted alongside it is the
        same category error the role filter exists to remove."""
        from renquant_pipeline.kernel.score_drift import (
            load_score_drift_from_db, MIN_SCORES_PER_RUN)
        runs = [(f"2026-08-{i + 1:02d}-live-x",
                 [0.5 + 0.001 * i] * (MIN_SCORES_PER_RUN + 5), "candidate")
                for i in range(4)]
        runs.append(("2026-08-09-live-x", [0.5] * 5, "candidate"))
        runs.append(("2026-08-09-live-x", [0.5] * MIN_SCORES_PER_RUN, "holding"))
        rep = load_score_drift_from_db(_roled_db(tmp_path / "p.db", runs))
        assert rep is not None
        assert not rep.run_id.startswith("2026-08-09"), rep.run_id


def test_persisted_role_filtered_row_is_scored_by_the_read_only_audit(tmp_path):
    """End-to-end regression (PR #281 review, P1): the audit script had its
    own unfiltered `_load_full_runs` query while `load_score_drift_from_db`
    filtered to candidate rows only. A monitor persisting `n_baseline` from
    the candidate-only population would then hit the audit script's
    candidate+holding reconstruction, which either fails the size-parity
    check (marked unreconstructable) or, if counts happened to coincide,
    silently scores the wrong mixed population. Proves --persist on a
    mixed-role DB is reconstructed and scored using the SAME candidate-only
    baseline the monitor used."""
    db = tmp_path / "roled.db"
    conn = _roled_db(db, _two_role_runs(n_runs=5))
    conn.close()

    code, reports = monitor([str(db)], persist=True)
    assert code == 1 and reports[0]["persisted"] is True
    assert reports[0]["n_baseline"] == 4 * 60  # candidate rows only

    result = audit(str(db))
    assert result["n_rows"] == 1
    assert result["n_unreconstructable"] == 0
    assert result["n_scored"] == 1


def test_a_db_without_the_role_column_still_works(tmp_path):
    """The column is optional. Older runs DBs and every minimal fixture create
    `candidate_scores(run_id, rank_score)`; an unconditional filter raises
    `OperationalError: no such column: role` and takes the monitor down."""
    from renquant_pipeline.kernel.score_drift import load_score_drift_from_db
    p = tmp_path / "legacy.db"
    _make_db(p, [(f"2026-08-{i + 1:02d}-live-x", [0.4 + 0.001 * i] * 40)
                 for i in range(5)])
    rep = load_score_drift_from_db(sqlite3.connect(str(p)))
    assert rep is not None
    assert rep.n_current == 40 and rep.n_baseline == 160
