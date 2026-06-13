"""reconciliation_actions persistence tests (eng plan §III.4)."""
from __future__ import annotations

import datetime

from renquant_pipeline.kernel.broker_reconciliation import reconcile
from renquant_pipeline.kernel.persistence import (
    get_connection,
    record_reconciliation_actions,
)

RUN_DATE = datetime.date(2026, 6, 13)


def _conn(tmp_path):
    return get_connection({"persistence": {"enabled": True,
                                           "db_path": str(tmp_path / "runs.db")}})


class TestRecord:
    def test_records_non_ok_actions(self, tmp_path):
        conn = _conn(tmp_path)
        # the real 2026-06-11 event: GE/META/HON vanished
        actions = reconcile({"MU": 1, "GE": 1, "META": 1, "HON": 1, "EQIX": 1},
                            {"MU": 1, "EQIX": 1})
        n = record_reconciliation_actions(conn, run_id="r1", run_date=RUN_DATE,
                                          actions=actions)
        assert n == 3  # 3 EXT_SELL; the 2 OK rows skipped
        rows = conn.execute(
            "SELECT kind, ticker FROM reconciliation_actions ORDER BY ticker"
        ).fetchall()
        assert rows == [("EXT_SELL", "GE"), ("EXT_SELL", "HON"),
                        ("EXT_SELL", "META")]

    def test_ok_only_writes_nothing(self, tmp_path):
        conn = _conn(tmp_path)
        actions = reconcile({"MU": 1}, {"MU": 1})  # all OK
        assert record_reconciliation_actions(conn, run_id="r1",
                                             run_date=RUN_DATE,
                                             actions=actions) == 0

    def test_quantities_recorded(self, tmp_path):
        conn = _conn(tmp_path)
        actions = reconcile({"MU": 5}, {"MU": 2})  # ADOPT_QTY
        record_reconciliation_actions(conn, run_id="r1", run_date=RUN_DATE,
                                      actions=actions)
        row = conn.execute("SELECT kind, state_qty, broker_qty "
                           "FROM reconciliation_actions").fetchone()
        assert row == ("ADOPT_QTY", 5.0, 2.0)

    def test_forensics_query(self, tmp_path):
        # "which days had a FORCED_COVER" = one SELECT
        conn = _conn(tmp_path)
        record_reconciliation_actions(conn, run_id="r1", run_date=RUN_DATE,
            actions=reconcile({"MU": 1}, {"MU": -1}))  # FORCED_COVER
        rows = conn.execute("SELECT run_date FROM reconciliation_actions "
                            "WHERE kind='FORCED_COVER'").fetchall()
        assert rows == [(RUN_DATE.isoformat(),)]

    def test_append_only(self, tmp_path):
        conn = _conn(tmp_path)
        a = reconcile({"MU": 1}, {})  # EXT_SELL
        record_reconciliation_actions(conn, run_id="r1", run_date=RUN_DATE, actions=a)
        record_reconciliation_actions(conn, run_id="r2", run_date=RUN_DATE, actions=a)
        n = conn.execute("SELECT COUNT(*) FROM reconciliation_actions").fetchone()[0]
        assert n == 2


class TestNoOps:
    def test_none_conn(self, tmp_path):
        a = reconcile({"MU": 1}, {})
        assert record_reconciliation_actions(None, run_id="r", run_date=RUN_DATE,
                                             actions=a) == 0

    def test_none_run_id(self, tmp_path):
        a = reconcile({"MU": 1}, {})
        assert record_reconciliation_actions(_conn(tmp_path), run_id=None,
                                             run_date=RUN_DATE, actions=a) == 0

    def test_empty_actions(self, tmp_path):
        assert record_reconciliation_actions(_conn(tmp_path), run_id="r",
                                             run_date=RUN_DATE, actions=[]) == 0
