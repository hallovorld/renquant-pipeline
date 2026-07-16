"""Tests for training_cutoff + model_content_sha256 persistence in pipeline_runs.

G4 DATA-BOUND unblock: the canonical admissibility validator rejects all
backfilled scores because runs.alpaca.db never persists the training cutoff
or model fingerprint. This module tests the schema migration and the
record_pipeline_run write path for the two new columns.
"""
from __future__ import annotations

import datetime
import sqlite3

from renquant_pipeline.kernel.persistence import (
    ensure_schema,
    get_connection,
    record_pipeline_run,
)

RUN_DATE = datetime.date(2026, 7, 15)


def _conn(tmp_path):
    return get_connection({"persistence": {"enabled": True,
                                           "db_path": str(tmp_path / "runs.db")}})


class TestTrainingMetadataColumns:

    def test_new_db_has_columns(self, tmp_path):
        conn = _conn(tmp_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pipeline_runs)")}
        assert "training_cutoff" in cols
        assert "model_content_sha256" in cols

    def test_migration_adds_columns_to_existing_db(self, tmp_path):
        db = tmp_path / "runs.db"
        conn = sqlite3.connect(str(db))
        conn.execute("""CREATE TABLE pipeline_runs (
            run_id TEXT PRIMARY KEY,
            run_date DATE NOT NULL,
            run_type TEXT NOT NULL,
            strategy TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.commit()
        ensure_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pipeline_runs)")}
        assert "training_cutoff" in cols
        assert "model_content_sha256" in cols
        conn.close()

    def test_record_persists_training_metadata(self, tmp_path):
        conn = _conn(tmp_path)
        run_id = record_pipeline_run(
            conn,
            run_type="live",
            run_date=RUN_DATE,
            strategy="renquant_104",
            training_cutoff="2026-04-15",
            model_content_sha256="abc123def456",
        )
        row = conn.execute(
            "SELECT training_cutoff, model_content_sha256 FROM pipeline_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row == ("2026-04-15", "abc123def456")

    def test_record_without_metadata_stores_null(self, tmp_path):
        conn = _conn(tmp_path)
        run_id = record_pipeline_run(
            conn,
            run_type="live",
            run_date=RUN_DATE,
            strategy="renquant_104",
        )
        row = conn.execute(
            "SELECT training_cutoff, model_content_sha256 FROM pipeline_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row == (None, None)

    def test_backcompat_existing_callers_unaffected(self, tmp_path):
        conn = _conn(tmp_path)
        run_id = record_pipeline_run(
            conn,
            run_type="sim",
            run_date=RUN_DATE,
            strategy="renquant_104",
            regime="BULL_CALM",
            confidence=0.75,
            portfolio_value=10000.0,
            cash=2000.0,
            n_candidates=5,
            n_exits=1,
            n_rotations=0,
            n_buys=2,
        )
        row = conn.execute(
            "SELECT run_type, regime, training_cutoff, model_content_sha256 "
            "FROM pipeline_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row[0] == "sim"
        assert row[1] == "BULL_CALM"
        assert row[2] is None
        assert row[3] is None
