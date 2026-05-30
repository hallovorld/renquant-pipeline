from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

from renquant_pipeline.kernel.persistence import record_training_run


def test_record_training_run_uses_common_canonical_columns(tmp_path: Path) -> None:
    db = tmp_path / "data" / "sim_runs.db"
    db.parent.mkdir()
    conn = sqlite3.connect(str(db))
    _create_training_runs(conn)

    run_id = record_training_run(
        conn,
        run_date=dt.datetime(2026, 5, 30, 9, 0, 0),
        strategy="renquant_104",
        artifact_type="panel_ltr_xgboost",
        config_snapshot={"a": 1},
        oos_mean_ic=0.1,
        train_ic=0.2,
        n_rows=10,
        feature_cols=["f1"],
        artifact_path="/tmp/artifact.json",
        elapsed_sec=3.0,
        trigger="unit",
        n_tickers=4,
        n_dates=5,
        n_features=1,
        device="cpu",
        deterministic=True,
        training_window_years=2.0,
        notes="ok",
        also_log_jsonl=False,
    )

    row = conn.execute(
        """SELECT run_id, run_date, strategy, artifact_type, config_json,
                  oos_mean_ic, train_ic, n_rows, feature_cols, artifact_path,
                  elapsed_sec, trigger, n_tickers, n_dates, n_features, device,
                  deterministic, training_window_years, notes
           FROM training_runs"""
    ).fetchone()
    conn.close()

    assert row[0] == run_id
    assert row[1:4] == ("2026-05-30T09:00:00", "renquant_104", "panel_ltr_xgboost")
    assert json.loads(row[4]) == {"a": 1}
    assert row[5:8] == (0.1, 0.2, 10)
    assert json.loads(row[8]) == ["f1"]
    assert row[9:] == ("/tmp/artifact.json", 3.0, "unit", 4, 5, 1, "cpu", 1, 2.0, "ok")


def _create_training_runs(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE training_runs (
            run_id TEXT PRIMARY KEY,
            run_date TIMESTAMP NOT NULL,
            strategy TEXT,
            artifact_type TEXT,
            config_json TEXT,
            oos_mean_ic REAL,
            train_ic REAL,
            n_rows INTEGER,
            feature_cols TEXT,
            artifact_path TEXT,
            commit_sha TEXT,
            elapsed_sec REAL,
            trigger TEXT,
            n_tickers INTEGER,
            n_dates INTEGER,
            n_features INTEGER,
            device TEXT,
            deterministic INTEGER,
            training_window_years REAL,
            notes TEXT
        )
    """)
    conn.commit()
