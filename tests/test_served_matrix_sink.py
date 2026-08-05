"""orch#703: the served feature matrix must survive the run that used it.

MEASURED 2026-08-01: `build_inference_matrix` produces the matrix that decides
every trade and nothing writes it down — `job_panel_scoring` reads it as
`ctx._panel_matrix`, an in-memory attribute. PRIORITY RAISED 2026-08-04: five
fleet lanes now pick DIFFERENT names on the same day (prod NVDA/GOOG/WELL/VLO,
RC AMZN, RSs SPG, RCS BWXT) and there is no way to answer "why" the next
morning.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from renquant_pipeline.kernel.panel_pipeline import served_matrix_sink as sink
from renquant_pipeline.kernel.panel_pipeline.task_persist_served_matrix import (
    PersistServedMatrixTask,
)


def _ctx(tmp_path, *, matrix=True, lane="alpaca", run_id="2026-08-04-live-abc123"):
    X = pd.DataFrame(
        {"f_mom": [0.5, -0.2, 1.1], "f_val": [1.0, 2.0, float("nan")]},
        index=["NVDA", "SPG", "BWXT"],
    )
    cands = [
        SimpleNamespace(ticker="NVDA", panel_score=0.9, rank_score=0.62, mu=0.03,
                        sigma=0.11, kelly_target_pct=1.9),
        SimpleNamespace(ticker="BWXT", panel_score=0.4, rank_score=0.31, mu=0.01,
                        sigma=0.20, kelly_target_pct=0.0),
    ]
    holdings = {"SPG": SimpleNamespace(ticker="SPG", panel_score=0.7, rank_score=0.55,
                                       mu=0.02, sigma=0.09, kelly_target_pct=3.0)}
    return SimpleNamespace(
        config={"_strategy_dir": str(tmp_path)},
        today=dt.date(2026, 8, 4),
        run_id=run_id,
        broker_name=lane,
        candidates=cands,
        holdings=holdings,
        buy_blocked=False,
        _panel_matrix=X if matrix else None,
        _panel_scorer=SimpleNamespace(
            metadata={"kind": "blend", "trained_date": "2026-08-02",
                      "config_fingerprint": "sha256:f8fb2259b2bf1537",
                      "content_sha256": "sha256:6461b827ab2339a8"},
            feature_cols=["f_mom", "f_val"],
        ),
    )


class TestItActuallyPersists:
    def test_the_matrix_and_the_decision_surface_land_together(self, tmp_path):
        ctx = _ctx(tmp_path)
        assert PersistServedMatrixTask().run(ctx) is None
        out = tmp_path / "logs" / "served_matrix" / "2026-08-04"
        parquet = out / "alpaca__2026-08-04-live-abc123.parquet"
        assert parquet.exists() and (out / "alpaca__2026-08-04-live-abc123.json").exists()

        df = pd.read_parquet(parquet).set_index("ticker")
        # every served feature, as served (NaN preserved as missing)
        assert list(df.loc["NVDA"][["f_mom", "f_val"]]) == [0.5, 1.0]
        assert pd.isna(df.loc["BWXT", "f_val"])
        # ... and the surface that decided, not just the raw score
        assert df.loc["NVDA", "rank_score"] == pytest.approx(0.62)
        assert df.loc["NVDA", "kelly_target_pct"] == pytest.approx(1.9)
        assert bool(df.loc["NVDA", "is_candidate"]) and not bool(df.loc["NVDA", "is_holding"])
        assert bool(df.loc["SPG", "is_holding"]) and not bool(df.loc["SPG", "is_candidate"])

    def test_the_sidecar_carries_the_identity_that_makes_it_readable_later(self, tmp_path):
        ctx = _ctx(tmp_path)
        PersistServedMatrixTask().run(ctx)
        m = json.loads((tmp_path / "logs" / "served_matrix" / "2026-08-04"
                        / "alpaca__2026-08-04-live-abc123.json").read_text())
        assert m["schema_version"] == sink.SCHEMA_VERSION
        assert m["lane"] == "alpaca" and m["run_id"] == "2026-08-04-live-abc123"
        assert m["n_rows"] == 3 and m["n_feature_cols"] == 2
        assert m["scorer"]["kind"] == "blend"
        assert m["scorer"]["content_sha256"] == "sha256:6461b827ab2339a8"
        assert m["scorer"]["trained_date"] == "2026-08-02"

    def test_two_lanes_on_the_same_day_do_not_overwrite_each_other(self, tmp_path):
        """The whole point: five lanes, same date, different picks."""
        for lane, run in (("alpaca", "r1"), ("alpaca_shadow_blend_rb_mom", "r2")):
            PersistServedMatrixTask().run(_ctx(tmp_path, lane=lane, run_id=run))
        day = tmp_path / "logs" / "served_matrix" / "2026-08-04"
        assert sorted(p.name for p in day.glob("*.parquet")) == [
            "alpaca__r1.parquet", "alpaca_shadow_blend_rb_mom__r2.parquet"]


class TestItNeverBreaksARun:
    def test_a_missing_matrix_is_logged_not_raised(self, tmp_path):
        assert PersistServedMatrixTask().run(_ctx(tmp_path, matrix=False)) is None
        assert not (tmp_path / "logs" / "served_matrix").exists()

    def test_a_write_failure_is_logged_not_raised(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sink, "write_served_matrix",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        import renquant_pipeline.kernel.panel_pipeline.task_persist_served_matrix as T
        assert T.PersistServedMatrixTask().run(_ctx(tmp_path)) is None

    def test_a_broken_scorer_object_does_not_stop_the_write(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx._panel_scorer = object()  # no metadata, no feature_cols
        assert PersistServedMatrixTask().run(ctx) is None
        m = json.loads(next((tmp_path / "logs" / "served_matrix" / "2026-08-04")
                            .glob("*.json")).read_text())
        assert m["scorer"]["kind"] is None, "absent must read as absent, not defaulted"
        assert m["n_rows"] == 3, "the matrix still lands even without scorer identity"

    def test_no_strategy_dir_SKIPS_rather_than_scattering_files(self, tmp_path, monkeypatch):
        ctx = _ctx(tmp_path)
        ctx.config = {}
        monkeypatch.chdir(tmp_path)
        assert PersistServedMatrixTask().run(ctx) is None
        assert not (tmp_path / "logs").exists()

    def test_an_explicit_disable_is_honoured(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.config = {"_strategy_dir": str(tmp_path), "served_matrix": {"enabled": False}}
        assert PersistServedMatrixTask().run(ctx) is None
        assert not (tmp_path / "logs").exists()


class TestDurability:
    def test_no_incoming_file_survives_a_successful_write(self, tmp_path):
        PersistServedMatrixTask().run(_ctx(tmp_path))
        day = tmp_path / "logs" / "served_matrix" / "2026-08-04"
        assert not list(day.glob("*.incoming"))

    def test_a_failed_parquet_write_leaves_no_torn_file_and_no_sidecar(self, tmp_path, monkeypatch):
        """The sidecar lands LAST, so its presence means the pair is complete."""
        real = pd.DataFrame.to_parquet

        def boom(self, path, *a, **k):
            Path(path).write_bytes(b"TORN")
            raise OSError("no space left on device")

        monkeypatch.setattr(pd.DataFrame, "to_parquet", boom)
        ctx = _ctx(tmp_path)
        assert PersistServedMatrixTask().run(ctx) is None
        monkeypatch.setattr(pd.DataFrame, "to_parquet", real)
        day = tmp_path / "logs" / "served_matrix" / "2026-08-04"
        assert not list(day.glob("*.parquet")) and not list(day.glob("*.json"))
        assert not list(day.glob("*.incoming"))


def test_it_runs_LAST_in_the_panel_scoring_chain():
    """Placement is the design: the raw scorer output alone does not explain a
    buy — rank_score after calibration, mu/sigma after NGBoost and the Kelly
    target do. If someone moves it earlier, this fails."""
    from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import PanelScoringJob
    names = [type(t).__name__ for t in PanelScoringJob().tasks]
    assert names[-1] == "PersistServedMatrixTask"
    for earlier in ("ApplyGlobalCalibrationTask", "ApplyNGBoostTask", "ApplyKellySizingTask"):
        assert names.index(earlier) < names.index("PersistServedMatrixTask"), earlier
