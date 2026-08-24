"""S3-P1: the served feature panel must persist, and must never break scoring.

CONTRACT MIRROR, NOT IMPORT. The consumer is renquant-orchestrator's
``FeatureSnapshot.from_mapping`` (shadow_realtime_serving.py:619), which
requires: non-empty ``feature_cutoff``, non-empty ``builder_version``,
non-empty ``features`` mapping. This repo must not import the orchestrator, so
those three requirements are pinned HERE as literal assertions — a drift in
either repo fails one side's tests instead of failing silently at 06:15.
"""
from __future__ import annotations

import json
import logging
import os
from types import SimpleNamespace

import pandas as pd
import pytest

from renquant_pipeline.kernel.panel_pipeline.feature_panel_export import (
    ExportFeaturePanelTask,
    write_feature_panel,
)


def _frame():
    return pd.DataFrame(
        {"alpha": [1.0, 2.5], "beta": [float("nan"), -3.25]},
        index=["AAPL", "MSFT"],
    )


class TestTheWriterHonoursTheConsumerContract:
    def test_the_three_required_keys_are_present_and_non_empty(self, tmp_path):
        panel, meta = write_feature_panel(_frame(), as_of="2026-08-24",
                                          out_dir=tmp_path, scorer_kind="blend",
                                          run_id="r1")
        d = json.loads(panel.read_text())
        assert d["feature_cutoff"] == "2026-08-24"
        assert d["builder_version"].startswith("feature_panel_export_v1+")
        assert d["features"] and d["features"]["AAPL"]["alpha"] == 1.0

    def test_nan_becomes_null_and_the_body_is_strict_json(self, tmp_path):
        panel, _ = write_feature_panel(_frame(), as_of="2026-08-24",
                                       out_dir=tmp_path, scorer_kind="blend",
                                       run_id=None)
        body = panel.read_text()
        assert "NaN" not in body, "json.dumps(allow_nan=False) must hold"
        assert json.loads(body)["features"]["AAPL"]["beta"] is None

    def test_the_meta_sha_matches_the_panel_content(self, tmp_path):
        import hashlib
        panel, meta = write_feature_panel(_frame(), as_of="2026-08-24",
                                          out_dir=tmp_path, scorer_kind="blend",
                                          run_id="r1")
        m = json.loads(meta.read_text())
        assert m["content_sha256"] == "sha256:" + hashlib.sha256(panel.read_bytes()).hexdigest()
        assert m["n_tickers"] == 2 and m["n_columns"] == 2 and m["null_cells"] == 1

    def test_an_empty_panel_is_REFUSED_not_written(self, tmp_path):
        """An empty features mapping is rejected by the consumer, so a written
        empty file would exist-but-not-load — worse than absence."""
        with pytest.raises(ValueError):
            write_feature_panel(pd.DataFrame(), as_of="2026-08-24",
                                out_dir=tmp_path, scorer_kind="blend", run_id=None)
        assert not list(tmp_path.iterdir())

    def test_a_rerun_overwrites_atomically(self, tmp_path):
        write_feature_panel(_frame(), as_of="2026-08-24", out_dir=tmp_path,
                            scorer_kind="blend", run_id="r1")
        X2 = _frame() * 2
        panel, _ = write_feature_panel(X2, as_of="2026-08-24", out_dir=tmp_path,
                                       scorer_kind="blend", run_id="r2")
        assert json.loads(panel.read_text())["features"]["AAPL"]["alpha"] == 2.0
        assert not list(tmp_path.glob("*.tmp")), "no torn/temp files left behind"


def _ctx(**kw):
    base = dict(candidates=[SimpleNamespace(ticker="AAPL")],
                _panel_matrix=_frame(), today=pd.Timestamp("2026-08-24"),
                _active_panel_scorer={"kind": "blend"}, run_id="r1")
    base.update(kw)
    return SimpleNamespace(**base)


class TestTheTaskNeverBreaksScoring:
    """Fail-open is the load-bearing property: this is an EXPORT of state the
    run already computed, bolted onto the capital path's scoring chain."""

    @pytest.fixture(autouse=True)
    def _redirect_data_root(self, tmp_path, monkeypatch):
        import renquant_pipeline.kernel.panel_pipeline.feature_panel_export as m
        monkeypatch.setattr(m, "data_root", lambda: tmp_path)
        monkeypatch.delenv("RENQUANT_READONLY_TAG", raising=False)
        monkeypatch.delenv("RENQUANT_DISABLE_FEATURE_PANEL_EXPORT", raising=False)
        self.out = tmp_path / "data" / "rq105"

    def test_the_prod_lane_writes(self):
        assert ExportFeaturePanelTask().run(_ctx()) is None
        assert (self.out / "feature_panel_2026-08-24.json").is_file()

    def test_a_readonly_lane_NEVER_writes(self, monkeypatch):
        """Per-lane writes would collide on the date-keyed filename; the prod
        lane owns the artifact."""
        monkeypatch.setenv("RENQUANT_READONLY_TAG", "alpaca_shadow")
        ExportFeaturePanelTask().run(_ctx())
        assert not self.out.exists()

    def test_a_candidateless_intraday_cycle_never_writes(self):
        """Every intraday run has n_candidates=0 (measured); without this guard
        the sell-only cycles would overwrite the daily panel ~35x/session with
        a holdings-only matrix."""
        ExportFeaturePanelTask().run(_ctx(candidates=[]))
        assert not self.out.exists()

    def test_the_kill_switch_wins(self, monkeypatch):
        monkeypatch.setenv("RENQUANT_DISABLE_FEATURE_PANEL_EXPORT", "1")
        ExportFeaturePanelTask().run(_ctx())
        assert not self.out.exists()

    def test_an_empty_matrix_is_skipped_not_written(self):
        ExportFeaturePanelTask().run(_ctx(_panel_matrix=pd.DataFrame()))
        assert not self.out.exists()

    def test_a_writer_explosion_is_a_WARNING_not_a_chain_failure(self, monkeypatch, caplog):
        import renquant_pipeline.kernel.panel_pipeline.feature_panel_export as m
        def boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(m, "write_feature_panel", boom)
        with caplog.at_level(logging.WARNING):
            assert ExportFeaturePanelTask().run(_ctx()) is None
        assert any("scoring unaffected" in r.message for r in caplog.records)


class TestTheTaskIsActuallyInTheChain:
    """A task nobody schedules is a document (the #600/#603 lesson, one layer
    down): assert the job's task list carries it, in the intended slot."""

    def test_panel_scoring_job_runs_the_export_after_apply_scores(self):
        from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import PanelScoringJob
        tasks = PanelScoringJob().tasks
        names = [type(t).__name__ for t in (tasks() if callable(tasks) else tasks)]
        assert "ExportFeaturePanelTask" in names
        assert names.index("ExportFeaturePanelTask") == names.index("ApplyScoresTask") + 1
