"""score_drift_audits persistence tests (L6 sidecar item 3)."""
from __future__ import annotations

import datetime

from renquant_pipeline.kernel.persistence import (
    get_connection,
    record_score_drift_audit,
)
from renquant_pipeline.kernel.score_drift import DriftReport

RUN_DATE = datetime.date(2026, 6, 13)


def _conn(tmp_path):
    return get_connection({"persistence": {"enabled": True,
                                           "db_path": str(tmp_path / "runs.db")}})


class TestRecord:
    def test_records_report(self, tmp_path):
        conn = _conn(tmp_path)
        r = DriftReport(psi=0.52, severity="CRITICAL", n_baseline=1577,
                        n_current=85, ok=False)
        assert record_score_drift_audit(conn, run_id="r1", run_date=RUN_DATE,
                                        report=r) == 1
        row = conn.execute(
            "SELECT run_id, severity, psi, n_baseline, n_current "
            "FROM score_drift_audits").fetchone()
        assert row == ("r1", "CRITICAL", 0.52, 1577, 85)

    def test_nan_psi_stored_as_null(self, tmp_path):
        conn = _conn(tmp_path)
        r = DriftReport(psi=float("nan"), severity="WARN", n_baseline=3,
                        n_current=1, ok=False)
        record_score_drift_audit(conn, run_id="r2", run_date=RUN_DATE, report=r)
        psi = conn.execute("SELECT psi FROM score_drift_audits "
                           "WHERE run_id='r2'").fetchone()[0]
        assert psi is None

    def test_append_only_history(self, tmp_path):
        conn = _conn(tmp_path)
        for i, sev in enumerate(["INFO", "WARN", "CRITICAL"]):
            r = DriftReport(psi=0.1 * (i + 1), severity=sev, n_baseline=100,
                            n_current=50, ok=(sev == "INFO"))
            record_score_drift_audit(conn, run_id=f"r{i}",
                                    run_date=RUN_DATE + datetime.timedelta(days=i),
                                    report=r)
        n = conn.execute("SELECT COUNT(*) FROM score_drift_audits").fetchone()[0]
        assert n == 3

    def test_forensics_query(self, tmp_path):
        # "which days had CRITICAL drift" = one SELECT
        conn = _conn(tmp_path)
        record_score_drift_audit(conn, run_id="r1", run_date=RUN_DATE,
            report=DriftReport(0.52, "CRITICAL", 1577, 85, False))
        record_score_drift_audit(conn, run_id="r0",
            run_date=RUN_DATE - datetime.timedelta(days=1),
            report=DriftReport(0.05, "INFO", 1500, 80, True))
        rows = conn.execute(
            "SELECT run_date FROM score_drift_audits WHERE severity='CRITICAL'"
        ).fetchall()
        assert rows == [(RUN_DATE.isoformat(),)]

    def test_noop_paths(self, tmp_path):
        conn = _conn(tmp_path)
        assert record_score_drift_audit(None, run_id="r", run_date=RUN_DATE,
                                       report=None) == 0
        assert record_score_drift_audit(conn, run_id="r", run_date=RUN_DATE,
                                       report=None) == 0
