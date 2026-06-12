"""gate_verdicts decision ledger tests (eng plan S2-PR4 / errata C).

Pins: append-only rows with inputs preserved as sorted JSON; no-op on
disabled persistence / missing run_id / empty registry; the forensics
query shape ("which gate blocked on date D and why") works.
"""
from __future__ import annotations

import datetime
import json

from renquant_pipeline.kernel.gate_registry import GateRegistry
from renquant_pipeline.kernel.persistence import (
    get_connection,
    record_gate_verdicts,
)

RUN_DATE = datetime.date(2026, 6, 12)


def _conn(tmp_path):
    return get_connection({"persistence": {"enabled": True,
                                           "db_path": str(tmp_path / "runs.db")}})


def _registry() -> GateRegistry:
    reg = GateRegistry()
    reg.submit(gate="ema50", scope="book", verdict="block",
               reason="SPY below 50-day EMA", inputs={"data_outage": False})
    reg.submit(gate="earnings", scope="MU", verdict="halve",
               reason="earnings window", inputs={"days_to_event": 2})
    return reg


class TestRecord:

    def test_rows_appended_with_inputs_json(self, tmp_path):
        conn = _conn(tmp_path)
        n = record_gate_verdicts(conn, run_id="r1", run_date=RUN_DATE,
                                 registry=_registry())
        assert n == 2
        rows = conn.execute(
            "SELECT gate, scope, verdict, reason, inputs_json "
            "FROM gate_verdicts ORDER BY verdict_id").fetchall()
        assert rows[0][:3] == ("ema50", "book", "block")
        assert json.loads(rows[0][4]) == {"data_outage": False}
        assert rows[1][:3] == ("earnings", "MU", "halve")

    def test_append_only_two_runs_accumulate(self, tmp_path):
        conn = _conn(tmp_path)
        record_gate_verdicts(conn, run_id="r1", run_date=RUN_DATE,
                             registry=_registry())
        record_gate_verdicts(conn, run_id="r2", run_date=RUN_DATE,
                             registry=_registry())
        n = conn.execute("SELECT COUNT(*) FROM gate_verdicts").fetchone()[0]
        assert n == 4

    def test_forensics_query_shape(self, tmp_path):
        # The errata-C promise: "which gate blocked buys on date D" is SQL.
        conn = _conn(tmp_path)
        record_gate_verdicts(conn, run_id="r1", run_date=RUN_DATE,
                             registry=_registry())
        rows = conn.execute(
            "SELECT gate, reason FROM gate_verdicts "
            "WHERE run_date = ? AND verdict = 'block' AND scope = 'book'",
            (RUN_DATE.isoformat(),)).fetchall()
        assert rows == [("ema50", "SPY below 50-day EMA")]


class TestNoOps:

    def test_none_conn(self):
        assert record_gate_verdicts(None, run_id="r", run_date=RUN_DATE,
                                    registry=_registry()) == 0

    def test_none_run_id(self, tmp_path):
        assert record_gate_verdicts(_conn(tmp_path), run_id=None,
                                    run_date=RUN_DATE,
                                    registry=_registry()) == 0

    def test_empty_registry(self, tmp_path):
        assert record_gate_verdicts(_conn(tmp_path), run_id="r",
                                    run_date=RUN_DATE,
                                    registry=GateRegistry()) == 0

    def test_none_registry(self, tmp_path):
        assert record_gate_verdicts(_conn(tmp_path), run_id="r",
                                    run_date=RUN_DATE, registry=None) == 0
